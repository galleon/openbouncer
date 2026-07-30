import asyncio
import enum
import json
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import lru_cache

import httpx
from nemoguardrails import LLMRails, RailsConfig

from app.core.errors import OpenAIError, format_sse_error, normalize_upstream_error
from app.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)

DEFAULT_NEMO_BASE_URL = "http://localhost:8000/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_NEMO_LIBRARY_CONFIG_PATH = "./guardrails_configs"

MODE_ENV_VAR = "GUARDRAILS_MODE"
NEMO_BASE_URL_ENV_VAR = "GUARDRAILS_NEMO_BASE_URL"
# Alternate, deployment-facing name for NEMO_BASE_URL_ENV_VAR (used in the
# Docker/compose config surface). Both are honored; NEMO_BASE_URL_ENV_VAR
# wins if both happen to be set.
ALT_NEMO_BASE_URL_ENV_VAR = "NEMO_GUARDRAILS_BASE_URL"
NEMO_DEFAULT_CONFIG_ID_ENV_VAR = "GUARDRAILS_NEMO_DEFAULT_CONFIG_ID"
NEMO_TIMEOUT_ENV_VAR = "GUARDRAILS_NEMO_TIMEOUT_SECONDS"
NEMO_LIBRARY_CONFIG_PATH_ENV_VAR = "GUARDRAILS_NEMO_LIBRARY_CONFIG_PATH"

CONFIG_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class GuardrailsMode(str, enum.Enum):
    DISABLED = "disabled"
    NEMO_LIBRARY = "nemo_library"
    NEMO_MICROSERVICE = "nemo_microservice"


@dataclass(frozen=True)
class GuardrailsConfig:
    mode: GuardrailsMode
    nemo_base_url: str = DEFAULT_NEMO_BASE_URL
    nemo_default_config_id: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    nemo_library_config_path: str = DEFAULT_NEMO_LIBRARY_CONFIG_PATH


def load_guardrails_config() -> GuardrailsConfig:
    raw_mode = os.environ.get(MODE_ENV_VAR, GuardrailsMode.DISABLED.value)
    try:
        mode = GuardrailsMode(raw_mode)
    except ValueError:
        allowed = ", ".join(m.value for m in GuardrailsMode)
        raise ValueError(
            f"Invalid {MODE_ENV_VAR} '{raw_mode}'. Must be one of: {allowed}"
        ) from None

    nemo_base_url = (
        os.environ.get(NEMO_BASE_URL_ENV_VAR)
        or os.environ.get(ALT_NEMO_BASE_URL_ENV_VAR)
        or DEFAULT_NEMO_BASE_URL
    )

    return GuardrailsConfig(
        mode=mode,
        nemo_base_url=nemo_base_url,
        nemo_default_config_id=os.environ.get(NEMO_DEFAULT_CONFIG_ID_ENV_VAR),
        timeout_seconds=float(
            os.environ.get(NEMO_TIMEOUT_ENV_VAR, str(DEFAULT_TIMEOUT_SECONDS))
        ),
        nemo_library_config_path=os.environ.get(
            NEMO_LIBRARY_CONFIG_PATH_ENV_VAR, DEFAULT_NEMO_LIBRARY_CONFIG_PATH
        ),
    )


def _extract_text(content: str | list) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if part.type == "text")


def _count_tokens(text: str) -> int:
    return len(text.split())


class GuardrailsService(ABC):
    """Gates/routes chat completion requests through a guardrails backend.

    process_chat_completion returns None when the request should pass
    through unchanged and be sent to the upstream LLM as usual (e.g.
    disabled mode). It returns a ChatCompletionResponse when the guardrails
    backend already produced the final response itself, because it called
    the underlying LLM on our behalf (e.g. the NeMo microservice or the
    in-process library) -- in that case the caller must not call the
    upstream LLM again.

    stream_chat_completion is the streaming counterpart: None means pass
    through to the upstream LLM's own streaming, otherwise it returns an
    async iterator of SSE `data: ...\\n\\n` frames ending in `data: [DONE]\\n\\n`
    -- the exact same contract as UpstreamClient.stream_chat_completion, so
    callers can relay either one the same way. The default implementation
    here raises, for backends that don't support streaming yet.
    """

    @abstractmethod
    async def process_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | None:
        raise NotImplementedError

    async def stream_chat_completion(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[str] | None:
        raise OpenAIError(
            "Streaming is not supported yet for this guardrails mode.",
            status_code=400,
            param="stream",
        )


class DisabledGuardrailsService(GuardrailsService):
    async def process_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | None:
        return None

    async def stream_chat_completion(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[str] | None:
        return None


class NemoMicroserviceGuardrailsService(GuardrailsService):
    """Calls a NeMo Guardrails Microservice's OpenAI-compatible
    /v1/chat/completions endpoint (nvcr.io/nvidia/nemo-microservices/guardrails),
    passing the request's guardrails.config_id through. The microservice
    calls the underlying LLM itself according to that config, so its
    response is the final response -- non-streaming only for now.
    """

    def __init__(
        self,
        *,
        base_url: str,
        default_config_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._default_config_id = default_config_id
        self._timeout = timeout
        self._external_client = http_client

    async def process_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        if request.stream:
            raise OpenAIError(
                "Streaming is not supported yet when guardrails are enabled.",
                status_code=400,
                param="stream",
            )

        config_id = (request.guardrails.config_id if request.guardrails else None) or (
            self._default_config_id
        )

        payload = request.model_dump(mode="json", exclude_none=True, exclude={"guardrails"})
        payload["stream"] = False
        if config_id:
            payload["guardrails"] = {"config_id": config_id}

        url = f"{self._base_url.rstrip('/')}/chat/completions"

        client = self._external_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient()

        try:
            try:
                response = await client.post(url, json=payload, timeout=self._timeout)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                raise OpenAIError(
                    f"Guardrails service request failed: {exc}",
                    status_code=503,
                    error_type="api_error",
                    code="guardrails_unavailable",
                ) from exc
        finally:
            if owns_client:
                await client.aclose()

        return self._parse_response(response)

    def _parse_response(self, response: httpx.Response) -> ChatCompletionResponse:
        if response.status_code >= 400:
            raise normalize_upstream_error(response.status_code, response.content)

        try:
            data = response.json()
        except ValueError as exc:
            raise OpenAIError(
                "Guardrails service returned a non-JSON response.",
                status_code=502,
                error_type="api_error",
                code="invalid_guardrails_response",
            ) from exc

        try:
            return ChatCompletionResponse.model_validate(data)
        except Exception as exc:
            raise OpenAIError(
                "Guardrails service response did not match the expected schema.",
                status_code=502,
                error_type="api_error",
                code="invalid_guardrails_response",
            ) from exc


def _to_nemo_messages(messages: list[ChatMessage]) -> list[dict]:
    # nemoguardrails' Colang flows and dialog rails operate on plain text, so
    # any image_url content parts are dropped here -- guardrails-library mode
    # is text-only. Also note nemoguardrails itself does not yet support
    # "system" role messages (see LLMRails.generate_async docstring); they
    # are still passed through as-is since we have no well-defined mapping
    # for them, and this matches the library's own documented limitation
    # rather than adding one of our own.
    return [{"role": m.role, "content": _extract_text(m.content)} for m in messages]


def _has_output_rails(config: RailsConfig) -> bool:
    return len(config.rails.output.flows) > 0


def _bot_message_content(bot_message: object) -> str:
    if isinstance(bot_message, dict):
        return bot_message.get("content") or ""
    return str(bot_message)


def _bot_message_to_chat_completion(
    bot_message: object, *, model: str, nemo_messages: list[dict]
) -> ChatCompletionResponse:
    content = _bot_message_content(bot_message)

    prompt_tokens = sum(_count_tokens(m["content"]) for m in nemo_messages)
    completion_tokens = _count_tokens(content)

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# nemoguardrails' stream_async() does not raise when the underlying
# generation fails (LLM call error, a blocking rail, etc.) -- instead,
# LLMRails._generation_task catches the exception and pushes a single chunk
# shaped like {"error": {"message": "..."}} (via
# nemoguardrails.utils.extract_error_json) before ending the stream. As of
# nemoguardrails 0.23.0 there is no "type"/"code" marker to disambiguate this
# from bot-generated text that happens to look the same (a `develop`-branch
# revision adds one -- STREAM_ERROR_TYPES / build_streaming_error_payload --
# but it had not been released at the time this was written), so this is a
# best-effort heuristic: treat any single chunk that parses as
# {"error": {"message": <str>}} as nemoguardrails signaling a failure rather
# than as literal content. Revisit this once a typed marker ships upstream.
def _nemo_stream_error_frame(token: str) -> str | None:
    try:
        parsed = json.loads(token)
    except (ValueError, TypeError):
        return None

    error_obj = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error_obj, dict) or not isinstance(error_obj.get("message"), str):
        return None

    return format_sse_error(
        OpenAIError(
            error_obj["message"],
            status_code=502,
            error_type="api_error",
            code=error_obj.get("code"),
        )
    )


def _sse_chunk(
    response_id: str,
    created: int,
    model: str,
    *,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


class NemoLibraryGuardrailsService(GuardrailsService):
    """Runs NeMo Guardrails in-process via the nemoguardrails Python library
    (https://github.com/NVIDIA-NeMo/Guardrails), instead of a separate
    microservice.

    Each config_id is a subdirectory of `config_store_path` containing its
    own config.yml (and optional Colang flows) -- the same multi-config
    layout used by `nemoguardrails server` / the microservice container, so
    the same configs can be reused across modes. Loaded RailsConfig/LLMRails
    instances are cached by config_id, since constructing one parses Colang
    flows, validates rails, and can initialize embeddings for the knowledge
    base -- too expensive to redo on every request.

    STREAMING LIMITATION (see also README): nemoguardrails can only stream
    the main LLM's tokens directly to the caller when the config has no
    output rails. Output rails need the complete bot message to check it
    (e.g. "did this response violate policy"), so there is nothing to check
    incrementally -- the whole message must be generated and validated
    before we know whether/what to send back. Concretely:

    - No output rails configured: true streaming. Tokens are forwarded to
      the caller as nemoguardrails produces them (input/dialog rails already
      ran before the first token, since dialog management happens before
      the main generation call).
    - Output rails configured: buffered mode. We call the normal buffered
      generate_async(), wait for the complete (possibly rewritten or
      refused) response, and then present it to the caller as a single SSE
      chunk followed by the terminal [DONE] -- i.e. it is a streaming
      response in shape only, not in latency.
    """

    def __init__(
        self,
        *,
        config_store_path: str,
        default_config_id: str | None = None,
        llm_factory: Callable[[], object] | None = None,
    ) -> None:
        self._config_store_path = config_store_path
        self._default_config_id = default_config_id
        self._llm_factory = llm_factory
        self._rails_cache: dict[str, LLMRails] = {}
        self._load_lock = asyncio.Lock()

    def _resolve_config_id(self, request: ChatCompletionRequest) -> str:
        config_id = (request.guardrails.config_id if request.guardrails else None) or (
            self._default_config_id
        )
        if not config_id:
            raise OpenAIError(
                "No guardrails config_id provided and no default is configured.",
                status_code=422,
                error_type="invalid_request_error",
                param="guardrails.config_id",
                code="missing_config_id",
            )
        if not CONFIG_ID_PATTERN.match(config_id):
            raise OpenAIError(
                f"Invalid guardrails config_id `{config_id}`.",
                status_code=422,
                error_type="invalid_request_error",
                param="guardrails.config_id",
                code="invalid_config_id",
            )
        return config_id

    async def _get_rails(self, config_id: str) -> LLMRails:
        rails = self._rails_cache.get(config_id)
        if rails is not None:
            return rails

        async with self._load_lock:
            # Another request may have loaded it while we were waiting.
            rails = self._rails_cache.get(config_id)
            if rails is not None:
                return rails
            rails = await asyncio.to_thread(self._load_rails, config_id)
            self._rails_cache[config_id] = rails
            return rails

    def _load_rails(self, config_id: str) -> LLMRails:
        # config_id is validated against CONFIG_ID_PATTERN before this is
        # called, but resolve-and-check-containment anyway as defense in
        # depth against path traversal, since it ultimately comes from the
        # request body.
        store_root = os.path.realpath(self._config_store_path)
        config_path = os.path.realpath(os.path.join(store_root, config_id))

        is_contained = (
            os.path.commonpath([store_root, config_path]) == store_root
        )
        if not is_contained or not os.path.isdir(config_path):
            raise OpenAIError(
                f"Unknown guardrails config_id `{config_id}`.",
                status_code=404,
                error_type="invalid_request_error",
                param="guardrails.config_id",
                code="guardrails_config_not_found",
            )

        try:
            config = RailsConfig.from_path(config_path)
            llm = self._llm_factory() if self._llm_factory else None
            return LLMRails(config, llm=llm)
        except Exception as exc:
            raise OpenAIError(
                f"Failed to load guardrails config `{config_id}`: {exc}",
                status_code=500,
                error_type="api_error",
                code="guardrails_config_invalid",
            ) from exc

    async def process_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        if request.stream:
            raise OpenAIError(
                "process_chat_completion does not support streaming; "
                "use stream_chat_completion instead.",
                status_code=400,
                param="stream",
            )

        config_id = self._resolve_config_id(request)
        rails = await self._get_rails(config_id)
        nemo_messages = _to_nemo_messages(request.messages)

        try:
            bot_message = await rails.generate_async(messages=nemo_messages)
        except Exception as exc:
            raise OpenAIError(
                f"Guardrails generation failed: {exc}",
                status_code=500,
                error_type="api_error",
                code="guardrails_generation_failed",
            ) from exc

        return _bot_message_to_chat_completion(
            bot_message, model=request.model, nemo_messages=nemo_messages
        )

    async def stream_chat_completion(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[str]:
        # Plain coroutine (not itself a generator): resolves config_id and
        # loads/caches the rails first, so a bad config_id or a load failure
        # raises right here rather than on first iteration -- matching the
        # GuardrailsService contract (an awaited call returns an iterator or
        # None, it never *is* the iterator), which lets callers like the
        # chat route treat every backend's stream_chat_completion the same
        # way regardless of whether it's DisabledGuardrailsService (a plain
        # coroutine) or this one.
        config_id = self._resolve_config_id(request)
        rails = await self._get_rails(config_id)
        nemo_messages = _to_nemo_messages(request.messages)
        return self._stream_response(rails, nemo_messages, request.model)

    async def _stream_response(
        self, rails: LLMRails, nemo_messages: list[dict], model: str
    ) -> AsyncIterator[str]:
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        if _has_output_rails(rails.config):
            # Buffered output-rails mode -- see class docstring and README.
            try:
                bot_message = await rails.generate_async(messages=nemo_messages)
            except Exception as exc:
                raise OpenAIError(
                    f"Guardrails generation failed: {exc}",
                    status_code=500,
                    error_type="api_error",
                    code="guardrails_generation_failed",
                ) from exc

            content = _bot_message_content(bot_message)
            yield _sse_chunk(response_id, created, model, delta={"content": content})
            yield _sse_chunk(response_id, created, model, delta={}, finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        # Input-rails-only true streaming -- see class docstring and README.
        token_iterator = rails.stream_async(messages=nemo_messages)

        try:
            first_token = await token_iterator.__anext__()
        except StopAsyncIteration:
            yield _sse_chunk(response_id, created, model, delta={}, finish_reason="stop")
            yield "data: [DONE]\n\n"
            return
        # Any other exception here happens before we've yielded anything to
        # our caller, so it is safe to let it propagate as a normal error
        # instead of an in-band SSE error frame.

        error_frame = _nemo_stream_error_frame(first_token)
        if error_frame is not None:
            yield error_frame
            yield "data: [DONE]\n\n"
            return

        yield _sse_chunk(response_id, created, model, delta={"content": first_token})

        try:
            async for token in token_iterator:
                error_frame = _nemo_stream_error_frame(token)
                if error_frame is not None:
                    yield error_frame
                    yield "data: [DONE]\n\n"
                    return
                yield _sse_chunk(response_id, created, model, delta={"content": token})
        except Exception as exc:
            yield format_sse_error(
                OpenAIError(
                    f"Guardrails streaming was interrupted: {exc}",
                    status_code=502,
                    error_type="api_error",
                    code="guardrails_stream_interrupted",
                )
            )
            yield "data: [DONE]\n\n"
            return

        yield _sse_chunk(response_id, created, model, delta={}, finish_reason="stop")
        yield "data: [DONE]\n\n"


def create_guardrails_service(config: GuardrailsConfig | None = None) -> GuardrailsService:
    config = config or load_guardrails_config()

    if config.mode is GuardrailsMode.DISABLED:
        return DisabledGuardrailsService()
    if config.mode is GuardrailsMode.NEMO_MICROSERVICE:
        return NemoMicroserviceGuardrailsService(
            base_url=config.nemo_base_url,
            default_config_id=config.nemo_default_config_id,
            timeout=config.timeout_seconds,
        )
    if config.mode is GuardrailsMode.NEMO_LIBRARY:
        return NemoLibraryGuardrailsService(
            config_store_path=config.nemo_library_config_path,
            default_config_id=config.nemo_default_config_id,
        )

    raise ValueError(f"Unsupported guardrails mode: {config.mode}")


@lru_cache
def get_guardrails_service() -> GuardrailsService:
    return create_guardrails_service()
