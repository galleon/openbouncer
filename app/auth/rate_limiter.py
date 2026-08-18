import asyncio
import logging
import os
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Protocol

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SECONDS = 60.0
REDIS_URL_ENV_VAR = "REDIS_URL"
REDIS_KEY_PREFIX = "openbouncer:ratelimit:"


class SupportsRateLimiting(Protocol):
    async def check(self, key_id: str, limit: int) -> bool: ...


class RateLimiter:
    """Fixed-window per-key request counter, in-memory.

    MVP-level rate limiting: a single process-wide counter per key that
    resets every `window_seconds`. This is simple and good enough for a
    single-process deployment, but is not a sliding-window/token-bucket
    limiter (bursts right at a window boundary aren't smoothed) and does not
    coordinate across multiple processes/replicas -- use RedisRateLimiter
    (enabled via the REDIS_URL env var, see get_rate_limiter()) for that.
    """

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._clock = clock
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key_id: str, limit: int) -> bool:
        """Record one request for `key_id` and return whether it is allowed
        (True) or the key has exceeded `limit` requests in the current
        window (False). Always counts the request, even when rejected, so
        a client hammering the API doesn't get free retries.
        """
        async with self._lock:
            now = self._clock()
            window_start, count = self._buckets.get(key_id, (now, 0))
            if now - window_start >= self._window_seconds:
                window_start, count = now, 0

            count += 1
            self._buckets[key_id] = (window_start, count)
            return count <= limit


class RedisRateLimiter:
    """Fixed-window per-key request counter, backed by Redis.

    Same fixed-window semantics as RateLimiter, but the counter lives in
    Redis (INCR, with a TTL set only on the window's first increment) so
    multiple app processes/replicas share one budget per key instead of
    each enforcing its own.
    """

    def __init__(
        self,
        client: redis_asyncio.Redis,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self._client = client
        self._window_seconds = int(window_seconds)

    async def check(self, key_id: str, limit: int) -> bool:
        redis_key = f"{REDIS_KEY_PREFIX}{key_id}"
        try:
            count = await self._client.incr(redis_key)
            if count == 1:
                await self._client.expire(redis_key, self._window_seconds)
        except RedisError as exc:
            # Fail open: this runs on every authenticated request (see
            # require_api_key), so an unreachable/erroring Redis must not
            # turn every request into a 500. Rate limiting is a
            # best-effort abuse guard, not a security boundary -- trading
            # strict enforcement for availability during a Redis outage is
            # a deliberate choice here, not an oversight.
            logger.warning("Redis rate limiter unavailable, failing open: %s", exc)
            return True
        return count <= limit


def _build_rate_limiter() -> SupportsRateLimiting:
    redis_url = os.environ.get(REDIS_URL_ENV_VAR)
    if not redis_url:
        return RateLimiter()
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    return RedisRateLimiter(client)


@lru_cache
def get_rate_limiter() -> SupportsRateLimiting:
    return _build_rate_limiter()
