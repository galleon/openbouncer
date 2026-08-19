import hashlib

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.auth.budget import BudgetTracker, get_budget_tracker
from app.auth.keys import get_key_store, parse_key_store
from app.main import app

CHAT_URL = "http://vllm-gemma4:8000/v1/chat/completions"

BUDGET_KEY = "sk-budget-abcdefabcdefabcdefabcdef"
BUDGET_KEY_HASH = hashlib.sha256(BUDGET_KEY.encode()).hexdigest()
# token_budget_daily matches UPSTREAM_SUCCESS_BODY's total_tokens below, so
# exactly one request's worth of usage exhausts it.
BUDGET_STORE_YAML = f"""
keys:
  - id: budget-key
    key_hash: {BUDGET_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
    token_budget_daily: 3
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
async def budget_client():
    store = parse_key_store(BUDGET_STORE_YAML)
    tracker = BudgetTracker()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_budget_tracker] = lambda: tracker
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {BUDGET_KEY}"},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_key_store, None)
        app.dependency_overrides.pop(get_budget_tracker, None)


@pytest.mark.asyncio
@respx.mock
async def test_first_request_within_budget_succeeds(budget_client, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))

    response = await budget_client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_request_after_budget_exhausted_is_rejected_with_429(budget_client, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))

    first = await budget_client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert first.status_code == 200
    assert route.call_count == 1

    # The first response's 3 tokens (== token_budget_daily) were recorded
    # after it completed -- this second request's pre-flight budget check
    # now sees the key as already at its cap.
    second = await budget_client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "hi again"}]},
    )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "token_budget_exceeded"
    assert second.json()["error"]["type"] == "rate_limit_error"
    # The upstream was never called for the rejected request -- same
    # "reject before spending anything further" posture as rate limiting.
    assert route.call_count == 1
