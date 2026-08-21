import json
from pathlib import Path

import httpx
import pytest
import respx

pytest.importorskip("nemoguardrails")
from nemoguardrails.testing import FakeLLMModel

from app.guardrails.service import GuardrailsService, NemoLibraryGuardrailsService, get_guardrails_service
from app.main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "guardrails_configs"
CHAT_URL = "http://vllm-gemma4:8000/v1/chat/completions"


@pytest.fixture
def guardrails_override():
    """Overrides get_guardrails_service for the duration of one test, using
    the real NemoLibraryGuardrailsService against the tiny test fixture
    configs with a FakeLLMModel -- proves the /v1/chat/completions route
    actually calls into GuardrailsService end-to-end, not just that the
    service class works in isolation.
    """

    def _install(*, responses=None, llm_exception=None) -> NemoLibraryGuardrailsService:
        service = NemoLibraryGuardrailsService(
            config_store_path=str(FIXTURES_DIR),
            llm_factory=lambda: FakeLLMModel(responses=responses, llm_exception=llm_exception),
        )
        app.dependency_overrides[get_guardrails_service] = lambda: service
        return service

    yield _install
    app.dependency_overrides.pop(get_guardrails_service, None)


@pytest.mark.asyncio
async def test_non_streaming_chat_goes_through_guardrails(client, guardrails_override):
    guardrails_override(responses=["Hello from guardrails!"])

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "guardrails": {"config_id": "no_rails"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Hello from guardrails!"


@pytest.mark.asyncio
async def test_non_streaming_chat_blocked_by_output_rail(client, guardrails_override):
    guardrails_override(responses=["Some bad message", "Yes"])

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "guardrails": {"config_id": "self_check_output"},
        },
    )

    assert response.status_code == 200
    assert "sorry" in response.json()["choices"][0]["message"]["content"].lower()


@pytest.mark.asyncio
async def test_streaming_chat_goes_through_guardrails(client, guardrails_override):
    guardrails_override(responses=["Hello streaming friend!"])

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "guardrails": {"config_id": "no_rails"},
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert body.endswith("data: [DONE]\n\n")

    reconstructed = ""
    for line in body.split("\n\n"):
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        payload = json.loads(line.removeprefix("data: "))
        reconstructed += payload["choices"][0]["delta"].get("content", "")
    assert reconstructed.strip() == "Hello streaming friend!"


@pytest.mark.asyncio
async def test_unknown_guardrails_config_id_returns_clean_error(client, guardrails_override):
    guardrails_override(responses=["hi"])

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "guardrails": {"config_id": "does_not_exist"},
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "guardrails_config_not_found"


@pytest.mark.asyncio
async def test_preset_field_accepted_and_ignored_by_guardrails_backend(
    client, guardrails_override
):
    guardrails_override(responses=["Hello from guardrails!"])

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "guardrails": {
                "enabled": True,
                "config_id": "no_rails",
                "preset": "jailbreak-attempt",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hello from guardrails!"


@pytest.mark.asyncio
@respx.mock
async def test_enabled_false_skips_guardrails_entirely(client, monkeypatch):
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "real upstream reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
        )
    )

    class ExplodingGuardrailsService(GuardrailsService):
        async def process_chat_completion(self, request):
            raise AssertionError("guardrails.enabled=false must not call GuardrailsService")

        async def stream_chat_completion(self, request):
            raise AssertionError("guardrails.enabled=false must not call GuardrailsService")

    app.dependency_overrides[get_guardrails_service] = lambda: ExplodingGuardrailsService()
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local/gemma4-nvfp4",
                "messages": [{"role": "user", "content": "hi"}],
                "guardrails": {"enabled": False, "config_id": "no_rails"},
            },
        )
    finally:
        app.dependency_overrides.pop(get_guardrails_service, None)

    # Falls through to the ordinary (non-guardrails) path, which calls the
    # real upstream (mocked here).
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "real upstream reply"
    assert route.called
