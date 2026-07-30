import pytest

from app.auth.rate_limiter import (
    REDIS_URL_ENV_VAR,
    RateLimiter,
    RedisRateLimiter,
    _build_rate_limiter,
)


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.asyncio
async def test_allows_requests_up_to_limit():
    clock = _FakeClock()
    limiter = RateLimiter(window_seconds=60.0, clock=clock)

    results = [await limiter.check("key-a", limit=3) for _ in range(3)]
    assert results == [True, True, True]


@pytest.mark.asyncio
async def test_rejects_requests_over_limit_within_window():
    clock = _FakeClock()
    limiter = RateLimiter(window_seconds=60.0, clock=clock)

    for _ in range(3):
        await limiter.check("key-a", limit=3)
    assert await limiter.check("key-a", limit=3) is False


@pytest.mark.asyncio
async def test_resets_after_window_elapses():
    clock = _FakeClock()
    limiter = RateLimiter(window_seconds=60.0, clock=clock)

    for _ in range(3):
        await limiter.check("key-a", limit=3)
    assert await limiter.check("key-a", limit=3) is False

    clock.advance(60.0)
    assert await limiter.check("key-a", limit=3) is True


@pytest.mark.asyncio
async def test_keys_are_tracked_independently():
    clock = _FakeClock()
    limiter = RateLimiter(window_seconds=60.0, clock=clock)

    for _ in range(3):
        await limiter.check("key-a", limit=3)
    assert await limiter.check("key-a", limit=3) is False
    # A different key has its own independent budget.
    assert await limiter.check("key-b", limit=3) is True


@pytest.mark.asyncio
async def test_rejected_requests_still_count_against_the_window():
    clock = _FakeClock()
    limiter = RateLimiter(window_seconds=60.0, clock=clock)

    for _ in range(5):
        await limiter.check("key-a", limit=3)

    clock.advance(59.9)
    # Still well within the same window -- all 5 prior calls should count,
    # not just the first 3 that were "allowed".
    assert await limiter.check("key-a", limit=3) is False


class _FakeRedisClient:
    """Minimal stand-in for redis.asyncio.Redis: just enough of INCR/EXPIRE
    to exercise RedisRateLimiter without a real Redis server."""

    def __init__(self):
        self._counts: dict[str, int] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def incr(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        self.expire_calls.append((key, seconds))


class TestRedisRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_requests_up_to_limit(self):
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(client, window_seconds=60.0)
        results = [await limiter.check("key-a", limit=3) for _ in range(3)]
        assert results == [True, True, True]

    @pytest.mark.asyncio
    async def test_rejects_requests_over_limit(self):
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(client, window_seconds=60.0)
        for _ in range(3):
            await limiter.check("key-a", limit=3)
        assert await limiter.check("key-a", limit=3) is False

    @pytest.mark.asyncio
    async def test_sets_expiry_only_on_first_increment(self):
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(client, window_seconds=60.0)
        for _ in range(3):
            await limiter.check("key-a", limit=3)
        assert client.expire_calls == [("openbouncer:ratelimit:key-a", 60)]

    @pytest.mark.asyncio
    async def test_keys_are_tracked_independently(self):
        client = _FakeRedisClient()
        limiter = RedisRateLimiter(client, window_seconds=60.0)
        for _ in range(3):
            await limiter.check("key-a", limit=3)
        assert await limiter.check("key-a", limit=3) is False
        assert await limiter.check("key-b", limit=3) is True


class TestBuildRateLimiter:
    def test_uses_in_memory_limiter_when_redis_not_configured(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        limiter = _build_rate_limiter()
        assert isinstance(limiter, RateLimiter)

    def test_uses_redis_limiter_when_configured(self, monkeypatch):
        monkeypatch.setenv(REDIS_URL_ENV_VAR, "redis://localhost:6379/0")
        limiter = _build_rate_limiter()
        assert isinstance(limiter, RedisRateLimiter)
