"""A named async lock, in-process by default, Redis-coordinated (real
cross-replica mutual exclusion) when REDIS_URL is set -- the same
in-memory/Redis dual-implementation convention as every other tracker in
this codebase (app.auth.rate_limiter, app.auth.usage, app.auth.budget,
app.auth.alerting), applied here to a lock primitive instead of a counter.

What this protects: every admin config write (app.auth.keys,
app.guardrails.prompt_injection, app.guardrails.output_leak,
app.guardrails.editable_config) and every append to the two hash-chained
logs (app.core.audit, app.core.guardrail_events) previously serialized
concurrent writes with a bare `asyncio.Lock()` -- real protection within
one process, none at all across two gateway replicas writing the same
shared file at once (see the README's "Multi-replica deployments"
section). admin_write_lock(name) replaces that bare lock with one that
actually coordinates when REDIS_URL is configured; without it, this
degrades to exactly the same process-local asyncio.Lock as before, so a
single-replica deployment's behavior doesn't change at all.

Fails CLOSED, unlike this codebase's other Redis-backed trackers. Rate
limiting/usage/budget/alerting all fail *open* on a Redis error (see each
of their docstrings) because they guard against abuse -- availability
matters more than strict enforcement during a Redis outage. A write lock
is the opposite trade-off: an admin write racing unprotected is a silent
lost update (or, for the hash-chained logs, a false "tampering" alarm from
GET /api/admin/audit-log/verify), not a rare inconvenience. Admin writes
are also infrequent and human-initiated, so refusing to persist during a
genuine Redis outage (AdminWriteLockUnavailableError, which route handlers
turn into a 503) and letting the admin retry is the safer failure mode.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

import redis.asyncio as redis_asyncio
from redis.exceptions import LockError, RedisError

from app.auth.rate_limiter import REDIS_URL_ENV_VAR

logger = logging.getLogger(__name__)

REDIS_KEY_PREFIX = "openbouncer:adminlock:"

# Both the Redis lock's own auto-expiry (so a replica that crashes mid-write
# doesn't wedge the lock forever) and the blocking_timeout for acquisition
# (so a request waiting on a slow write doesn't hang indefinitely). Admin
# writes are small, rare, human-initiated actions -- 10s is generous
# headroom, not a tight budget.
_LOCK_TIMEOUT_SECONDS = 10.0

# One asyncio.Lock per name, for the no-REDIS_URL case -- created lazily and
# reused, same "dict access has no await in between, so it's safe without
# its own lock" reasoning already used throughout this codebase (see e.g.
# app.core.audit's module docstring).
_in_process_locks: dict[str, asyncio.Lock] = {}


class AdminWriteLockUnavailableError(RuntimeError):
    """Raised when REDIS_URL is configured but the distributed lock can't
    be acquired -- Redis unreachable, or another replica held it past
    _LOCK_TIMEOUT_SECONDS. Route handlers should turn this into a 503, not
    proceed with an unprotected write."""


@lru_cache
def _redis_client() -> redis_asyncio.Redis | None:
    redis_url = os.environ.get(REDIS_URL_ENV_VAR)
    if not redis_url:
        return None
    return redis_asyncio.from_url(redis_url, decode_responses=True)


@asynccontextmanager
async def admin_write_lock(name: str) -> AsyncIterator[None]:
    """Acquires the named lock for the duration of the `async with` block.
    In-process asyncio.Lock if REDIS_URL isn't set; a real Redis-backed
    distributed lock (redis.asyncio's own SET-NX-based Lock, safe release
    via its built-in token check) if it is.
    """
    client = _redis_client()
    if client is None:
        lock = _in_process_locks.setdefault(name, asyncio.Lock())
        async with lock:
            yield
        return

    redis_lock = client.lock(
        f"{REDIS_KEY_PREFIX}{name}",
        timeout=_LOCK_TIMEOUT_SECONDS,
        blocking_timeout=_LOCK_TIMEOUT_SECONDS,
    )
    try:
        acquired = await redis_lock.acquire()
    except RedisError as exc:
        raise AdminWriteLockUnavailableError(
            f"Could not acquire the distributed write lock {name!r}: {exc}"
        ) from exc
    if not acquired:
        raise AdminWriteLockUnavailableError(
            f"Timed out acquiring the distributed write lock {name!r} after "
            f"{_LOCK_TIMEOUT_SECONDS}s -- another replica is holding it."
        )

    try:
        yield
    finally:
        # A release failure here doesn't mean the write was lost -- it
        # already happened (or the caller's own exception is already
        # propagating past us). It just means this lock will sit until its
        # own `timeout` expires and self-heals. Log, don't raise: raising
        # here would misreport a successful write as a failed one.
        try:
            await redis_lock.release()
        except (RedisError, LockError) as exc:
            logger.warning("Failed to release admin write lock %r: %s", name, exc)
