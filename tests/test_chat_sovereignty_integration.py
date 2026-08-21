import hashlib

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.auth.keys import get_key_store, parse_key_store
from app.main import app

# local/gemma4-nvfp4 in the bundled config/models.yaml is tagged
# `sovereignty: {hosting: on-prem, data_residency: EU}` -- these tests rely
# on that real registry entry rather than overriding get_model_registry, so
# they also double as a check that the bundled config actually declares it.
CHAT_URL = "http://vllm-gemma4:8000/v1/chat/completions"

EU_KEY = "sk-eu-abcdefabcdefabcdefabcdefabcdef"
EU_KEY_HASH = hashlib.sha256(EU_KEY.encode()).hexdigest()
US_ONLY_KEY = "sk-usonly-abcdefabcdefabcdefabcdef"
US_ONLY_KEY_HASH = hashlib.sha256(US_ONLY_KEY.encode()).hexdigest()
UNRESTRICTED_KEY = "sk-unrestricted-sov-abcdefabcdefabcdef"
UNRESTRICTED_KEY_HASH = hashlib.sha256(UNRESTRICTED_KEY.encode()).hexdigest()

STORE_YAML = f"""
keys:
  - id: eu-key
    key_hash: {EU_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
    required_sovereignty:
      data_residency: EU
  - id: us-only-key
    key_hash: {US_ONLY_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
    required_sovereignty:
      data_residency: US
  - id: unrestricted-key
    key_hash: {UNRESTRICTED_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
"""

UPSTREAM_SUCCESS_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
}


@pytest.fixture
async def sovereignty_client_factory():
    store = parse_key_store(STORE_YAML)
    app.dependency_overrides[get_key_store] = lambda: store

    async def _client(raw_key: str) -> AsyncClient:
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {raw_key}"})

    try:
        yield _client
    finally:
        app.dependency_overrides.pop(get_key_store, None)


@pytest.mark.asyncio
@respx.mock
async def test_matching_sovereignty_constraint_allows_the_request(sovereignty_client_factory, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))

    async with await sovereignty_client_factory(EU_KEY) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_mismatched_sovereignty_constraint_rejects_with_403(sovereignty_client_factory, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))

    async with await sovereignty_client_factory(US_ONLY_KEY) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "sovereignty_violation"
    assert body["error"]["type"] == "permission_error"
    assert not route.called  # rejected before the upstream is ever reached


@pytest.mark.asyncio
@respx.mock
async def test_unrestricted_key_is_unaffected(sovereignty_client_factory, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))

    async with await sovereignty_client_factory(UNRESTRICTED_KEY) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_streaming_request_also_enforces_sovereignty(sovereignty_client_factory, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))

    async with await sovereignty_client_factory(US_ONLY_KEY) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    # A real 403, not a 200 SSE stream -- checked before any generation, same
    # as ensure_model_allowed.
    assert response.status_code == 403
    assert not response.headers.get("content-type", "").startswith("text/event-stream")
    assert response.json()["error"]["code"] == "sovereignty_violation"
    assert not route.called
