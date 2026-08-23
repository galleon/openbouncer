import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from app.guardrails.output_leak import OutputLeakAction, OutputLeakConfig, get_output_leak_config
from app.guardrails.service import NemoLibraryGuardrailsService, get_guardrails_service
from app.main import app

CHAT_URL = "http://vllm-gemma4:8000/v1/chat/completions"
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "guardrails_configs"

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location.",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
}


def _sse_body(*data_values: str) -> bytes:
    body = ""
    for value in data_values:
        body += f"data: {value}\n\n"
    return body.encode()


def _tool_call_response(arguments: str, *, call_id: str = "call_abc123") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _tool_call_delta_chunks(arg_fragments: list[str], *, call_id: str = "call_abc123") -> list[str]:
    """Builds the streaming delta sequence for one tool call: the first
    fragment carries id/type/function.name, every fragment carries an
    arguments piece -- matches the real OpenAI-compatible streaming shape."""
    chunks = []
    for i, fragment in enumerate(arg_fragments):
        delta_tool_call = {"index": 0, "function": {"arguments": fragment}}
        if i == 0:
            delta_tool_call["id"] = call_id
            delta_tool_call["type"] = "function"
            delta_tool_call["function"]["name"] = "get_weather"
        chunks.append(
            json.dumps(
                {
                    "id": "chatcmpl-up",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
                    "choices": [{"index": 0, "delta": {"tool_calls": [delta_tool_call]}, "finish_reason": None}],
                }
            )
        )
    chunks.append(
        json.dumps(
            {
                "id": "chatcmpl-up",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
        )
    )
    return chunks


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


def _metric_value(body: str, name: str, labels: str) -> float:
    pattern = re.escape(f"{name}{{{labels}}}") + r" ([0-9.eE+-]+)"
    match = re.search(pattern, body)
    return float(match.group(1)) if match else 0.0


def _ol_config(**category_overrides: OutputLeakAction) -> OutputLeakConfig:
    return OutputLeakConfig(enabled=True, categories=dict(category_overrides))


@pytest.fixture
def ol_override():
    def _install(config: OutputLeakConfig) -> OutputLeakConfig:
        app.dependency_overrides[get_output_leak_config] = lambda: config
        return config

    yield _install
    app.dependency_overrides.pop(get_output_leak_config, None)


@pytest.fixture(autouse=True)
def _upstream_api_key(monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")


class TestRequestPassthrough:
    @pytest.mark.asyncio
    @respx.mock
    async def test_tools_forwarded_to_upstream_verbatim(self, client):
        route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_tool_call_response("{}")))

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [WEATHER_TOOL],
                "tool_choice": "auto",
            },
        )

        assert response.status_code == 200
        sent = json.loads(route.calls.last.request.content)
        assert sent["tools"] == [WEATHER_TOOL]
        assert sent["tool_choice"] == "auto"

    @pytest.mark.asyncio
    @respx.mock
    async def test_tool_result_history_round_trips(self, client):
        # A realistic multi-turn conversation: the model's own prior
        # tool-calling turn (content=None) replayed back, followed by the
        # tool's result (role="tool") -- both must validate and forward.
        route = respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_tool_call_response("{}", call_id="call_2"))
        )
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [
                    {"role": "user", "content": "weather in Paris?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"location": "Paris"}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_abc123", "content": '{"temp_c": 18}'},
                ],
                "tools": [WEATHER_TOOL],
            },
        )
        assert response.status_code == 200
        assert route.called

    @pytest.mark.asyncio
    async def test_tool_calls_on_non_assistant_message_rejected(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [
                    {
                        "role": "user",
                        "content": "hi",
                        "tool_calls": [
                            {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_null_content_on_non_assistant_message_rejected(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "local/gemma4-nvfp4", "messages": [{"role": "user", "content": None}]},
        )
        assert response.status_code == 400


class TestResponsePassthrough:
    @pytest.mark.asyncio
    @respx.mock
    async def test_non_streaming_tool_calls_relayed(self, client):
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, json=_tool_call_response('{"location": "Paris"}'))
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [WEATHER_TOOL],
            },
        )

        assert response.status_code == 200
        message = response.json()["choices"][0]["message"]
        assert message["content"] is None
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"
        assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"location": "Paris"}
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    @respx.mock
    async def test_streaming_tool_call_deltas_relayed_unmodified(self, client):
        chunks = _tool_call_delta_chunks(['{"loc', 'ation": "Pa', 'ris"}'])
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(*chunks, "[DONE]")))

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": [WEATHER_TOOL],
                "stream": True,
            },
        )

        assert response.status_code == 200
        body = response.text
        for chunk in chunks:
            assert f"data: {chunk}\n\n" in body


class TestToolCallsMetric:
    @pytest.mark.asyncio
    @respx.mock
    async def test_non_streaming_response_with_tool_calls_increments_metric(self, admin_client):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_tool_call_response("{}")))

        before = _metric_value(
            (await admin_client.get("/metrics")).text,
            "openbouncer_tool_calls_total",
            'model="local/gemma4-nvfp4"',
        )
        response = await admin_client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [WEATHER_TOOL],
            },
        )
        assert response.status_code == 200
        after = _metric_value(
            (await admin_client.get("/metrics")).text,
            "openbouncer_tool_calls_total",
            'model="local/gemma4-nvfp4"',
        )
        assert after == before + 1


class TestNemoLibraryRejection:
    @pytest.fixture
    def guardrails_override(self):
        def _install() -> NemoLibraryGuardrailsService:
            service = NemoLibraryGuardrailsService(config_store_path=str(FIXTURES_DIR))
            app.dependency_overrides[get_guardrails_service] = lambda: service
            return service

        yield _install
        app.dependency_overrides.pop(get_guardrails_service, None)

    @pytest.mark.asyncio
    async def test_tools_with_guardrails_requested_is_rejected(self, client, guardrails_override):
        guardrails_override()
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [WEATHER_TOOL],
                "guardrails": {"config_id": "no_rails"},
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "tool_calling_not_supported_in_nemo_library_mode"

    @pytest.mark.asyncio
    @respx.mock
    async def test_tools_with_guardrails_disabled_is_not_rejected(self, client, guardrails_override):
        # guardrails.enabled=false bypasses the guardrails backend entirely
        # (see app/api/routes/chat.py's _guardrails_requested), so the
        # nemo_library-specific rejection -- which only fires when this
        # call would actually route through it -- must not apply here.
        guardrails_override()
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_tool_call_response("{}")))
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [WEATHER_TOOL],
                "guardrails": {"enabled": False},
            },
        )
        assert response.status_code == 200


class TestOutputLeakScansToolCallArguments:
    @pytest.mark.asyncio
    @respx.mock
    async def test_block_category_blocks_response_with_leak_in_arguments(self, client, ol_override):
        ol_override(_ol_config(secret_token=OutputLeakAction.BLOCK))
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_tool_call_response('{"api_key": "sk-abcdefghijklmnop0123456789"}')
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [WEATHER_TOOL],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "output_leak_detected"

    @pytest.mark.asyncio
    @respx.mock
    async def test_redact_category_escalates_to_block_for_tool_call_match(self, client, ol_override):
        # REDACT isn't supported inside tool-call arguments -- see
        # app.guardrails.output_leak's module docstring -- so a
        # REDACT-configured category matching there must BLOCK, not
        # silently redact (which would risk producing invalid JSON) and
        # not silently pass through unflagged either.
        ol_override(_ol_config(secret_token=OutputLeakAction.REDACT))
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_tool_call_response('{"api_key": "sk-abcdefghijklmnop0123456789"}')
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [WEATHER_TOOL],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "output_leak_detected"

    @pytest.mark.asyncio
    @respx.mock
    async def test_flag_category_passes_through_tool_calls_unmodified(self, client, ol_override):
        ol_override(_ol_config(secret_token=OutputLeakAction.FLAG))
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_tool_call_response('{"api_key": "sk-abcdefghijklmnop0123456789"}')
            )
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [WEATHER_TOOL],
            },
        )
        assert response.status_code == 200
        message = response.json()["choices"][0]["message"]
        # Unmodified -- FLAG only logs/records, never mutates the response.
        assert json.loads(message["tool_calls"][0]["function"]["arguments"])["api_key"] == "sk-abcdefghijklmnop0123456789"

    @pytest.mark.asyncio
    @respx.mock
    async def test_redact_on_content_preserves_clean_tool_calls(self, client, ol_override):
        # A REDACT match in `content` must not touch tool_calls that have
        # no match of their own -- see redact_message_content's docstring.
        ol_override(_ol_config(email=OutputLeakAction.REDACT))
        body = _tool_call_response("{}")
        body["choices"][0]["message"]["content"] = "contact jane.doe@example.com, and also see the tool call"
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [WEATHER_TOOL],
            },
        )
        assert response.status_code == 200
        message = response.json()["choices"][0]["message"]
        assert "jane.doe@example.com" not in message["content"]
        assert "[EMAIL]" in message["content"]
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"


class TestOutputLeakStreamingToolCalls:
    @pytest.mark.asyncio
    @respx.mock
    async def test_buffered_block_on_streamed_tool_call_arguments(self, client, ol_override):
        ol_override(_ol_config(secret_token=OutputLeakAction.BLOCK))
        chunks = _tool_call_delta_chunks(['{"api_key": "sk-abcdefghijklmnop0123456789"}'])
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, content=_sse_body(*chunks, "[DONE]")))

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [WEATHER_TOOL],
                "stream": True,
            },
        )
        assert response.status_code == 200
        body = response.text
        assert "output_leak_detected" in body
        assert body.endswith("data: [DONE]\n\n")

    @pytest.mark.asyncio
    @respx.mock
    async def test_buffered_redact_on_content_reconstructs_clean_tool_calls(self, client, ol_override):
        # The REDACT path collapses the stream into one synthetic chunk --
        # it must not silently drop tool_calls the model actually made
        # (see _with_output_leak_scan's REDACT branch).
        ol_override(_ol_config(email=OutputLeakAction.REDACT))
        content_chunk = _content_chunk("email me at jane.doe@example.com")
        tool_call_chunks = _tool_call_delta_chunks(['{"location": "Paris"}'])
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(200, content=_sse_body(content_chunk, *tool_call_chunks, "[DONE]"))
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "call the tool"}],
                "tools": [WEATHER_TOOL],
                "stream": True,
            },
        )
        assert response.status_code == 200
        body = response.text

        reconstructed_content = ""
        tool_calls_seen = None
        for line in body.split("\n\n"):
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            payload = json.loads(line.removeprefix("data: "))
            delta = payload["choices"][0]["delta"]
            reconstructed_content += delta.get("content") or ""
            if delta.get("tool_calls"):
                tool_calls_seen = delta["tool_calls"]

        assert "jane.doe@example.com" not in reconstructed_content
        assert "[EMAIL]" in reconstructed_content
        assert tool_calls_seen is not None
        assert tool_calls_seen[0]["function"]["name"] == "get_weather"
        assert json.loads(tool_calls_seen[0]["function"]["arguments"]) == {"location": "Paris"}
