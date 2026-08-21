import json

import httpx
import pytest
import respx

from app.core.errors import OpenAIError
from app.guardrails.service import (
    MODE_ENV_VAR,
    NEMO_BASE_URL_ENV_VAR,
    NEMO_DEFAULT_CONFIG_ID_ENV_VAR,
    NEMO_TIMEOUT_ENV_VAR,
    DisabledGuardrailsService,
    GuardrailsConfig,
    GuardrailsMode,
    NemoLibraryGuardrailsService,
    NemoMicroserviceGuardrailsService,
    create_guardrails_service,
    load_guardrails_config,
)
from app.schemas.chat import ChatCompletionRequest

BASE_URL = "http://localhost:8000/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


def _success_response(model: str = "meta/llama-3.1-8b-instruct") -> dict:
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
        "guardrails": {
            "config_id": "content_safety",
            "state": None,
            "llm_output": None,
            "output_data": None,
            "log": None,
        },
    }


class TestDisabledGuardrailsService:
    @pytest.mark.asyncio
    async def test_passes_through(self):
        service = DisabledGuardrailsService()
        request = ChatCompletionRequest(
            model="nvidia/qwen3.6-nvfp4", messages=[{"role": "user", "content": "hi"}]
        )
        result = await service.process_chat_completion(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_stream_passes_through(self):
        service = DisabledGuardrailsService()
        request = ChatCompletionRequest(
            model="nvidia/qwen3.6-nvfp4",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        result = await service.stream_chat_completion(request)
        assert result is None


class TestGuardrailsServiceStreamingDefault:
    @pytest.mark.asyncio
    async def test_backend_without_streaming_support_rejects_it(self):
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)
        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        with pytest.raises(OpenAIError) as exc_info:
            await service.stream_chat_completion(request)
        assert exc_info.value.status_code == 400
        assert exc_info.value.param == "stream"


class TestGuardrailsConfigLoading:
    def test_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv(MODE_ENV_VAR, raising=False)
        monkeypatch.delenv(NEMO_BASE_URL_ENV_VAR, raising=False)
        monkeypatch.delenv(NEMO_DEFAULT_CONFIG_ID_ENV_VAR, raising=False)
        monkeypatch.delenv(NEMO_TIMEOUT_ENV_VAR, raising=False)

        config = load_guardrails_config()

        assert config.mode is GuardrailsMode.DISABLED
        assert config.nemo_base_url == "http://localhost:8000/v1"
        assert config.nemo_default_config_id is None
        assert config.timeout_seconds == 30.0

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv(MODE_ENV_VAR, "nemo_microservice")
        monkeypatch.setenv(NEMO_BASE_URL_ENV_VAR, "http://nemo-guardrails:9000/v1")
        monkeypatch.setenv(NEMO_DEFAULT_CONFIG_ID_ENV_VAR, "content_safety")
        monkeypatch.setenv(NEMO_TIMEOUT_ENV_VAR, "5.5")

        config = load_guardrails_config()

        assert config.mode is GuardrailsMode.NEMO_MICROSERVICE
        assert config.nemo_base_url == "http://nemo-guardrails:9000/v1"
        assert config.nemo_default_config_id == "content_safety"
        assert config.timeout_seconds == 5.5

    def test_invalid_mode_rejected(self, monkeypatch):
        monkeypatch.setenv(MODE_ENV_VAR, "bogus_mode")
        with pytest.raises(ValueError, match="Invalid GUARDRAILS_MODE"):
            load_guardrails_config()


class TestGuardrailsServiceFactory:
    def test_disabled_mode_builds_disabled_service(self):
        service = create_guardrails_service(GuardrailsConfig(mode=GuardrailsMode.DISABLED))
        assert isinstance(service, DisabledGuardrailsService)

    def test_nemo_microservice_mode_builds_configured_service(self):
        config = GuardrailsConfig(
            mode=GuardrailsMode.NEMO_MICROSERVICE,
            nemo_base_url="http://nemo:8000/v1",
            nemo_default_config_id="content_safety",
            timeout_seconds=12.0,
        )
        service = create_guardrails_service(config)
        assert isinstance(service, NemoMicroserviceGuardrailsService)
        assert service._base_url == "http://nemo:8000/v1"
        assert service._default_config_id == "content_safety"
        assert service._timeout == 12.0

    def test_nemo_library_mode_builds_library_service(self):
        config = GuardrailsConfig(
            mode=GuardrailsMode.NEMO_LIBRARY,
            nemo_library_config_path="/some/configs",
            nemo_default_config_id="no_rails",
        )
        service = create_guardrails_service(config)
        assert isinstance(service, NemoLibraryGuardrailsService)
        assert service._config_store_path == "/some/configs"
        assert service._default_config_id == "no_rails"


class TestNemoLibraryModeWithoutTheOptionalPackage:
    """Simulates `nemoguardrails` not being installed (the `nemo` extra
    omitted, see pyproject.toml) by patching the module's own RailsConfig
    binding to None -- the same state app.guardrails.service ends up in
    when the real `import nemoguardrails` fails, without needing to
    actually uninstall the package for this test."""

    def test_nemo_library_mode_raises_a_clear_actionable_error(self, monkeypatch):
        import app.guardrails.service as service_module

        monkeypatch.setattr(service_module, "RailsConfig", None)
        config = GuardrailsConfig(mode=GuardrailsMode.NEMO_LIBRARY)

        with pytest.raises(OpenAIError) as exc_info:
            create_guardrails_service(config)

        assert exc_info.value.status_code == 500
        assert exc_info.value.error_type == "api_error"
        assert exc_info.value.code == "nemo_library_not_installed"
        assert "uv sync --extra nemo" in exc_info.value.message

    def test_disabled_and_nemo_microservice_modes_are_unaffected(self, monkeypatch):
        import app.guardrails.service as service_module

        monkeypatch.setattr(service_module, "RailsConfig", None)
        monkeypatch.setattr(service_module, "LLMRails", None)

        assert isinstance(
            create_guardrails_service(GuardrailsConfig(mode=GuardrailsMode.DISABLED)),
            DisabledGuardrailsService,
        )
        assert isinstance(
            create_guardrails_service(GuardrailsConfig(mode=GuardrailsMode.NEMO_MICROSERVICE)),
            NemoMicroserviceGuardrailsService,
        )


class TestNemoMicroserviceGuardrailsService:
    @pytest.mark.asyncio
    @respx.mock
    async def test_calls_endpoint_with_request_config_id(self):
        route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))

        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "Hello!"}],
            guardrails={"config_id": "content_safety"},
        )
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

        result = await service.process_chat_completion(request)

        assert route.called
        sent_payload = route.calls.last.request.content
        payload = json.loads(sent_payload)
        assert payload["model"] == "meta/llama-3.1-8b-instruct"
        assert payload["messages"] == [{"role": "user", "content": "Hello!"}]
        assert payload["stream"] is False
        assert payload["guardrails"] == {"config_id": "content_safety"}

        assert result.id == "chatcmpl-abc123"
        assert result.choices[0].message.content == "Hello!"

    @pytest.mark.asyncio
    @respx.mock
    async def test_falls_back_to_default_config_id(self):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))

        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "Hello!"}],
        )
        service = NemoMicroserviceGuardrailsService(
            base_url=BASE_URL, default_config_id="content_safety"
        )

        await service.process_chat_completion(request)

        payload = json.loads(respx.calls.last.request.content)
        assert payload["guardrails"] == {"config_id": "content_safety"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_omits_guardrails_field_when_no_config_id_available(self):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_success_response()))

        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "Hello!"}],
        )
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

        await service.process_chat_completion(request)

        payload = json.loads(respx.calls.last.request.content)
        assert "guardrails" not in payload

    @pytest.mark.asyncio
    async def test_rejects_streaming_without_calling_upstream(self):
        with respx.mock:
            route = respx.post(CHAT_URL).mock(
                return_value=httpx.Response(200, json=_success_response())
            )
            request = ChatCompletionRequest(
                model="meta/llama-3.1-8b-instruct",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

            with pytest.raises(OpenAIError) as exc_info:
                await service.process_chat_completion(request)

            assert exc_info.value.status_code == 400
            assert exc_info.value.param == "stream"
            assert not route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_normalizes_error_response(self):
        respx.post(CHAT_URL).mock(
            return_value=httpx.Response(
                422,
                json={
                    "error": {
                        "message": "No guardrails config_id provided",
                        "type": "invalid_request_error",
                        "code": "missing_config_id",
                    }
                },
            )
        )
        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
        )
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(request)

        assert exc_info.value.status_code == 422
        assert exc_info.value.error_type == "invalid_request_error"
        assert exc_info.value.code == "missing_config_id"

    @pytest.mark.asyncio
    @respx.mock
    async def test_normalizes_non_json_response(self):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
        )
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(request)

        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "invalid_guardrails_response"

    @pytest.mark.asyncio
    @respx.mock
    async def test_normalizes_malformed_success_body(self):
        respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
        )
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(request)

        assert exc_info.value.status_code == 502
        assert exc_info.value.code == "invalid_guardrails_response"

    @pytest.mark.asyncio
    @respx.mock
    async def test_connect_error_returns_service_unavailable(self):
        respx.post(CHAT_URL).mock(side_effect=httpx.ConnectError("connection refused"))
        request = ChatCompletionRequest(
            model="meta/llama-3.1-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
        )
        service = NemoMicroserviceGuardrailsService(base_url=BASE_URL)

        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(request)

        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "guardrails_unavailable"
