import json
from pathlib import Path

import pytest
from nemoguardrails.testing import FakeLLMModel

from app.core.errors import OpenAIError
from app.guardrails.service import NemoLibraryGuardrailsService
from app.schemas.chat import ChatCompletionRequest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "guardrails_configs"


def _request(config_id: str | None = None, **overrides) -> ChatCompletionRequest:
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    if config_id is not None:
        payload["guardrails"] = {"config_id": config_id}
    payload.update(overrides)
    return ChatCompletionRequest(**payload)


def _service(responses=None, llm_exception=None, **kwargs) -> NemoLibraryGuardrailsService:
    def factory():
        return FakeLLMModel(responses=responses, llm_exception=llm_exception)

    return NemoLibraryGuardrailsService(
        config_store_path=str(FIXTURES_DIR), llm_factory=factory, **kwargs
    )


class TestConfigIdResolution:
    @pytest.mark.asyncio
    async def test_missing_config_id_rejected(self):
        service = _service(responses=["hi"])
        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(_request())
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "missing_config_id"

    @pytest.mark.asyncio
    async def test_default_config_id_used_when_request_omits_it(self):
        service = _service(responses=["Hello from default!"], default_config_id="no_rails")
        result = await service.process_chat_completion(_request())
        assert result.choices[0].message.content == "Hello from default!"

    @pytest.mark.asyncio
    async def test_unknown_config_id_rejected(self):
        service = _service(responses=["hi"])
        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(_request(config_id="does_not_exist"))
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "guardrails_config_not_found"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", ["../evil", "no_rails/../../etc", "a b", "a/b"])
    async def test_path_traversal_and_invalid_config_ids_rejected(self, bad_id):
        service = _service(responses=["hi"])
        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(_request(config_id=bad_id))
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "invalid_config_id"


class TestRailsCaching:
    @pytest.mark.asyncio
    async def test_same_config_id_reuses_cached_rails(self):
        service = _service(responses=["a", "b"])
        rails_first = await service._get_rails("no_rails")
        rails_second = await service._get_rails("no_rails")
        assert rails_first is rails_second

    @pytest.mark.asyncio
    async def test_different_config_ids_get_different_rails(self):
        service = _service(responses=["a", "b", "c"])
        no_rails = await service._get_rails("no_rails")
        self_check = await service._get_rails("self_check_output")
        assert no_rails is not self_check


class TestNonStreamingGeneration:
    @pytest.mark.asyncio
    async def test_no_rails_config_returns_bot_reply(self):
        service = _service(responses=["Hello there!"])
        result = await service.process_chat_completion(
            _request(config_id="no_rails", model="my-model")
        )
        assert result.object == "chat.completion"
        assert result.model == "my-model"
        assert result.choices[0].message.role == "assistant"
        assert result.choices[0].message.content == "Hello there!"
        assert result.choices[0].finish_reason == "stop"
        assert result.usage.total_tokens > 0

    @pytest.mark.asyncio
    async def test_output_rail_passes_through_allowed_content(self):
        service = _service(responses=["Hello there!", "No"])
        result = await service.process_chat_completion(_request(config_id="self_check_output"))
        assert result.choices[0].message.content == "Hello there!"

    @pytest.mark.asyncio
    async def test_output_rail_blocks_disallowed_content(self):
        service = _service(responses=["Some bad message", "Yes"])
        result = await service.process_chat_completion(_request(config_id="self_check_output"))
        assert "sorry" in result.choices[0].message.content.lower()

    @pytest.mark.asyncio
    async def test_generation_failure_is_normalized(self):
        service = _service(llm_exception=RuntimeError("upstream is down"))
        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(_request(config_id="no_rails"))
        assert exc_info.value.status_code == 500
        assert exc_info.value.code == "guardrails_generation_failed"

    @pytest.mark.asyncio
    async def test_stream_flag_rejected_by_non_streaming_method(self):
        service = _service(responses=["hi"])
        with pytest.raises(OpenAIError) as exc_info:
            await service.process_chat_completion(
                _request(config_id="no_rails", stream=True)
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.param == "stream"


class TestStreaming:
    @pytest.mark.asyncio
    async def test_no_output_rails_streams_multiple_content_chunks(self):
        service = _service(responses=["Hello there friend!"])
        agen = await service.stream_chat_completion(
            _request(config_id="no_rails", stream=True)
        )
        chunks = [chunk async for chunk in agen]

        assert chunks[-1] == "data: [DONE]\n\n"
        content_chunks = [c for c in chunks[:-1]]
        assert len(content_chunks) > 2, "expected true token-by-token streaming"

        reconstructed = ""
        for raw in content_chunks:
            payload = json.loads(raw.removeprefix("data: ").strip())
            delta = payload["choices"][0]["delta"]
            reconstructed += delta.get("content", "")
        assert reconstructed.strip() == "Hello there friend!"

        final_payload = json.loads(content_chunks[-1].removeprefix("data: ").strip())
        assert final_payload["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_output_rails_configured_uses_single_buffered_chunk(self):
        service = _service(responses=["Hello there!", "No"])
        agen = await service.stream_chat_completion(
            _request(config_id="self_check_output", stream=True)
        )
        chunks = [chunk async for chunk in agen]

        assert chunks[-1] == "data: [DONE]\n\n"
        content_chunks = [
            c
            for c in chunks[:-1]
            if json.loads(c.removeprefix("data: ").strip())["choices"][0]["delta"]
        ]
        assert len(content_chunks) == 1, "buffered mode should emit exactly one content chunk"

        payload = json.loads(content_chunks[0].removeprefix("data: ").strip())
        assert payload["choices"][0]["delta"]["content"] == "Hello there!"

    @pytest.mark.asyncio
    async def test_output_rails_blocking_still_uses_buffered_mode(self):
        service = _service(responses=["Some bad message", "Yes"])
        agen = await service.stream_chat_completion(
            _request(config_id="self_check_output", stream=True)
        )
        chunks = [chunk async for chunk in agen]
        payload = json.loads(chunks[0].removeprefix("data: ").strip())
        assert "sorry" in payload["choices"][0]["delta"]["content"].lower()

    @pytest.mark.asyncio
    async def test_true_streaming_failure_emits_sse_error_then_done(self):
        service = _service(llm_exception=RuntimeError("upstream is down"))
        agen = await service.stream_chat_completion(
            _request(config_id="no_rails", stream=True)
        )
        chunks = [chunk async for chunk in agen]

        assert chunks[-1] == "data: [DONE]\n\n"
        error_payload = json.loads(chunks[0].removeprefix("data: ").strip())
        assert error_payload["error"]["type"] == "api_error"
        assert "upstream is down" in error_payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_buffered_mode_generation_failure_raises_before_first_chunk(self):
        service = _service(llm_exception=RuntimeError("upstream is down"))
        agen = await service.stream_chat_completion(
            _request(config_id="self_check_output", stream=True)
        )
        with pytest.raises(OpenAIError) as exc_info:
            await agen.__anext__()
        assert exc_info.value.status_code == 500
        assert exc_info.value.code == "guardrails_generation_failed"

    @pytest.mark.asyncio
    async def test_unknown_config_id_raises_before_returning_a_generator(self):
        service = _service(responses=["hi"])
        with pytest.raises(OpenAIError) as exc_info:
            await service.stream_chat_completion(
                _request(config_id="does_not_exist", stream=True)
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.code == "guardrails_config_not_found"


class TestStreamingUsageEstimate:
    """nemoguardrails exposes no real per-token usage through either
    generate_async() or stream_async() -- usage_holder is filled in with a
    word-count estimate instead (same heuristic
    _bot_message_to_chat_completion already uses for the non-streaming
    path). See NemoLibraryGuardrailsService._stream_response."""

    @pytest.mark.asyncio
    async def test_true_streaming_records_word_count_estimate(self):
        service = _service(responses=["Hello there friend"])
        usage_holder: dict[str, int] = {}
        agen = await service.stream_chat_completion(
            _request(config_id="no_rails", stream=True), usage_holder=usage_holder
        )
        [_ async for _ in agen]

        assert usage_holder["prompt_tokens"] == 1  # "hi"
        assert usage_holder["completion_tokens"] == 3  # "Hello there friend"
        assert usage_holder["total_tokens"] == 4

    @pytest.mark.asyncio
    async def test_buffered_output_rails_records_word_count_estimate(self):
        service = _service(responses=["Hello there!", "No"])
        usage_holder: dict[str, int] = {}
        agen = await service.stream_chat_completion(
            _request(config_id="self_check_output", stream=True), usage_holder=usage_holder
        )
        [_ async for _ in agen]

        assert usage_holder["completion_tokens"] == 2  # "Hello there!"
        assert usage_holder["total_tokens"] == usage_holder["prompt_tokens"] + 2

    @pytest.mark.asyncio
    async def test_no_usage_holder_is_a_no_op(self):
        # Default usage_holder=None -- must not raise.
        service = _service(responses=["Hello there friend"])
        agen = await service.stream_chat_completion(_request(config_id="no_rails", stream=True))
        [_ async for _ in agen]

    @pytest.mark.asyncio
    async def test_interrupted_stream_leaves_usage_holder_untouched(self):
        service = _service(llm_exception=RuntimeError("upstream is down"))
        usage_holder: dict[str, int] = {}
        agen = await service.stream_chat_completion(
            _request(config_id="no_rails", stream=True), usage_holder=usage_holder
        )
        [_ async for _ in agen]

        # The error-frame path never produced a real completion -- no
        # fabricated success-shaped usage numbers.
        assert usage_holder == {}
