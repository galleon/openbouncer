import pytest

from app.auth.usage import UsageTracker


@pytest.mark.asyncio
async def test_records_request_with_no_usage():
    tracker = UsageTracker()
    await tracker.record("key-a")
    stats = tracker.get("key-a")
    assert stats.requests == 1
    assert stats.prompt_tokens == 0
    assert stats.completion_tokens == 0
    assert stats.total_tokens == 0


@pytest.mark.asyncio
async def test_accumulates_usage_across_calls():
    tracker = UsageTracker()
    await tracker.record("key-a", prompt_tokens=10, completion_tokens=5, total_tokens=15)
    await tracker.record("key-a", prompt_tokens=3, completion_tokens=2, total_tokens=5)

    stats = tracker.get("key-a")
    assert stats.requests == 2
    assert stats.prompt_tokens == 13
    assert stats.completion_tokens == 7
    assert stats.total_tokens == 20


@pytest.mark.asyncio
async def test_keys_are_tracked_independently():
    tracker = UsageTracker()
    await tracker.record("key-a", total_tokens=100)
    await tracker.record("key-b", total_tokens=1)

    assert tracker.get("key-a").total_tokens == 100
    assert tracker.get("key-b").total_tokens == 1


def test_unknown_key_returns_zeroed_stats():
    tracker = UsageTracker()
    stats = tracker.get("never-seen")
    assert stats.requests == 0
    assert stats.total_tokens == 0
