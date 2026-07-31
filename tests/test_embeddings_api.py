import httpx
import pytest
import respx

from app.guardrails.service import GuardrailsService, get_guardrails_service
from app.main import app

BASE_URL = "http://vllm-bge-m3:8000/v1"
EMBEDDINGS_URL = f"{BASE_URL}/embeddings"


def _upstream_response(model: str = "BAAI/bge-m3") -> dict:
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "model": model,
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    }


@pytest.fixture(autouse=True)
def _upstream_api_key(monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "local")


@pytest.mark.asyncio
@respx.mock
async def test_embeddings_calls_real_upstream(client):
    route = respx.post(EMBEDDINGS_URL).mock(
        return_value=httpx.Response(200, json=_upstream_response())
    )

    response = await client.post(
        "/v1/embeddings",
        json={"model": "local/bge-m3", "input": "hello world"},
    )

    assert response.status_code == 200
    assert route.called
    body = response.json()
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    # Reports our own public model id, not whatever the upstream echoed back.
    assert body["model"] == "local/bge-m3"


@pytest.mark.asyncio
async def test_embeddings_rejects_unregistered_model(client):
    response = await client.post(
        "/v1/embeddings",
        json={"model": "does/not-exist", "input": "hi"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_embeddings_rejects_chat_only_model(client):
    # local/gemma4-nvfp4 exists and is allowed for the test key, but its
    # capabilities are chat-only -- no "embeddings".
    response = await client.post(
        "/v1/embeddings",
        json={"model": "local/gemma4-nvfp4", "input": "hi"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "model_does_not_support_embeddings"
    assert body["error"]["param"] == "model"


@pytest.mark.asyncio
@respx.mock
async def test_embeddings_never_calls_guardrails(client):
    """Regardless of GUARDRAILS_MODE, /v1/embeddings must never touch
    GuardrailsService -- guardrails are for moderating generated
    conversational text, not embedding vectors."""

    class ExplodingGuardrailsService(GuardrailsService):
        async def process_chat_completion(self, request):
            raise AssertionError("embeddings must never call GuardrailsService")

        async def stream_chat_completion(self, request):
            raise AssertionError("embeddings must never call GuardrailsService")

    respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(200, json=_upstream_response()))
    app.dependency_overrides[get_guardrails_service] = lambda: ExplodingGuardrailsService()
    try:
        response = await client.post(
            "/v1/embeddings",
            json={"model": "local/bge-m3", "input": "hi"},
        )
    finally:
        app.dependency_overrides.pop(get_guardrails_service, None)

    assert response.status_code == 200
