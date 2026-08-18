import json
import re

import httpx
import pytest
import respx

from app.guardrails.output_leak import OutputLeakAction, OutputLeakConfig, get_output_leak_config
from app.main import app

CHAT_URL = "http://vllm-gemma4:8000/v1/chat/completions"

UPSTREAM_SUCCESS_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "sure, email me at jane.doe@example.com anytime"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
}

CLEAN_UPSTREAM_BODY = {
    **UPSTREAM_SUCCESS_BODY,
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "here's your answer, nothing sensitive"}, "finish_reason": "stop"}
    ],
}


def _sse_body(*data_values: str) -> bytes:
    body = ""
    for value in data_values:
        body += f"data: {value}\n\n"
    return body.encode()


def _content_chunk(content: str) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-up",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
    )


def _metric_value(body: str, name: str, labels: str = "") -> float:
    target = f"{name}{{{labels}}}" if labels else name
    pattern = re.escape(target) + r" ([0-9.eE+-]+)"
    match = re.search(pattern, body)
    return float(match.group(1)) if match else 0.0


def _config(**category_overrides: OutputLeakAction) -> OutputLeakConfig:
    return OutputLeakConfig(enabled=True, categories=dict(category_overrides))


@pytest.fixture
def ol_override():
    def _install(config: OutputLeakConfig) -> OutputLeakConfig:
        app.dependency_overrides[get_output_leak_config] = lambda: config
        return config

    yield _install
    app.dependency_overrides.pop(get_output_leak_config, None)


@pytest.mark.asyncio
@respx.mock
async def test_block_action_non_streaming_returns_403_after_generation(client, ol_override, monkeypatch):
    # Unlike the prompt-injection pre-filter's block (which never calls the
    # upstream at all), an output-leak block only becomes knowable *after*
    # generation -- the upstream route IS called here.
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))
    ol_override(_config(email=OutputLeakAction.BLOCK))

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "what's your contact email?"}]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "output_leak_detected"
    assert response.json()["error"]["type"] == "permission_error"
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_redact_action_rewrites_response_content(client, ol_override, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))
    ol_override(_config(email=OutputLeakAction.REDACT))

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "what's your contact email?"}]},
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert "[EMAIL]" in content
    assert "jane.doe@example.com" not in content


@pytest.mark.asyncio
@respx.mock
async def test_flag_action_returns_response_unmodified(client, ol_override, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))
    ol_override(_config())  # every category defaults to flag

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "what's your contact email?"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == UPSTREAM_SUCCESS_BODY["choices"][0]["message"]["content"]


@pytest.mark.asyncio
@respx.mock
async def test_disabled_config_is_zero_overhead(admin_client, ol_override, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))
    ol_override(OutputLeakConfig(enabled=False))

    before_body = (await admin_client.get("/metrics")).text
    before = _metric_value(before_body, "openbouncer_output_leak_scanned_total", "")

    response = await admin_client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "what's your contact email?"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == UPSTREAM_SUCCESS_BODY["choices"][0]["message"]["content"]

    after_body = (await admin_client.get("/metrics")).text
    after = _metric_value(after_body, "openbouncer_output_leak_scanned_total", "")
    assert after == before


@pytest.mark.asyncio
@respx.mock
async def test_block_action_increments_action_metric(admin_client, ol_override, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=UPSTREAM_SUCCESS_BODY))
    ol_override(_config(email=OutputLeakAction.BLOCK))

    before_body = (await admin_client.get("/metrics")).text
    before = _metric_value(before_body, "openbouncer_output_leak_actions_total", 'action="block"')

    response = await admin_client.post(
        "/v1/chat/completions",
        json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": "what's your contact email?"}]},
    )
    assert response.status_code == 403

    after_body = (await admin_client.get("/metrics")).text
    after = _metric_value(after_body, "openbouncer_output_leak_actions_total", 'action="block"')
    assert after - before == 1


@pytest.mark.asyncio
@respx.mock
async def test_streaming_flag_only_forwards_chunks_live(client, ol_override, monkeypatch):
    # No category configured redact/block -- requires_buffering() is False,
    # so chunks should be relayed as they arrive rather than collapsed into
    # one buffered chunk.
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    chunk1 = _content_chunk("sure, email me at ")
    chunk2 = _content_chunk("jane.doe@example.com anytime")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(chunk1, chunk2, "[DONE]")))
    ol_override(_config())  # flag-only

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "what's your contact email?"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert f"data: {chunk1}\n\n" in body
    assert f"data: {chunk2}\n\n" in body
    assert body.count("data: [DONE]\n\n") == 1


@pytest.mark.asyncio
@respx.mock
async def test_streaming_redact_buffers_and_collapses_to_one_chunk(client, ol_override, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    chunk1 = _content_chunk("sure, email me at ")
    chunk2 = _content_chunk("jane.doe@example.com anytime")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(chunk1, chunk2, "[DONE]")))
    ol_override(_config(email=OutputLeakAction.REDACT))

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "what's your contact email?"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert "[EMAIL]" in body
    assert "jane.doe@example.com" not in body
    assert body.count("data: [DONE]\n\n") == 1


@pytest.mark.asyncio
@respx.mock
async def test_streaming_block_yields_in_band_sse_error_not_http_403(client, ol_override, monkeypatch):
    # Streaming has already committed to HTTP 200 text/event-stream by the
    # time the buffered scan runs -- unlike the non-streaming case, this
    # can't be a real HTTP error (see _with_output_leak_scan's docstring).
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    chunk1 = _content_chunk("sure, email me at jane.doe@example.com")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(chunk1, "[DONE]")))
    ol_override(_config(email=OutputLeakAction.BLOCK))

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "what's your contact email?"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert '"code": "output_leak_detected"' in body
    assert body.count("data: [DONE]\n\n") == 1


@pytest.mark.asyncio
@respx.mock
async def test_streaming_clean_response_with_redact_configured_still_buffers_but_passes_through(
    client, ol_override, monkeypatch
):
    # requires_buffering() is True (a category is configured redact), but
    # this particular response has nothing to redact -- content should
    # still reach the client unchanged, just via the buffered path.
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    chunk1 = _content_chunk("here's your answer, ")
    chunk2 = _content_chunk("nothing sensitive")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(chunk1, chunk2, "[DONE]")))
    ol_override(_config(email=OutputLeakAction.REDACT))

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "tell me something"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    body = response.text
    assert f"data: {chunk1}\n\n" in body
    assert f"data: {chunk2}\n\n" in body
