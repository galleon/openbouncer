"""Burst-block alerting: turns a run of guardrail blocks from one key into
a pushed webhook notification, instead of requiring someone to go look at
the Activity dashboard or the guardrail event log
(app.core.guardrail_events) after the fact.

Deployment config, not per-request policy -- env-var driven (like
REDIS_URL/PROMETHEUS_URL), not an admin-API-editable YAML like
app.guardrails.prompt_injection/output_leak, since "where to send ops
alerts" is an operator/infrastructure decision, not a guardrail policy a
key's traffic should be evaluated against. Unset OPENBOUNCER_ALERT_WEBHOOK_URL
(the default) disables the feature entirely -- same "powerful new feature
defaults off" posture as GUARDRAILS_MODE/the guardrails YAMLs.

Structurally this is a burst counter (AlertTracker/RedisAlertTracker below,
same in-memory-vs-Redis split as app.auth.rate_limiter/budget) plus a
fire-and-forget webhook POST (send_alert) -- callers (app/api/routes/chat.py)
call record_block() once per *request* that resolves to BLOCK (not once per
matched category -- see that module for why), and only call send_alert() if
a decision comes back. record_block() itself does both the counting and the
"should this actually notify" decision (threshold crossed, not already in
a cooldown for this key) in one call, mirroring
app.auth.rate_limiter.RateLimiter.check()'s "record-then-decide" shape.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Protocol

import httpx
import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from app.auth.rate_limiter import REDIS_URL_ENV_VAR
from app.core.metrics import ALERT_WEBHOOK_FAILURES_TOTAL, ALERTS_TRIGGERED_TOTAL

logger = logging.getLogger(__name__)

WEBHOOK_URL_ENV_VAR = "OPENBOUNCER_ALERT_WEBHOOK_URL"
BLOCK_THRESHOLD_ENV_VAR = "OPENBOUNCER_ALERT_BLOCK_THRESHOLD"
WINDOW_SECONDS_ENV_VAR = "OPENBOUNCER_ALERT_WINDOW_SECONDS"
COOLDOWN_SECONDS_ENV_VAR = "OPENBOUNCER_ALERT_COOLDOWN_SECONDS"

DEFAULT_BLOCK_THRESHOLD = 5
DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_COOLDOWN_SECONDS = 1800.0

REDIS_KEY_PREFIX = "openbouncer:alert:"

_WEBHOOK_TIMEOUT_SECONDS = 5.0


def _int_env(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _block_threshold() -> int:
    return _int_env(BLOCK_THRESHOLD_ENV_VAR, DEFAULT_BLOCK_THRESHOLD)


def _window_seconds() -> float:
    return _float_env(WINDOW_SECONDS_ENV_VAR, DEFAULT_WINDOW_SECONDS)


def _cooldown_seconds() -> float:
    return _float_env(COOLDOWN_SECONDS_ENV_VAR, DEFAULT_COOLDOWN_SECONDS)


def is_configured() -> bool:
    """Whether alerting is enabled at all -- callers (app/api/routes/chat.py)
    check this before even calling record_block(), so tracking a burst
    counter per block is zero-overhead when the feature isn't in use, same
    "off by default" discipline as the guardrails' own `enabled` flags."""
    return bool(os.environ.get(WEBHOOK_URL_ENV_VAR))


@dataclass(frozen=True)
class AlertDecision:
    key_id: str
    block_count: int
    window_seconds: float
    # guardrail ("prompt_injection" | "output_leak") -> count within the
    # window. A single request can only ever hit one of the two block
    # paths (prompt-injection blocks pre-generation, so output-leak's own
    # check never runs for that request), so these counts never double-count
    # one request under both guardrails.
    guardrail_counts: dict[str, int]


class SupportsAlertTracking(Protocol):
    async def record_block(self, key_id: str, guardrail: str) -> AlertDecision | None: ...


class AlertTracker:
    """Basic in-memory burst-block counter with cooldown-gated alerting.

    MVP-level, same posture as app.auth.rate_limiter.RateLimiter: a single
    process-wide counter per key, doesn't coordinate across replicas -- use
    RedisAlertTracker (enabled via REDIS_URL, see get_alert_tracker()) for
    that, which is also where cross-replica double-alerting is actually
    guarded against (see that class's docstring).

    The block-count window and the cooldown are independent: the window
    keeps rolling on its own fixed cadence regardless of whether an alert
    fires, and the cooldown only gates *re-notification* -- so if blocks
    continue past the cooldown, the next one that crosses the threshold in
    whatever the *current* window is fires a fresh alert, not a stale count
    carried over from before.
    """

    def __init__(
        self,
        *,
        threshold: int | None = None,
        window_seconds: float | None = None,
        cooldown_seconds: float | None = None,
        clock=time.monotonic,
    ) -> None:
        self._threshold = threshold if threshold is not None else _block_threshold()
        self._window_seconds = window_seconds if window_seconds is not None else _window_seconds()
        self._cooldown_seconds = cooldown_seconds if cooldown_seconds is not None else _cooldown_seconds()
        self._clock = clock
        self._buckets: dict[str, tuple[float, dict[str, int]]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def record_block(self, key_id: str, guardrail: str) -> AlertDecision | None:
        async with self._lock:
            now = self._clock()
            window_start, counts = self._buckets.get(key_id, (now, {}))
            if now - window_start >= self._window_seconds:
                window_start, counts = now, {}
            counts = dict(counts)
            counts[guardrail] = counts.get(guardrail, 0) + 1
            self._buckets[key_id] = (window_start, counts)

            total = sum(counts.values())
            if total < self._threshold:
                return None

            if now < self._cooldown_until.get(key_id, 0.0):
                return None  # already alerted recently for this key -- suppress

            self._cooldown_until[key_id] = now + self._cooldown_seconds
            return AlertDecision(
                key_id=key_id, block_count=total, window_seconds=self._window_seconds, guardrail_counts=counts
            )


class RedisAlertTracker:
    """Same burst-block counting as AlertTracker, backed by Redis, so
    multiple gateway replicas share one budget and one cooldown per key
    instead of each enforcing its own -- same reasoning and REDIS_URL
    trigger as RedisRateLimiter/RedisBudgetTracker.

    The cooldown gate specifically needs to be atomic across replicas:
    two replicas both reading "not in cooldown" at the same instant and
    both firing would double the alert. `SET key value NX EX seconds`
    (set-if-not-exists-with-expiry) makes exactly one replica's SET
    succeed, so only that one gets to return a decision -- a plain
    GET-then-SET would race.
    """

    def __init__(self, client: redis_asyncio.Redis) -> None:
        self._client = client
        self._threshold = _block_threshold()
        self._window_seconds = int(_window_seconds())
        self._cooldown_seconds = int(_cooldown_seconds())

    def _window_key(self, key_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}window:{key_id}"

    def _cooldown_key(self, key_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}cooldown:{key_id}"

    async def record_block(self, key_id: str, guardrail: str) -> AlertDecision | None:
        window_key = self._window_key(key_id)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.hincrby(window_key, guardrail, 1)
                pipe.expire(window_key, self._window_seconds)
                await pipe.execute()
            counts_raw = await self._client.hgetall(window_key)
        except RedisError as exc:
            # Fail open, same reasoning as RedisRateLimiter.check: this
            # can run on the request path (via app.api.routes.chat), so an
            # unreachable Redis must not turn every request into a 500.
            # Burst detection is a best-effort ops signal, not a security
            # boundary -- silently skipping it during an outage (rather
            # than, say, alerting on every single block) is the
            # deliberate choice here.
            logger.warning("Redis alert tracker unavailable, skipping burst detection: %s", exc)
            return None

        counts = {k: int(v) for k, v in counts_raw.items()}
        total = sum(counts.values())
        if total < self._threshold:
            return None

        try:
            won = await self._client.set(
                self._cooldown_key(key_id), "1", nx=True, ex=self._cooldown_seconds
            )
        except RedisError as exc:
            logger.warning("Redis alert tracker unavailable, skipping burst detection: %s", exc)
            return None
        if not won:
            return None  # another replica already claimed this alert, or we're still in cooldown

        return AlertDecision(
            key_id=key_id, block_count=total, window_seconds=float(self._window_seconds), guardrail_counts=counts
        )


def _build_alert_tracker() -> SupportsAlertTracking:
    redis_url = os.environ.get(REDIS_URL_ENV_VAR)
    if not redis_url:
        return AlertTracker()
    client = redis_asyncio.from_url(redis_url, decode_responses=True)
    return RedisAlertTracker(client)


@lru_cache
def get_alert_tracker() -> SupportsAlertTracking:
    return _build_alert_tracker()


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------

# Holds references to in-flight fire-and-forget delivery tasks so they
# aren't garbage-collected mid-flight (a bare `asyncio.create_task(...)`
# with no other reference is only weakly held by the event loop) -- each
# task removes itself via its own done-callback once it finishes.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _drain_background_tasks() -> None:
    """Test-only: waits for any in-flight fire-and-forget deliveries to
    finish, so a test can assert on their side effects deterministically
    instead of racing a sleep. Not used by application code -- the whole
    point of send_alert() is that callers on the request path do *not*
    wait for this."""
    pending = list(_background_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _alert_payload(decision: AlertDecision) -> dict:
    breakdown = ", ".join(f"{g}: {c}" for g, c in sorted(decision.guardrail_counts.items()))
    text = (
        f'OpenBouncer: key "{decision.key_id}" triggered {decision.block_count} blocks '
        f"in {int(decision.window_seconds)}s ({breakdown})"
    )
    return {
        # A plain "text" field renders directly in a Slack incoming
        # webhook with no Slack-specific code on our side; the structured
        # fields below are for any other receiver. Deliberately no match
        # snippets/content here -- see the module docstring and
        # app.core.guardrail_events' own "Snippet privacy" note. This
        # pushes only counts to a third-party-configured endpoint; the
        # actual matched content stays behind the authenticated
        # /api/admin/guardrail-events endpoint.
        "text": text,
        "key_id": decision.key_id,
        "block_count": decision.block_count,
        "window_seconds": decision.window_seconds,
        "guardrails": decision.guardrail_counts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _post_webhook(url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=_WEBHOOK_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        ALERT_WEBHOOK_FAILURES_TOTAL.inc()
        logger.warning("Alert webhook delivery failed: %s", exc)
        return
    if response.status_code >= 400:
        ALERT_WEBHOOK_FAILURES_TOTAL.inc()
        logger.warning("Alert webhook returned %s: %s", response.status_code, response.text[:300])


async def send_alert(decision: AlertDecision, *, guardrail: str) -> None:
    """Fire-and-forget delivery of a triggered alert -- called from the
    request path right after AlertTracker.record_block() returns a
    decision. Must never block or fail the caller's own response: the
    actual network POST runs as an independent background task (see
    _fire_and_forget), not awaited here. Single best-effort attempt, no
    retries -- same fail-open posture applied to every other Redis-backed
    tracker in this codebase, now extended to webhook delivery too.
    """
    url = os.environ.get(WEBHOOK_URL_ENV_VAR)
    if not url:
        return
    ALERTS_TRIGGERED_TOTAL.labels(guardrail=guardrail).inc()
    _fire_and_forget(_post_webhook(url, _alert_payload(decision)))


@dataclass(frozen=True)
class WebhookTestResult:
    configured: bool
    delivered: bool
    status_code: int | None
    error: str | None


async def send_test_alert() -> WebhookTestResult:
    """Synchronous (awaited, not fire-and-forget) delivery of a clearly-
    labeled test payload to the configured webhook -- unlike send_alert()
    above, the caller here (POST /api/admin/alerts/test) wants to know the
    outcome before responding, so an operator can verify their webhook URL
    actually works instead of finding out it was misconfigured only when
    a real burst happens."""
    url = os.environ.get(WEBHOOK_URL_ENV_VAR)
    if not url:
        return WebhookTestResult(configured=False, delivered=False, status_code=None, error=None)

    payload = {
        "text": (
            "OpenBouncer: this is a test alert (POST /api/admin/alerts/test) -- "
            "not a real burst-block event."
        ),
        "key_id": "test",
        "block_count": 0,
        "window_seconds": 0,
        "guardrails": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=_WEBHOOK_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return WebhookTestResult(configured=True, delivered=False, status_code=None, error=str(exc)[:300])

    if response.status_code >= 400:
        return WebhookTestResult(
            configured=True, delivered=False, status_code=response.status_code, error=response.text[:300]
        )
    return WebhookTestResult(configured=True, delivered=True, status_code=response.status_code, error=None)
