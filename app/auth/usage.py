import asyncio
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from app.auth.rate_limiter import REDIS_URL_ENV_VAR

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = "openbouncer:usage:"


@dataclass
class UsageStats:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class SupportsUsageTracking(Protocol):
    async def record(
        self,
        key_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None: ...

    async def get(self, key_id: str) -> UsageStats: ...

    async def all(self) -> dict[str, UsageStats]: ...


class UsageTracker:
    """Basic in-memory per-key usage accounting.

    Records token counts from whatever `usage` object a response ends up
    with (real upstream usage, or a word-count estimate where a real count
    isn't available -- see app.guardrails.service's streaming path) --
    "basic" by design: this is not billing-grade metering, and doesn't
    persist across restarts or coordinate across replicas. Use
    RedisUsageTracker (enabled via the REDIS_URL env var, see
    get_usage_tracker()) for that -- same MVP-vs-Redis split as
    app.auth.rate_limiter.
    """

    def __init__(self) -> None:
        self._stats: dict[str, UsageStats] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        key_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        async with self._lock:
            stats = self._stats.setdefault(key_id, UsageStats())
            stats.requests += 1
            stats.prompt_tokens += prompt_tokens
            stats.completion_tokens += completion_tokens
            stats.total_tokens += total_tokens

    async def get(self, key_id: str) -> UsageStats:
        return self._stats.get(key_id, UsageStats())

    async def all(self) -> dict[str, UsageStats]:
        """Every key with at least one recorded request -- used by
        /metrics to export per-key usage as Prometheus gauges (see
        app/api/routes/metrics.py). A plain dict copy, not a live view, so
        callers can't accidentally mutate internal state."""
        return dict(self._stats)


class RedisUsageTracker:
    """Same per-key usage accounting as UsageTracker, backed by a Redis
    hash per key (`HINCRBY` on requests/prompt_tokens/completion_tokens/
    total_tokens) so multiple gateway replicas share one running total per
    key instead of each keeping its own -- same reasoning and REDIS_URL
    trigger as app.auth.rate_limiter.RedisRateLimiter.
    """

    def __init__(self, client: redis_asyncio.Redis) -> None:
        self._client = client

    def _redis_key(self, key_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}{key_id}"

    async def record(
        self,
        key_id: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        redis_key = self._redis_key(key_id)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.hincrby(redis_key, "requests", 1)
                pipe.hincrby(redis_key, "prompt_tokens", prompt_tokens)
                pipe.hincrby(redis_key, "completion_tokens", completion_tokens)
                pipe.hincrby(redis_key, "total_tokens", total_tokens)
                await pipe.execute()
        except RedisError as exc:
            # Best-effort: this is awaited *after* a response has already
            # been produced (the upstream model already answered, or the
            # stream already finished) -- a request that already succeeded
            # must not turn into a 500 just because the accounting write
            # failed at the very end. Same "availability over strict
            # accounting during a Redis outage" trade-off as
            # RedisRateLimiter.check's fail-open behavior.
            logger.warning("Redis usage tracker unavailable, dropping usage record: %s", exc)

    async def get(self, key_id: str) -> UsageStats:
        try:
            data = await self._client.hgetall(self._redis_key(key_id))
        except RedisError as exc:
            logger.warning("Redis usage tracker unavailable, returning zeroed stats: %s", exc)
            return UsageStats()
        return _stats_from_redis_hash(data)

    async def all(self) -> dict[str, UsageStats]:
        result: dict[str, UsageStats] = {}
        try:
            async for redis_key in self._client.scan_iter(match=f"{REDIS_KEY_PREFIX}*"):
                key_id = redis_key[len(REDIS_KEY_PREFIX) :]
                data = await self._client.hgetall(redis_key)
                result[key_id] = _stats_from_redis_hash(data)
        except RedisError as exc:
            logger.warning(
                "Redis usage tracker unavailable, returning partial usage data: %s", exc
            )
        return result


def _stats_from_redis_hash(data: dict[str, str]) -> UsageStats:
    return UsageStats(
        requests=int(data.get("requests", 0)),
        prompt_tokens=int(data.get("prompt_tokens", 0)),
        completion_tokens=int(data.get("completion_tokens", 0)),
        total_tokens=int(data.get("total_tokens", 0)),
    )


def _build_usage_tracker() -> SupportsUsageTracking:
    redis_url = os.environ.get(REDIS_URL_ENV_VAR)
    if not redis_url:
        return UsageTracker()
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    return RedisUsageTracker(client)


@lru_cache
def get_usage_tracker() -> SupportsUsageTracking:
    return _build_usage_tracker()
