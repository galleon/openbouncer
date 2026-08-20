from redis.exceptions import ConnectionError as RedisConnectionError

import pytest

from app.auth.alerting import (
    REDIS_URL_ENV_VAR,
    AlertTracker,
    RedisAlertTracker,
    _build_alert_tracker,
    is_configured,
)


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(**overrides) -> AlertTracker:
    defaults = dict(threshold=3, window_seconds=60.0, cooldown_seconds=300.0, clock=_FakeClock())
    defaults.update(overrides)
    return AlertTracker(**defaults)


class TestIsConfigured:
    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("OPENBOUNCER_ALERT_WEBHOOK_URL", raising=False)
        assert is_configured() is False

    def test_true_when_set(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_WEBHOOK_URL", "https://example.com/hook")
        assert is_configured() is True


class TestAlertTracker:
    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self):
        tracker = _tracker(threshold=3)
        assert await tracker.record_block("key-a", "prompt_injection") is None
        assert await tracker.record_block("key-a", "prompt_injection") is None

    @pytest.mark.asyncio
    async def test_reaching_threshold_returns_a_decision(self):
        tracker = _tracker(threshold=3)
        await tracker.record_block("key-a", "prompt_injection")
        await tracker.record_block("key-a", "prompt_injection")
        decision = await tracker.record_block("key-a", "prompt_injection")
        assert decision is not None
        assert decision.key_id == "key-a"
        assert decision.block_count == 3
        assert decision.guardrail_counts == {"prompt_injection": 3}

    @pytest.mark.asyncio
    async def test_combines_counts_across_guardrails(self):
        tracker = _tracker(threshold=3)
        await tracker.record_block("key-a", "prompt_injection")
        await tracker.record_block("key-a", "output_leak")
        decision = await tracker.record_block("key-a", "prompt_injection")
        assert decision is not None
        assert decision.block_count == 3
        assert decision.guardrail_counts == {"prompt_injection": 2, "output_leak": 1}

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeat_alerts(self):
        tracker = _tracker(threshold=3, cooldown_seconds=300.0)
        await tracker.record_block("key-a", "prompt_injection")
        await tracker.record_block("key-a", "prompt_injection")
        first = await tracker.record_block("key-a", "prompt_injection")
        assert first is not None

        # A 4th block, still well within the cooldown -- must not re-alert.
        second = await tracker.record_block("key-a", "prompt_injection")
        assert second is None

    @pytest.mark.asyncio
    async def test_re_alerts_after_cooldown_expires(self):
        clock = _FakeClock()
        tracker = _tracker(threshold=3, window_seconds=600.0, cooldown_seconds=100.0, clock=clock)
        for _ in range(3):
            await tracker.record_block("key-a", "prompt_injection")

        clock.advance(150.0)  # past cooldown, still inside the 600s window
        decision = await tracker.record_block("key-a", "prompt_injection")
        assert decision is not None
        # Window never reset (still open), so this reflects the full
        # accumulated count, not just the one new block.
        assert decision.block_count == 4

    @pytest.mark.asyncio
    async def test_window_resets_independently_of_cooldown(self):
        clock = _FakeClock()
        tracker = _tracker(threshold=3, window_seconds=60.0, cooldown_seconds=10_000.0, clock=clock)
        for _ in range(3):
            await tracker.record_block("key-a", "prompt_injection")

        clock.advance(120.0)  # window rolls over, cooldown (10000s) still active
        # Fresh window: only 2 blocks so far, below threshold -- even
        # though we're still deep in the prior alert's cooldown.
        assert await tracker.record_block("key-a", "prompt_injection") is None
        assert await tracker.record_block("key-a", "prompt_injection") is None
        # Still suppressed by cooldown despite crossing the threshold
        # again in the new window.
        assert await tracker.record_block("key-a", "prompt_injection") is None

    @pytest.mark.asyncio
    async def test_keys_are_tracked_independently(self):
        tracker = _tracker(threshold=3)
        for _ in range(3):
            await tracker.record_block("key-a", "prompt_injection")
        assert await tracker.record_block("key-b", "prompt_injection") is None


class _FakeRedisPipeline:
    def __init__(self, client):
        self._client = client
        self._ops: list[tuple] = []

    def hincrby(self, key: str, field: str, amount: int):
        self._ops.append(("hincrby", key, field, amount))
        return self

    def expire(self, key: str, seconds: int):
        self._ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> None:
        for op in self._ops:
            if op[0] == "hincrby":
                _, key, field, amount = op
                hash_ = self._client.hashes.setdefault(key, {})
                hash_[field] = str(int(hash_.get(field, "0")) + amount)
        self._ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeRedisClient:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}

    def pipeline(self, transaction: bool = True):
        return _FakeRedisPipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True


class TestRedisAlertTracker:
    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_BLOCK_THRESHOLD", "3")
        client = _FakeRedisClient()
        tracker = RedisAlertTracker(client)
        assert await tracker.record_block("key-a", "prompt_injection") is None

    @pytest.mark.asyncio
    async def test_reaching_threshold_returns_a_decision(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_BLOCK_THRESHOLD", "3")
        client = _FakeRedisClient()
        tracker = RedisAlertTracker(client)
        await tracker.record_block("key-a", "prompt_injection")
        await tracker.record_block("key-a", "prompt_injection")
        decision = await tracker.record_block("key-a", "prompt_injection")
        assert decision is not None
        assert decision.block_count == 3

    @pytest.mark.asyncio
    async def test_second_replica_does_not_double_alert(self, monkeypatch):
        # Two RedisAlertTracker instances sharing the same backing client
        # simulate two gateway replicas -- only one may win the atomic
        # SET NX cooldown claim.
        monkeypatch.setenv("OPENBOUNCER_ALERT_BLOCK_THRESHOLD", "1")
        client = _FakeRedisClient()
        replica_a = RedisAlertTracker(client)
        replica_b = RedisAlertTracker(client)

        first = await replica_a.record_block("key-a", "prompt_injection")
        second = await replica_b.record_block("key-a", "prompt_injection")
        assert first is not None
        assert second is None

    @pytest.mark.asyncio
    async def test_keys_are_tracked_independently(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_BLOCK_THRESHOLD", "1")
        client = _FakeRedisClient()
        tracker = RedisAlertTracker(client)
        assert await tracker.record_block("key-a", "prompt_injection") is not None
        assert await tracker.record_block("key-b", "prompt_injection") is not None


class _BrokenRedisClient:
    def pipeline(self, transaction: bool = True):
        raise RedisConnectionError("connection refused")

    async def hgetall(self, key: str):
        raise RedisConnectionError("connection refused")

    async def set(self, *args, **kwargs):
        raise RedisConnectionError("connection refused")


class TestRedisAlertTrackerFailsOpen:
    @pytest.mark.asyncio
    async def test_record_block_returns_none_when_redis_unreachable(self):
        tracker = RedisAlertTracker(_BrokenRedisClient())
        # Fails open by skipping burst detection entirely (never alerting)
        # rather than raising -- this can run on the request path (see
        # app.api.routes.chat), so an unreachable Redis must not turn a
        # blocked request into a 500.
        assert await tracker.record_block("key-a", "prompt_injection") is None


class TestBuildAlertTracker:
    def test_uses_in_memory_tracker_when_redis_not_configured(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        assert isinstance(_build_alert_tracker(), AlertTracker)

    def test_uses_redis_tracker_when_configured(self, monkeypatch):
        monkeypatch.setenv(REDIS_URL_ENV_VAR, "redis://localhost:6379/0")
        assert isinstance(_build_alert_tracker(), RedisAlertTracker)
