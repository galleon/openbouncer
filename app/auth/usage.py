import asyncio
from dataclasses import dataclass
from functools import lru_cache


@dataclass
class UsageStats:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class UsageTracker:
    """Basic in-memory per-key usage accounting.

    Records token counts from whatever `usage` object a response ends up
    with (real upstream usage, or our own stub-computed counts when running
    without a real backend) -- "basic" and "when available" by design: this
    is not billing-grade metering, does not persist across restarts, and
    streaming responses currently only increment `requests` since we don't
    yet capture a final usage total for them.
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

    def get(self, key_id: str) -> UsageStats:
        return self._stats.get(key_id, UsageStats())

    def all(self) -> dict[str, UsageStats]:
        """Every key with at least one recorded request -- used by
        /metrics to export per-key usage as Prometheus gauges (see
        app/api/routes/metrics.py). A plain dict copy, not a live view, so
        callers can't accidentally mutate internal state."""
        return dict(self._stats)


@lru_cache
def get_usage_tracker() -> UsageTracker:
    return UsageTracker()
