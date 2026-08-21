import asyncio

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import LockError

import app.core.distributed_lock as distributed_lock_module
from app.auth.rate_limiter import REDIS_URL_ENV_VAR
from app.core.distributed_lock import AdminWriteLockUnavailableError, _redis_client, admin_write_lock


@pytest.fixture(autouse=True)
def _clear_redis_client_cache():
    # _redis_client is @lru_cache'd on no arguments -- clear it so each
    # test's REDIS_URL_ENV_VAR state (set via monkeypatch) is actually
    # re-read, not served from a previous test's cached result.
    _redis_client.cache_clear()
    yield
    _redis_client.cache_clear()


class TestRedisClientSelection:
    def test_none_when_redis_url_not_set(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        assert _redis_client() is None

    def test_returns_a_client_when_redis_url_set(self, monkeypatch):
        monkeypatch.setenv(REDIS_URL_ENV_VAR, "redis://localhost:6379/0")
        assert _redis_client() is not None


class TestInProcessLock:
    @pytest.mark.asyncio
    async def test_allows_sequential_acquisition(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        async with admin_write_lock("some-resource"):
            pass
        async with admin_write_lock("some-resource"):
            pass

    @pytest.mark.asyncio
    async def test_serializes_concurrent_acquisitions(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        events: list[str] = []

        async def holder(label: str, hold_seconds: float) -> None:
            async with admin_write_lock("shared"):
                events.append(f"{label}-start")
                await asyncio.sleep(hold_seconds)
                events.append(f"{label}-end")

        # "a" holds the lock first and sleeps -- if the lock actually
        # serializes, "b" can't start until "a" has fully finished, so the
        # events interleave as start/end/start/end, never start/start/....
        await asyncio.gather(holder("a", 0.05), holder("b", 0.0))

        assert events == ["a-start", "a-end", "b-start", "b-end"]

    @pytest.mark.asyncio
    async def test_different_names_do_not_contend(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        events: list[str] = []

        async def holder(name: str, label: str, hold_seconds: float) -> None:
            async with admin_write_lock(name):
                events.append(f"{label}-start")
                await asyncio.sleep(hold_seconds)
                events.append(f"{label}-end")

        # Two different resource names -- "b" should be able to start
        # before "a" finishes, unlike the shared-name case above.
        await asyncio.gather(holder("resource-a", "a", 0.05), holder("resource-b", "b", 0.0))

        assert events[0] == "a-start"
        assert events[1] == "b-start"

    @pytest.mark.asyncio
    async def test_lock_released_on_exception(self, monkeypatch):
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)
        with pytest.raises(ValueError):
            async with admin_write_lock("some-resource"):
                raise ValueError("boom")

        # Didn't wedge the lock -- a fresh acquisition still succeeds.
        async with admin_write_lock("some-resource"):
            pass


class _FakeRedisLock:
    """Stands in for redis.asyncio.lock.Lock -- just the acquire()/release()
    surface admin_write_lock actually calls, controllable per test instead
    of needing a real Redis server."""

    def __init__(self, *, acquire_result=True, acquire_exception=None, release_exception=None):
        self._acquire_result = acquire_result
        self._acquire_exception = acquire_exception
        self._release_exception = release_exception
        self.released = False

    async def acquire(self):
        if self._acquire_exception is not None:
            raise self._acquire_exception
        return self._acquire_result

    async def release(self):
        self.released = True
        if self._release_exception is not None:
            raise self._release_exception


class _FakeRedisClientForLock:
    def __init__(self, lock: _FakeRedisLock):
        self._lock = lock

    def lock(self, name, *, timeout, blocking_timeout):
        return self._lock


class TestRedisBackedLock:
    @pytest.mark.asyncio
    async def test_acquires_and_releases_on_success(self, monkeypatch):
        fake_lock = _FakeRedisLock()
        monkeypatch.setattr(
            distributed_lock_module, "_redis_client", lambda: _FakeRedisClientForLock(fake_lock)
        )

        entered = False
        async with admin_write_lock("some-resource"):
            entered = True
        assert entered is True
        assert fake_lock.released is True

    @pytest.mark.asyncio
    async def test_raises_when_acquire_times_out(self, monkeypatch):
        fake_lock = _FakeRedisLock(acquire_result=False)
        monkeypatch.setattr(
            distributed_lock_module, "_redis_client", lambda: _FakeRedisClientForLock(fake_lock)
        )

        with pytest.raises(AdminWriteLockUnavailableError):
            async with admin_write_lock("some-resource"):
                pytest.fail("body must not run when the lock could not be acquired")

    @pytest.mark.asyncio
    async def test_raises_when_acquire_errors(self, monkeypatch):
        fake_lock = _FakeRedisLock(acquire_exception=RedisConnectionError("connection refused"))
        monkeypatch.setattr(
            distributed_lock_module, "_redis_client", lambda: _FakeRedisClientForLock(fake_lock)
        )

        with pytest.raises(AdminWriteLockUnavailableError):
            async with admin_write_lock("some-resource"):
                pytest.fail("body must not run when Redis is unreachable")

    @pytest.mark.asyncio
    async def test_release_failure_does_not_mask_a_successful_write(self, monkeypatch, caplog):
        fake_lock = _FakeRedisLock(release_exception=LockError("Cannot release an unlocked lock"))
        monkeypatch.setattr(
            distributed_lock_module, "_redis_client", lambda: _FakeRedisClientForLock(fake_lock)
        )

        result = "not set"
        async with admin_write_lock("some-resource"):
            result = "write happened"

        # The write's own result stands -- a release failure after a
        # successful write must not surface as an error to the caller.
        assert result == "write happened"

    @pytest.mark.asyncio
    async def test_release_failure_does_not_mask_the_bodys_own_exception(self, monkeypatch):
        fake_lock = _FakeRedisLock(release_exception=LockError("Cannot release an unlocked lock"))
        monkeypatch.setattr(
            distributed_lock_module, "_redis_client", lambda: _FakeRedisClientForLock(fake_lock)
        )

        with pytest.raises(ValueError, match="boom"):
            async with admin_write_lock("some-resource"):
                raise ValueError("boom")
