import json

import httpx
import pytest
import respx

from app.core.errors import OpenAIError
from app.schemas.embeddings import EmbeddingRequest
from app.upstream.client import UpstreamClient

BASE_URL = "http://localhost:11434/v1"
EMBEDDINGS_URL = f"{BASE_URL}/embeddings"


def _success_response(model: str = "nomic-embed-text") -> dict:
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "model": model,
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


@pytest.mark.asyncio
@respx.mock
async def test_calls_correct_url_and_forwards_input():
    route = respx.post(EMBEDDINGS_URL).mock(
        return_value=httpx.Response(200, json=_success_response())
    )

    request = EmbeddingRequest(model="ollama/nomic-embed-text", input="hello world")
    client = UpstreamClient()

    result = await client.create_embeddings(
        base_url=BASE_URL,
        api_key="ollama",
        upstream_model="nomic-embed-text",
        request=request,
    )

    assert route.called
    sent = route.calls.last.request
    assert sent.url == EMBEDDINGS_URL
    assert sent.headers["authorization"] == "Bearer ollama"

    payload = json.loads(sent.content)
    assert payload["model"] == "nomic-embed-text"
    assert payload["input"] == "hello world"

    assert result.data[0].embedding == [0.1, 0.2, 0.3]
    assert result.usage.total_tokens == 3


@pytest.mark.asyncio
@respx.mock
async def test_forwards_list_input():
    respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(200, json=_success_response()))

    request = EmbeddingRequest(model="ollama/nomic-embed-text", input=["a", "b"])
    client = UpstreamClient()

    await client.create_embeddings(
        base_url=BASE_URL,
        api_key="ollama",
        upstream_model="nomic-embed-text",
        request=request,
    )

    payload = json.loads(respx.calls.last.request.content)
    assert payload["input"] == ["a", "b"]


@pytest.mark.asyncio
@respx.mock
async def test_normalizes_upstream_error():
    respx.post(EMBEDDINGS_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": "Invalid key", "type": "authentication_error"}},
        )
    )
    request = EmbeddingRequest(model="ollama/nomic-embed-text", input="hi")
    client = UpstreamClient()

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_embeddings(
            base_url=BASE_URL,
            api_key="bad",
            upstream_model="nomic-embed-text",
            request=request,
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "authentication_error"


@pytest.mark.asyncio
@respx.mock
async def test_normalizes_malformed_success_response():
    respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    request = EmbeddingRequest(model="ollama/nomic-embed-text", input="hi")
    client = UpstreamClient()

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_embeddings(
            base_url=BASE_URL,
            api_key="ollama",
            upstream_model="nomic-embed-text",
            request=request,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "invalid_upstream_response"


@pytest.mark.asyncio
@respx.mock
async def test_connect_error_after_retries_returns_service_unavailable():
    respx.post(EMBEDDINGS_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    request = EmbeddingRequest(model="ollama/nomic-embed-text", input="hi")
    client = UpstreamClient(max_retries=1, backoff_seconds=0.001)

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_embeddings(
            base_url=BASE_URL,
            api_key="ollama",
            upstream_model="nomic-embed-text",
            request=request,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "upstream_unavailable"
