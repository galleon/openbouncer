import asyncio
import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.keys import get_key_store, parse_key_store
from app.core.registry import ModelConcurrencyLimiter, get_model_registry, parse_model_registry
from app.main import app
from app.upstream.client import get_upstream_client

CONCURRENCY_KEY = "sk-concurrency-abcdefabcdefabcdefabcdef"
CONCURRENCY_KEY_HASH = hashlib.sha256(CONCURRENCY_KEY.encode()).hexdigest()
CONCURRENCY_KEY_STORE_YAML = f"""
keys:
  - id: concurrency-key
    key_hash: {CONCURRENCY_KEY_HASH}
    allowed_models: [test/limited]
    requests_per_minute: 1000000
"""

LIMITED_REGISTRY_YAML = """
models:
  - id: test/limited
    upstream_model: test-model
    base_url: http://upstream.invalid/v1
    api_key_env: UPSTREAM_TEST_API_KEY
    capabilities: [chat]
    concurrency_limit: 1
"""


class _ControllableUpstreamClient:
    """Fake UpstreamClient whose stream_chat_completion holds each call
    open until release_event is set, signaling reached_block right before
    it blocks -- lets a test deterministically wait for "the in-flight call
    has reached its blocking point" instead of guessing how many event-loop
    ticks a real ASGI request needs to get there."""

    def __init__(self):
        self.active = 0
        self.max_active_seen = 0
        self.release_event = asyncio.Event()
        self.reached_block = asyncio.Event()

    async def stream_chat_completion(self, *, base_url, api_key, upstream_model, request):
        self.active += 1
        self.max_active_seen = max(self.max_active_seen, self.active)
        try:
            yield 'data: {"id":"c1","object":"chat.completion.chunk","choices":[]}\n\n'
            self.reached_block.set()
            await self.release_event.wait()
            yield "data: [DONE]\n\n"
        finally:
            self.active -= 1


class TestModelConcurrencyLimiterUnit:
    @pytest.mark.asyncio
    async def test_tracks_in_flight_while_held(self):
        limiter = ModelConcurrencyLimiter(limit=2)
        async with limiter.acquire():
            async with limiter.acquire():
                assert limiter.in_flight == 2
                assert limiter.queued == 0
        assert limiter.in_flight == 0

    @pytest.mark.asyncio
    async def test_blocks_beyond_limit_until_release(self):
        limiter = ModelConcurrencyLimiter(limit=1)
        started = asyncio.Event()
        release = asyncio.Event()
        second_acquired = asyncio.Event()

        async def holder():
            async with limiter.acquire():
                started.set()
                await release.wait()

        async def waiter():
            await started.wait()
            async with limiter.acquire():
                second_acquired.set()

        task1 = asyncio.create_task(holder())
        task2 = asyncio.create_task(waiter())
        await started.wait()
        # Give waiter() a tick to actually reach the acquire() call and
        # start queuing behind the held semaphore.
        for _ in range(5):
            await asyncio.sleep(0)
        assert limiter.in_flight == 1
        assert limiter.queued == 1
        assert not second_acquired.is_set()

        release.set()
        await asyncio.wait_for(second_acquired.wait(), timeout=1)
        await task1
        await task2
        assert limiter.in_flight == 0
        assert limiter.queued == 0


@pytest.fixture
async def concurrency_client(monkeypatch):
    monkeypatch.setenv("UPSTREAM_TEST_API_KEY", "test-key")
    registry = parse_model_registry(LIMITED_REGISTRY_YAML)
    key_store = parse_key_store(CONCURRENCY_KEY_STORE_YAML)
    fake_upstream = _ControllableUpstreamClient()

    app.dependency_overrides[get_model_registry] = lambda: registry
    app.dependency_overrides[get_key_store] = lambda: key_store
    app.dependency_overrides[get_upstream_client] = lambda: fake_upstream
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {CONCURRENCY_KEY}"},
        ) as ac:
            yield ac, fake_upstream, registry
    finally:
        app.dependency_overrides.pop(get_model_registry, None)
        app.dependency_overrides.pop(get_key_store, None)
        app.dependency_overrides.pop(get_upstream_client, None)


@pytest.mark.asyncio
async def test_streaming_chat_route_enforces_concurrency_limit(concurrency_client):
    client, fake_upstream, registry = concurrency_client
    body = {
        "model": "test/limited",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    # Fire 2 concurrent requests against a concurrency_limit=1 model.
    task1 = asyncio.create_task(client.post("/v1/chat/completions", json=body))
    task2 = asyncio.create_task(client.post("/v1/chat/completions", json=body))

    # Deterministically wait for whichever request got the semaphore to
    # reach its blocking point, rather than guessing how many event-loop
    # ticks a real ASGI request needs -- since concurrency_limit=1, the
    # second request can't even enter the fake upstream body until the
    # first releases, so this can only fire for the one holding the slot.
    await asyncio.wait_for(fake_upstream.reached_block.wait(), timeout=1)
    assert fake_upstream.max_active_seen == 1

    limiter = registry.get_concurrency_limiter("test/limited")
    assert limiter.in_flight == 1
    # No event to await for "the second request has reached the semaphore"
    # (it's blocked inside asyncio.Semaphore.acquire(), not at an
    # application-visible point) -- poll briefly rather than guessing a
    # fixed tick count, which was flaky under scheduler variance.
    for _ in range(1000):
        if limiter.queued == 1:
            break
        await asyncio.sleep(0)
    assert limiter.queued == 1

    # Unblock the held request -- the second should then proceed.
    fake_upstream.release_event.set()
    response1 = await task1
    response2 = await task2

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert fake_upstream.max_active_seen == 1
    assert limiter.in_flight == 0
    assert limiter.queued == 0
