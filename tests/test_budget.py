from datetime import datetime, timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth.budget import (
    REDIS_URL_ENV_VAR,
    BudgetTracker,
    RedisBudgetTracker,
    _build_budget_tracker,
)


class _FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _clock(start: str = "2026-08-19T10:00:00+00:00") -> _FakeClock:
    return _FakeClock(datetime.fromisoformat(start))


class TestBudgetTrackerUnlimited:
    @pytest.mark.asyncio
    async def test_no_limits_configured_is_always_within_budget(self):
        tracker = BudgetTracker(clock=_clock())
        await tracker.record("key-a", 1_000_000)
        assert await tracker.check("key-a", daily_limit=None, monthly_limit=None) is True


class TestBudgetTrackerDaily:
    @pytest.mark.asyncio
    async def test_within_budget_below_limit(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 500)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is True

    @pytest.mark.asyncio
    async def test_exceeded_when_usage_reaches_limit(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 1000)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is False

    @pytest.mark.asyncio
    async def test_exceeded_when_usage_goes_over_limit(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 600)
        await tracker.record("key-a", 600)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is False

    @pytest.mark.asyncio
    async def test_resets_on_the_next_calendar_day(self):
        clock = _clock("2026-08-19T23:59:00+00:00")
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 1000)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is False

        clock.advance(minutes=2)  # rolls over to 2026-08-20
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is True

    @pytest.mark.asyncio
    async def test_keys_are_tracked_independently(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 1000)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is False
        assert await tracker.check("key-b", daily_limit=1000, monthly_limit=None) is True

    @pytest.mark.asyncio
    async def test_zero_or_negative_tokens_are_not_recorded(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 0)
        await tracker.record("key-a", -5)
        assert await tracker.check("key-a", daily_limit=1, monthly_limit=None) is True


class TestBudgetTrackerMonthly:
    @pytest.mark.asyncio
    async def test_exceeded_when_usage_reaches_limit(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 5000)
        assert await tracker.check("key-a", daily_limit=None, monthly_limit=5000) is False

    @pytest.mark.asyncio
    async def test_resets_on_the_next_calendar_month(self):
        clock = _clock("2026-08-31T23:59:00+00:00")
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 5000)
        assert await tracker.check("key-a", daily_limit=None, monthly_limit=5000) is False

        clock.advance(minutes=2)  # rolls over to 2026-09-01
        assert await tracker.check("key-a", daily_limit=None, monthly_limit=5000) is True

    @pytest.mark.asyncio
    async def test_daily_reset_does_not_reset_monthly(self):
        clock = _clock("2026-08-19T23:59:00+00:00")
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 5000)
        clock.advance(minutes=2)  # next day, same month
        assert await tracker.check("key-a", daily_limit=None, monthly_limit=5000) is False


class TestBudgetTrackerBothLimits:
    @pytest.mark.asyncio
    async def test_either_limit_exceeded_fails_the_check(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 100)
        # Daily limit already hit, monthly limit still has headroom.
        assert await tracker.check("key-a", daily_limit=100, monthly_limit=1_000_000) is False

    @pytest.mark.asyncio
    async def test_both_within_limits_passes(self):
        clock = _clock()
        tracker = BudgetTracker(clock=clock)
        await tracker.record("key-a", 100)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=10_000) is True


class _FakeRedisPipeline:
    """Minimal stand-in for a redis.asyncio pipeline: just enough of
    INCRBY/EXPIRE (queued) + EXECUTE to exercise RedisBudgetTracker.record()
    without a real Redis server -- same idea as test_usage_tracker.py's
    _FakeRedisPipeline."""

    def __init__(self, client):
        self._client = client
        self._ops: list[tuple] = []

    def incrby(self, key: str, amount: int):
        self._ops.append(("incrby", key, amount))
        return self

    def expire(self, key: str, seconds: int):
        self._ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> None:
        for op, key, value in self._ops:
            if op == "incrby":
                self._client.values[key] = str(int(self._client.values.get(key, "0")) + value)
            else:
                self._client.expire_calls.append((key, value))
        self._ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeRedisClient:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.expire_calls: list[tuple[str, int]] = []

    def pipeline(self, transaction: bool = True):
        return _FakeRedisPipeline(self)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class TestRedisBudgetTracker:
    @pytest.mark.asyncio
    async def test_within_budget_below_limit(self):
        client = _FakeRedisClient()
        tracker = RedisBudgetTracker(client)
        await tracker.record("key-a", 500)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is True

    @pytest.mark.asyncio
    async def test_exceeded_when_usage_reaches_limit(self):
        client = _FakeRedisClient()
        tracker = RedisBudgetTracker(client)
        await tracker.record("key-a", 1000)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is False

    @pytest.mark.asyncio
    async def test_records_to_both_daily_and_monthly_keys(self):
        client = _FakeRedisClient()
        tracker = RedisBudgetTracker(client)
        await tracker.record("key-a", 500)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is True
        assert await tracker.check("key-a", daily_limit=None, monthly_limit=1000) is True
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=1000) is True

    @pytest.mark.asyncio
    async def test_sets_expiry_on_both_keys(self):
        client = _FakeRedisClient()
        tracker = RedisBudgetTracker(client)
        await tracker.record("key-a", 500)
        seconds_by_key = dict(client.expire_calls)
        assert len(seconds_by_key) == 2

    @pytest.mark.asyncio
    async def test_zero_tokens_not_recorded(self):
        client = _FakeRedisClient()
        tracker = RedisBudgetTracker(client)
        await tracker.record("key-a", 0)
        assert client.values == {}

    @pytest.mark.asyncio
    async def test_keys_are_tracked_independently(self):
        client = _FakeRedisClient()
        tracker = RedisBudgetTracker(client)
        await tracker.record("key-a", 1000)
        assert await tracker.check("key-a", daily_limit=1000, monthly_limit=None) is False
        assert await tracker.check("key-b", daily_limit=1000, monthly_limit=None) is True


class _BrokenRedisClient:
    """Every call raises, same as a real redis.asyncio.Redis would on a
    connection failure -- same idea as test_rate_limiter.py's
    _BrokenRedisClient."""

    def pipeline(self, transaction: bool = True):
        raise RedisConnectionError("connection refused")

    async def get(self, key: str) -> str | None:
        raise RedisConnectionError("connection refused")


class TestRedisBudgetTrackerFailsOpen:
    @pytest.mark.asyncio
    async def test_check_returns_true_when_redis_unreachable(self):
        tracker = RedisBudgetTracker(_BrokenRedisClient())
        assert await tracker.check("key-a", daily_limit=1, monthly_limit=None) is True

    @pytest.mark.asyncio
    async def test_record_does_not_raise_when_redis_unreachable(self):
        tracker = RedisBudgetTracker(_BrokenRedisClient())
        await tracker.record("key-a", 100)  # must not raise


class TestBuildBudgetTracker:
    def test_uses_in_memory_tracker_when_redis_not_configured(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        tracker = _build_budget_tracker()
        assert isinstance(tracker, BudgetTracker)

    def test_uses_redis_tracker_when_configured(self, monkeypatch):
        monkeypatch.setenv(REDIS_URL_ENV_VAR, "redis://localhost:6379/0")
        tracker = _build_budget_tracker()
        assert isinstance(tracker, RedisBudgetTracker)
