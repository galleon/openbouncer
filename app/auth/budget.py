"""Per-key token consumption budgets, enforced the same way rate limiting
is (a check on every authenticated request, in-memory or Redis-backed
depending on REDIS_URL -- see get_budget_tracker()) but for cumulative
*token* usage over a calendar-aligned window instead of a *request count*
over a rolling one.

Structurally this can't work exactly like app.auth.rate_limiter.RateLimiter:
a request's token cost isn't known until the upstream model has actually
generated a response, so there's no way to charge a request against its
budget before running it. What CAN be checked before running it is whether
the key has *already* exceeded its budget from prior requests this
window -- so, unlike RateLimiter.check() (which atomically records-then-
checks in one call), this is split into two calls used from two different
places:

- check() -- called from app.auth.dependency.require_api_key, before the
  request is processed, gating on already-accumulated usage.
- record() -- called from the route handlers (app/api/routes/chat.py,
  app/api/routes/embeddings.py) after a response's real/estimated token
  usage is known, the same place app.auth.usage.UsageTracker.record() is
  already called.

Windows are calendar-aligned (UTC midnight / UTC 1st-of-month), not a
rolling N seconds from first use -- "daily"/"monthly" budgets are meant to
reset predictably (matches OpenRouter's own "daily, weekly, or monthly
reset windows" framing for the same concept), not N seconds after
whenever a key happened to first make a request.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Protocol

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from app.auth.rate_limiter import REDIS_URL_ENV_VAR

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = "openbouncer:budget:"
# Comfortably longer than the window itself, so a quiet key's counter is
# still there next time it's used within the same window, but is cleaned
# up by Redis on its own afterward rather than accumulating forever (see
# BudgetTracker's own unbounded-growth caveat below for the in-memory
# case, which has no such cleanup).
_DAILY_TTL_SECONDS = 2 * 86400
_MONTHLY_TTL_SECONDS = 32 * 86400


def _daily_window_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _monthly_window_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


class SupportsBudgetTracking(Protocol):
    async def check(
        self, key_id: str, *, daily_limit: int | None, monthly_limit: int | None
    ) -> bool: ...

    async def record(self, key_id: str, tokens: int) -> None: ...


class BudgetTracker:
    """Basic in-memory per-key token budget tracking.

    MVP-level, same posture as app.auth.rate_limiter.RateLimiter: a single
    process-wide counter per (key, window), doesn't persist across
    restarts or coordinate across replicas -- use RedisBudgetTracker
    (enabled via the REDIS_URL env var, see get_budget_tracker()) for
    that. Unlike RateLimiter's buckets (which get overwritten in place
    every time a window rolls over), a *new* dict entry is created per
    calendar window key here, so unlike RateLimiter this dict grows for
    the life of the process -- one entry per key per day/month it was
    ever used. Acceptable for a long-running single process at realistic
    key counts, not bounded the way the Redis-backed version is (its
    TTLs expire old windows automatically); revisit if this ever needs to
    run unbounded in a very long-lived, very high-key-count deployment.
    """

    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._clock = clock
        self._daily: dict[tuple[str, str], int] = {}
        self._monthly: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    async def record(self, key_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        now = self._clock()
        async with self._lock:
            dkey = (key_id, _daily_window_key(now))
            mkey = (key_id, _monthly_window_key(now))
            self._daily[dkey] = self._daily.get(dkey, 0) + tokens
            self._monthly[mkey] = self._monthly.get(mkey, 0) + tokens

    async def check(
        self, key_id: str, *, daily_limit: int | None, monthly_limit: int | None
    ) -> bool:
        if daily_limit is None and monthly_limit is None:
            return True
        now = self._clock()
        async with self._lock:
            if daily_limit is not None:
                used = self._daily.get((key_id, _daily_window_key(now)), 0)
                if used >= daily_limit:
                    return False
            if monthly_limit is not None:
                used = self._monthly.get((key_id, _monthly_window_key(now)), 0)
                if used >= monthly_limit:
                    return False
        return True


class RedisBudgetTracker:
    """Same per-key token budget tracking as BudgetTracker, backed by
    Redis (INCRBY on a key that embeds the calendar window, with a TTL
    comfortably longer than that window) so multiple gateway replicas
    share one budget per key instead of each enforcing its own -- same
    reasoning and REDIS_URL trigger as RedisRateLimiter/RedisUsageTracker.
    """

    def __init__(self, client: redis_asyncio.Redis) -> None:
        self._client = client

    def _daily_key(self, key_id: str, now: datetime) -> str:
        return f"{REDIS_KEY_PREFIX}daily:{key_id}:{_daily_window_key(now)}"

    def _monthly_key(self, key_id: str, now: datetime) -> str:
        return f"{REDIS_KEY_PREFIX}monthly:{key_id}:{_monthly_window_key(now)}"

    async def record(self, key_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        now = datetime.now(timezone.utc)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                daily_key = self._daily_key(key_id, now)
                monthly_key = self._monthly_key(key_id, now)
                pipe.incrby(daily_key, tokens)
                pipe.expire(daily_key, _DAILY_TTL_SECONDS)
                pipe.incrby(monthly_key, tokens)
                pipe.expire(monthly_key, _MONTHLY_TTL_SECONDS)
                await pipe.execute()
        except RedisError as exc:
            # Best-effort, same "don't fail an already-completed request
            # over an accounting write" reasoning as
            # RedisUsageTracker.record.
            logger.warning("Redis budget tracker unavailable, dropping token record: %s", exc)

    async def check(
        self, key_id: str, *, daily_limit: int | None, monthly_limit: int | None
    ) -> bool:
        if daily_limit is None and monthly_limit is None:
            return True
        now = datetime.now(timezone.utc)
        try:
            if daily_limit is not None:
                raw = await self._client.get(self._daily_key(key_id, now))
                if int(raw or 0) >= daily_limit:
                    return False
            if monthly_limit is not None:
                raw = await self._client.get(self._monthly_key(key_id, now))
                if int(raw or 0) >= monthly_limit:
                    return False
        except RedisError as exc:
            # Fail open, same reasoning as RedisRateLimiter.check: this
            # runs on every authenticated request, so an unreachable Redis
            # must not turn every request into a 500. A token budget is a
            # cost-control guard, not a security boundary.
            logger.warning("Redis budget tracker unavailable, failing open: %s", exc)
            return True
        return True


def _build_budget_tracker() -> SupportsBudgetTracking:
    redis_url = os.environ.get(REDIS_URL_ENV_VAR)
    if not redis_url:
        return BudgetTracker()
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    return RedisBudgetTracker(client)


@lru_cache
def get_budget_tracker() -> SupportsBudgetTracking:
    return _build_budget_tracker()
