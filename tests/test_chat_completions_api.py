import httpx
import pytest
import respx

CHAT_URL = "http://vllm-gemma4:8000/v1/chat/completions"


def _upstream_response(content: str = "hello back") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


@pytest.fixture(autouse=True)
def _upstream_api_key(monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")


@pytest.mark.asyncio
@respx.mock
async def test_chat_completions_calls_real_upstream(client):
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_upstream_response("hello back"))
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    assert response.status_code == 200
    assert route.called
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello back"
    # Reports our own public model id, not whatever the upstream echoed back.
    assert body["model"] == "local/gemma4-nvfp4"


@pytest.mark.asyncio
@respx.mock
async def test_chat_completions_tolerates_upstream_vendor_fields(client):
    # Real vLLM responses include fields this gateway doesn't define (seen
    # live: refusal, annotations, audio, function_call, tool_calls,
    # reasoning on the message; logprobs, stop_reason, token_ids,
    # routed_experts on the choice; service_tier, system_fingerprint,
    # prompt_tokens_details on the top level) -- parsing must not choke on
    # fields it doesn't recognize.
    upstream_body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
        "service_tier": None,
        "system_fingerprint": "vllm-0.22.1+deadbeef",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "hi",
                    "refusal": None,
                    "annotations": None,
                    "audio": None,
                    "function_call": None,
                    "tool_calls": [],
                    "reasoning": None,
                },
                "logprobs": None,
                "finish_reason": "stop",
                "stop_reason": 106,
                "token_ids": None,
                "routed_experts": None,
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "prompt_tokens_details": None,
        },
        "prompt_logprobs": None,
    }
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=upstream_body))

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"


@pytest.mark.asyncio
@respx.mock
async def test_chat_completions_image_input(client):
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_upstream_response("a cat"))
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        },
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "a cat"
    # The image_url content part must actually reach the upstream, not just
    # the text part.
    sent_body = respx.calls.last.request.content
    assert b"https://example.com/cat.png" in sent_body
    assert route.called


@pytest.mark.asyncio
async def test_chat_completions_rejects_unsupported_top_level_field(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 5,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "n" in body["error"]["param"]
    assert "n" in body["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completions_rejects_image_input_missing_url(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {}}]}
            ],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] is None


@pytest.mark.asyncio
async def test_chat_completions_rejects_unsupported_content_part_type(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": "https://example.com/cat.mp4"}}
                    ],
                }
            ],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
@respx.mock
async def test_chat_completions_accepts_all_allowed_models(client):
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_upstream_response()))
    for model_id in ("local/gemma4-nvfp4",):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["model"] == model_id


@pytest.mark.asyncio
async def test_chat_completions_rejects_unregistered_model(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["param"] == "model"


@pytest.mark.asyncio
async def test_chat_completions_rejects_embeddings_only_model(client):
    # local/bge-m3 exists and is allowed for the test key, but its
    # capabilities are embeddings-only -- no "chat".
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/bge-m3",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "model_does_not_support_chat"
    assert body["error"]["param"] == "model"


@pytest.mark.asyncio
async def test_chat_completions_rejects_upstream_model_name(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            # The upstream_model string for local/gemma4-nvfp4 (see
            # config/models.yaml) -- must not resolve as a registry id.
            "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "model_not_found"
