import json

import httpx
import pytest
import respx

from app.core.errors import OpenAIError
from app.schemas.chat import ChatCompletionRequest
from app.upstream.client import UpstreamClient

BASE_URL = "https://integrate.api.nvidia.com/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


def _success_response(model: str = "nvidia/Qwen3.6-27B-NVFP4") -> dict:
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


@pytest.mark.asyncio
@respx.mock
async def test_calls_correct_url_and_forwards_messages_including_image():
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))

    request = ChatCompletionRequest(
        model="nvidia/nemotron-vision",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/cat.png", "detail": "high"},
                    },
                ],
            }
        ],
        temperature=0.2,
        top_p=0.8,
        max_tokens=64,
        stop=["\n"],
        user="user-123",
    )

    client = UpstreamClient()
    result = await client.create_chat_completion(
        base_url=BASE_URL,
        api_key="secret-key",
        upstream_model="nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-NVFP4-QAD",
        request=request,
    )

    assert route.called
    sent = route.calls.last.request
    assert sent.url == CHAT_URL
    assert sent.headers["authorization"] == "Bearer secret-key"

    payload = json.loads(sent.content)
    assert payload["model"] == "nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-NVFP4-QAD"
    assert payload["stream"] is False
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.8
    assert payload["max_tokens"] == 64
    assert payload["stop"] == ["\n"]
    assert payload["user"] == "user-123"

    parts = payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/cat.png", "detail": "high"},
    }

    assert result.id == "chatcmpl-abc123"
    assert result.choices[0].message.content == "Hello!"


@pytest.mark.asyncio
@respx.mock
async def test_forces_non_streaming_even_if_requested_stream_false_explicit():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))

    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
    )
    client = UpstreamClient()
    result = await client.create_chat_completion(
        base_url=BASE_URL,
        api_key="k",
        upstream_model="nvidia/Qwen3.6-27B-NVFP4",
        request=request,
    )
    assert result.object == "chat.completion"


@pytest.mark.asyncio
async def test_streaming_request_is_rejected_without_calling_upstream():
    with respx.mock:
        route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))

        request = ChatCompletionRequest(
            model="nvidia/qwen3.6-nvfp4",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        client = UpstreamClient()

        with pytest.raises(OpenAIError) as exc_info:
            await client.create_chat_completion(
                base_url=BASE_URL,
                api_key="k",
                upstream_model="nvidia/Qwen3.6-27B-NVFP4",
                request=request,
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.param == "stream"
        assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_normalizes_upstream_error_with_openai_shape():
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
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient()

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="bad-key",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )

    err = exc_info.value
    assert err.status_code == 401
    assert err.error_type == "authentication_error"
    assert err.code == "invalid_api_key"
    assert err.message == "Invalid API key"


@pytest.mark.asyncio
@respx.mock
async def test_normalizes_upstream_error_without_json_body():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient()

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="k",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )

    err = exc_info.value
    assert err.status_code == 500
    assert err.error_type == "api_error"
    assert "500" in err.message
    assert "Internal Server Error" in err.message


@pytest.mark.asyncio
@respx.mock
async def test_normalizes_non_json_success_response():
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient()

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="k",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "invalid_upstream_response"


@pytest.mark.asyncio
@respx.mock
async def test_normalizes_success_response_missing_required_fields():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient()

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="k",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "invalid_upstream_response"


@pytest.mark.asyncio
@respx.mock
async def test_retries_on_connect_error_then_succeeds():
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=_success_response()),
        ]
    )
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient(max_retries=2, backoff_seconds=0.001)

    result = await client.create_chat_completion(
        base_url=BASE_URL,
        api_key="k",
        upstream_model="nvidia/Qwen3.6-27B-NVFP4",
        request=request,
    )

    assert route.call_count == 2
    assert result.id == "chatcmpl-abc123"


@pytest.mark.asyncio
@respx.mock
async def test_exhausts_retries_and_raises_normalized_error():
    route = respx.post(CHAT_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient(max_retries=2, backoff_seconds=0.001)

    with pytest.raises(OpenAIError) as exc_info:
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="k",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )

    assert route.call_count == 3
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "upstream_unavailable"


@pytest.mark.asyncio
@respx.mock
async def test_does_not_retry_on_http_error_status():
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(500, text="boom"))
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )
    client = UpstreamClient(max_retries=2, backoff_seconds=0.001)

    with pytest.raises(OpenAIError):
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="k",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_reuses_externally_provided_http_client():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))
    request = ChatCompletionRequest(
        model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
    )

    async with httpx.AsyncClient() as shared_client:
        client = UpstreamClient(http_client=shared_client)
        await client.create_chat_completion(
            base_url=BASE_URL,
            api_key="k",
            upstream_model="nvidia/Qwen3.6-27B-NVFP4",
            request=request,
        )
        assert not shared_client.is_closed
