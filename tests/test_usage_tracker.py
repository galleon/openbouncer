import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth.usage import REDIS_URL_ENV_VAR, RedisUsageTracker, UsageTracker, _build_usage_tracker


@pytest.mark.asyncio
async def test_records_request_with_no_usage():
    tracker = UsageTracker()
    await tracker.record("key-a")
    stats = await tracker.get("key-a")
    assert stats.requests == 1
    assert stats.prompt_tokens == 0
    assert stats.completion_tokens == 0
    assert stats.total_tokens == 0


@pytest.mark.asyncio
async def test_accumulates_usage_across_calls():
    tracker = UsageTracker()
    await tracker.record("key-a", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    await tracker.record("key-a", prompt_tokens=3, completion_tokens=2, total_tokens=5)

    stats = await tracker.get("key-a")
    assert stats.requests == 2
    assert stats.prompt_tokens == 13
    assert stats.completion_tokens == 7
    assert stats.total_tokens == 20


@pytest.mark.asyncio
async def test_keys_are_tracked_independently():
    tracker = UsageTracker()
    await tracker.record("key-a", total_tokens=100)
    await tracker.record("key-b", total_tokens=1)

    assert (await tracker.get("key-a")).total_tokens == 100
    assert (await tracker.get("key-b")).total_tokens == 1


@pytest.mark.asyncio
async def test_unknown_key_returns_zeroed_stats():
    tracker = UsageTracker()
    stats = await tracker.get("never-seen")
    assert stats.requests == 0
    assert stats.total_tokens == 0


@pytest.mark.asyncio
async def test_all_returns_every_recorded_key():
    tracker = UsageTracker()
    await tracker.record("key-a", total_tokens=1)
    await tracker.record("key-b", total_tokens=2)

    all_stats = await tracker.all()
    assert set(all_stats) == {"key-a", "key-b"}
    assert all_stats["key-a"].total_tokens == 1
    assert all_stats["key-b"].total_tokens == 2


class _FakeRedisPipeline:
    """Minimal stand-in for a redis.asyncio pipeline: just enough of
    HINCRBY (queued) + EXECUTE to exercise RedisUsageTracker.record()
    without a real Redis server."""

    def __init__(self, client):
        self._client = client
        self._ops: list[tuple[str, str, int]] = []

    def hincrby(self, key: str, field: str, amount: int):
        self._ops.append((key, field, amount))
        return self

    async def execute(self) -> None:
        for key, field, amount in self._ops:
            hash_ = self._client.hashes.setdefault(key, {})
            hash_[field] = str(int(hash_.get(field, "0")) + amount)
        self._ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeRedisClient:
    """Minimal stand-in for redis.asyncio.Redis: just enough of
    pipeline/HINCRBY, HGETALL, and SCAN to exercise RedisUsageTracker
    without a real Redis server -- same idea as test_rate_limiter.py's
    _FakeRedisClient."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self, transaction: bool = True):
        return _FakeRedisPipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def scan_iter(self, match: str | None = None):
        prefix = match[:-1] if match and match.endswith("*") else match
        for key in list(self.hashes):
            if prefix is None or key.startswith(prefix):
                yield key


class TestRedisUsageTracker:
    @pytest.mark.asyncio
    async def test_records_request_with_no_usage(self):
        client = _FakeRedisClient()
        tracker = RedisUsageTracker(client)
        await tracker.record("key-a")
        stats = await tracker.get("key-a")
        assert stats.requests == 1
        assert stats.total_tokens == 0

    @pytest.mark.asyncio
    async def test_accumulates_usage_across_calls(self):
        client = _FakeRedisClient()
        tracker = RedisUsageTracker(client)
        await tracker.record("key-a", prompt_tokens=10, completion_tokens=5, total_tokens=15)
        await tracker.record("key-a", prompt_tokens=3, completion_tokens=2, total_tokens=5)

        stats = await tracker.get("key-a")
        assert stats.requests == 2
        assert stats.prompt_tokens == 13
        assert stats.completion_tokens == 7
        assert stats.total_tokens == 20

    @pytest.mark.asyncio
    async def test_keys_are_tracked_independently(self):
        client = _FakeRedisClient()
        tracker = RedisUsageTracker(client)
        await tracker.record("key-a", total_tokens=100)
        await tracker.record("key-b", total_tokens=1)

        assert (await tracker.get("key-a")).total_tokens == 100
        assert (await tracker.get("key-b")).total_tokens == 1

    @pytest.mark.asyncio
    async def test_unknown_key_returns_zeroed_stats(self):
        client = _FakeRedisClient()
        tracker = RedisUsageTracker(client)
        stats = await tracker.get("never-seen")
        assert stats.requests == 0
        assert stats.total_tokens == 0

    @pytest.mark.asyncio
    async def test_all_returns_every_recorded_key_stripped_of_prefix(self):
        client = _FakeRedisClient()
        tracker = RedisUsageTracker(client)
        await tracker.record("key-a", total_tokens=1)
        await tracker.record("key-b", total_tokens=2)

        all_stats = await tracker.all()
        assert set(all_stats) == {"key-a", "key-b"}
        assert all_stats["key-a"].total_tokens == 1
        assert all_stats["key-b"].total_tokens == 2

    @pytest.mark.asyncio
    async def test_all_ignores_unrelated_redis_keys(self):
        client = _FakeRedisClient()
        client.hashes["openbouncer:ratelimit:key-a"] = {"count": "5"}
        tracker = RedisUsageTracker(client)
        await tracker.record("key-a", total_tokens=1)

        all_stats = await tracker.all()
        assert set(all_stats) == {"key-a"}


class _BrokenRedisPipeline:
    def hincrby(self, key, field, amount):
        return self

    async def execute(self):
        raise RedisConnectionError("connection refused")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _BrokenRedisClient:
    """Stands in for a Redis client that can't reach the server at all --
    every call raises, same as a real redis.asyncio.Redis would on a
    connection failure."""

    def pipeline(self, transaction: bool = True):
        return _BrokenRedisPipeline()

    async def hgetall(self, key: str):
        raise RedisConnectionError("connection refused")

    async def scan_iter(self, match: str | None = None):
        raise RedisConnectionError("connection refused")
        yield  # pragma: no cover -- makes this an async generator


class TestRedisUsageTrackerDegradesGracefully:
    """A request that already succeeded (the upstream model already
    answered, or the stream already finished) must not turn into a 500
    just because the accounting write failed at the very end -- see
    RedisUsageTracker.record's docstring."""

    @pytest.mark.asyncio
    async def test_record_does_not_raise_when_redis_unreachable(self):
        tracker = RedisUsageTracker(_BrokenRedisClient())
        await tracker.record("key-a", total_tokens=5)  # must not raise

    @pytest.mark.asyncio
    async def test_get_returns_zeroed_stats_when_redis_unreachable(self):
        tracker = RedisUsageTracker(_BrokenRedisClient())
        stats = await tracker.get("key-a")
        assert stats.requests == 0
        assert stats.total_tokens == 0

    @pytest.mark.asyncio
    async def test_all_returns_empty_when_redis_unreachable(self):
        tracker = RedisUsageTracker(_BrokenRedisClient())
        assert await tracker.all() == {}


class TestBuildUsageTracker:
    def test_uses_in_memory_tracker_when_redis_not_configured(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        tracker = _build_usage_tracker()
        assert isinstance(tracker, UsageTracker)

    def test_uses_redis_tracker_when_configured(self, monkeypatch):
        monkeypatch.setenv(REDIS_URL_ENV_VAR, "redis://localhost:6379/0")
        tracker = _build_usage_tracker()
        assert isinstance(tracker, RedisUsageTracker)
