import json

import httpx
import pytest
import respx

from app.api.routes.chat import _relay_stream
from app.schemas.chat import ChatCompletionRequest
from app.upstream.client import UpstreamClient

BASE_URL = "http://vllm-gemma4:8000/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


class _TrackingStream(httpx.AsyncByteStream):
    """A streaming response body that can raise mid-iteration and reports aclose()."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


def _sse_body(*data_values: str) -> bytes:
    body = ""
    for value in data_values:
        body += f"data: {value}\n\n"
    return body.encode()


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")


@pytest.mark.asyncio
@respx.mock
async def test_stream_relays_valid_frames_and_ends_with_done(client):
    chunk1 = json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": []})
    chunk2 = json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": []})
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, content=_sse_body(chunk1, chunk2, "[DONE]"))
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    assert f"data: {chunk1}\n\n" in body
    assert f"data: {chunk2}\n\n" in body
    assert body.count("data: [DONE]\n\n") == 1
    assert body.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
@respx.mock
async def test_stream_skips_malformed_data_frames(client):
    valid_chunk = json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": []})
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, content=_sse_body(valid_chunk, "{not valid json", "[DONE]"))
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert f"data: {valid_chunk}\n\n" in body
    assert "not valid json" not in body
    assert body.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
@respx.mock
async def test_stream_falls_back_to_done_when_upstream_omits_it(client):
    valid_chunk = json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": []})
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(valid_chunk)))

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert f"data: {valid_chunk}\n\n" in body
    assert body.endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
@respx.mock
async def test_stream_error_before_first_token_returns_json_error(client):
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "message": "Invalid API key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
        )
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
@respx.mock
async def test_stream_error_after_start_emits_sse_error_then_done(client):
    valid_chunk = json.dumps({"id": "c1", "object": "chat.completion.chunk", "choices": []})
    stream = _TrackingStream(
        [f"data: {valid_chunk}\n\n".encode(), httpx.ReadError("connection reset")]
    )
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, stream=stream))

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    body = response.text
    assert f"data: {valid_chunk}\n\n" in body
    assert '"upstream_disconnected"' in body
    assert '"type": "api_error"' in body
    assert body.endswith("data: [DONE]\n\n")
    # the terminal DONE must be the last frame, after the error frame
    assert body.index('"upstream_disconnected"') < body.rindex("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_stream_rejects_unregistered_model(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_stream_missing_api_key_returns_json_error(client, monkeypatch):
    monkeypatch.delenv("UPSTREAM_VLLM_API_KEY", raising=False)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "missing_api_key"


@pytest.mark.asyncio
@respx.mock
async def test_disconnect_closes_upstream_connection():
    stream = _TrackingStream(
        [
            f"data: {json.dumps({'id': 'c1'})}\n\n".encode(),
            f"data: {json.dumps({'id': 'c2'})}\n\n".encode(),
        ]
    )
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, stream=stream))

    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    upstream_client = UpstreamClient()
    agen = upstream_client.stream_chat_completion(
        base_url=BASE_URL,
        api_key="test-key",
        upstream_model="nvidia/Qwen3.6-27B-NVFP4",
        request=request,
    )

    first_chunk = await agen.__anext__()
    assert first_chunk.startswith("data: ")

    relay = _relay_stream(agen, first_chunk)
    first_relayed = await relay.__anext__()
    assert first_relayed == first_chunk

    # Simulate what Starlette does when the client disconnects: the consumer
    # stops asking for more data and closes the generator early.
    await relay.aclose()

    assert stream.closed is True
