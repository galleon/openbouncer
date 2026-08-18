import json
import logging
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.auth.dependency import (
    AuthContext,
    ensure_guardrails_config_allowed,
    ensure_model_allowed,
    require_api_key,
)
from app.auth.usage import SupportsUsageTracking, get_usage_tracker
from app.core.errors import OpenAIError, format_sse_error
from app.core.metrics import (
    CHAT_COMPLETION_DURATION_SECONDS,
    CHAT_COMPLETIONS_TOTAL,
    OUTPUT_LEAK_ACTIONS_TOTAL,
    OUTPUT_LEAK_MATCHES_TOTAL,
    OUTPUT_LEAK_SCANNED_TOTAL,
    PROMPT_INJECTION_ACTIONS_TOTAL,
    PROMPT_INJECTION_MATCHES_TOTAL,
    PROMPT_INJECTION_SCANNED_TOTAL,
)
from app.core.registry import ModelConcurrencyLimiter, ModelRegistry, get_model_registry, resolve_api_key
from app.core.request_context import get_request_id
from app.guardrails.output_leak import (
    OutputLeakAction,
    OutputLeakConfig,
    apply_action as apply_output_leak_action,
    extract_stream_delta_text,
    get_output_leak_config,
    redact_text as redact_output_leak_text,
    requires_buffering as output_leak_requires_buffering,
    resolve_overall_action as resolve_output_leak_action,
    scan_response as scan_output_leak_response,
    scan_text as scan_output_leak_text,
)
from app.guardrails.prompt_injection import (
    InjectionAction,
    PromptInjectionConfig,
    apply_action,
    get_prompt_injection_config,
    scan_request,
)
from app.guardrails.service import GuardrailsService, get_guardrails_service
from app.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
from app.upstream.client import UpstreamClient, get_upstream_client

logger = logging.getLogger(__name__)

router = APIRouter()


def _guardrails_requested(request: ChatCompletionRequest) -> bool:
    return request.guardrails is not None and request.guardrails.enabled


def _requested_config_id(request: ChatCompletionRequest) -> str | None:
    # Only meaningful when the client explicitly names a config_id -- an
    # omitted one falls back to the server-side GUARDRAILS_NEMO_DEFAULT_CONFIG_ID,
    # which is an operator choice, not something the client selected, so no
    # per-key allowlist check applies to it.
    if not _guardrails_requested(request):
        return None
    return request.guardrails.config_id


def _sse_chunk(response_id: str, created: int, model: str, *, delta: dict, finish_reason: str | None = None) -> str:
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _apply_output_leak_guardrail(
    response: ChatCompletionResponse, ol_config: OutputLeakConfig, *, key_id: str
) -> ChatCompletionResponse:
    """Non-streaming counterpart to _with_output_leak_scan below. Unlike
    the prompt-injection pre-filter (which runs before any LLM call and so
    can reject a request with a real HTTP error before anything is
    generated), this runs *after* generation -- a BLOCK here still means a
    real HTTP error, since a non-streaming JSON response is only ever sent
    once, atomically, and nothing has been sent to the caller yet."""
    if not ol_config.enabled:
        return response

    OUTPUT_LEAK_SCANNED_TOTAL.inc()
    results = scan_output_leak_response(response, ol_config)
    if not results:
        return response

    response, action, matches = apply_output_leak_action(response, results)
    OUTPUT_LEAK_ACTIONS_TOTAL.labels(action=action.value).inc()
    for match in matches:
        OUTPUT_LEAK_MATCHES_TOTAL.labels(category=match.category.value).inc()

    categories = sorted({m.category.value for m in matches})
    if action is OutputLeakAction.BLOCK:
        logger.warning(
            "output leak blocked key_id=%s model=%s categories=%s request_id=%s",
            key_id,
            response.model,
            categories,
            get_request_id(),
        )
        raise OpenAIError(
            "The model's response was blocked by the output sensitive-information guardrail.",
            status_code=403,
            error_type="permission_error",
            code="output_leak_detected",
        )
    if action is OutputLeakAction.FLAG:
        logger.info(
            "output leak flagged key_id=%s model=%s categories=%s request_id=%s",
            key_id,
            response.model,
            categories,
            get_request_id(),
        )
    # REDACT: `response` above is already the rewritten copy from
    # apply_output_leak_action() -- nothing further to do.
    return response


def _log_and_meter_output_leak_matches(
    matches: list, *, key_id: str, model: str, blocked: bool = False, redacted: bool = False
) -> None:
    if not matches:
        return
    action = resolve_output_leak_action(matches)
    OUTPUT_LEAK_ACTIONS_TOTAL.labels(action=action.value).inc()
    for match in matches:
        OUTPUT_LEAK_MATCHES_TOTAL.labels(category=match.category.value).inc()
    verb = "blocked" if blocked else "redacted" if redacted else "flagged"
    log = logger.warning if blocked else logger.info
    log(
        "output leak %s (stream) key_id=%s model=%s categories=%s request_id=%s",
        verb,
        key_id,
        model,
        sorted({m.category.value for m in matches}),
        get_request_id(),
    )


async def _with_output_leak_scan(
    agen: AsyncIterator[str], ol_config: OutputLeakConfig, *, key_id: str, model: str
) -> AsyncIterator[str]:
    """Wraps a raw SSE chat-completion-chunk stream (from either a direct
    upstream or a guardrails backend -- same contract either way, see
    GuardrailsService's docstring) with output-leak scanning.

    Streaming BLOCK/REDACT decisions can only be made after the fact
    (unlike the prompt-injection pre-filter's real HTTP error), because
    the response text doesn't exist until it's been generated -- and by
    the time that's known, the caller's HTTP response has already
    committed to 200 text/event-stream. So a streaming BLOCK is an in-band
    SSE error frame here, same convention nemoguardrails' own output-rail
    failures already use (see app.guardrails.service._nemo_stream_error_frame).

    When no enabled category/custom pattern is configured redact/block
    (requires_buffering() is False), tokens are forwarded to the caller
    live, with scanning done only for flag-level logging/metrics once the
    stream ends. Otherwise the whole response is buffered before anything
    is released -- same trade-off nemoguardrails' own output rails accept
    for streaming, see the README's "Streaming limitation" section.
    """
    if not ol_config.enabled:
        async for chunk in agen:
            yield chunk
        return

    OUTPUT_LEAK_SCANNED_TOTAL.inc()

    if not output_leak_requires_buffering(ol_config):
        accumulated: list[str] = []
        async for chunk in agen:
            accumulated.append(extract_stream_delta_text(chunk))
            yield chunk
        matches = scan_output_leak_text("".join(accumulated), ol_config)
        _log_and_meter_output_leak_matches(matches, key_id=key_id, model=model)
        return

    buffered: list[str] = []
    async for chunk in agen:
        buffered.append(chunk)
    text = "".join(extract_stream_delta_text(c) for c in buffered)
    matches = scan_output_leak_text(text, ol_config)
    action = resolve_output_leak_action(matches)

    if action is OutputLeakAction.BLOCK:
        _log_and_meter_output_leak_matches(matches, key_id=key_id, model=model, blocked=True)
        yield format_sse_error(
            OpenAIError(
                "The model's response was blocked by the output sensitive-information guardrail.",
                status_code=403,
                error_type="permission_error",
                code="output_leak_detected",
            )
        )
        yield "data: [DONE]\n\n"
        return

    if action is OutputLeakAction.REDACT:
        _log_and_meter_output_leak_matches(matches, key_id=key_id, model=model, redacted=True)
        redacted_text = redact_output_leak_text(text, matches)
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        yield _sse_chunk(response_id, created, model, delta={"content": redacted_text})
        yield _sse_chunk(response_id, created, model, delta={}, finish_reason="stop")
        yield "data: [DONE]\n\n"
        return

    # FLAG or no match: relay the buffered original chunks unmodified.
    _log_and_meter_output_leak_matches(matches, key_id=key_id, model=model)
    for chunk in buffered:
        yield chunk


async def _with_concurrency_limit(
    limiter: ModelConcurrencyLimiter, agen: AsyncIterator[str]
) -> AsyncIterator[str]:
    # Holds the model's concurrency slot for the whole streamed response,
    # not just until the first chunk -- released when this generator is
    # closed (normal completion, or via .aclose() on early client
    # disconnect, same as the upstream agen itself; see _relay_stream).
    async with limiter.acquire():
        async for chunk in agen:
            yield chunk


async def _relay_stream(
    agen: AsyncIterator[str],
    first_chunk: str,
    *,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[str]:
    try:
        yield first_chunk
        async for chunk in agen:
            yield chunk
    finally:
        await agen.aclose()
        if on_close is not None:
            await on_close()


async def _wrap_as_streaming_response(
    agen: AsyncIterator[str], *, on_close: Callable[[], Awaitable[None]] | None = None
) -> StreamingResponse:
    try:
        first_chunk = await agen.__anext__()
    except StopAsyncIteration:
        first_chunk = "data: [DONE]\n\n"

    return StreamingResponse(
        _relay_stream(agen, first_chunk, on_close=on_close),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatCompletionRequest,
    registry: ModelRegistry = Depends(get_model_registry),
    upstream_client: UpstreamClient = Depends(get_upstream_client),
    auth: AuthContext = Depends(require_api_key),
    usage_tracker: SupportsUsageTracking = Depends(get_usage_tracker),
    guardrails: GuardrailsService = Depends(get_guardrails_service),
    pi_config: PromptInjectionConfig = Depends(get_prompt_injection_config),
    ol_config: OutputLeakConfig = Depends(get_output_leak_config),
) -> ChatCompletionResponse | StreamingResponse:
    if request.model not in registry:
        # No CHAT_COMPLETIONS_TOTAL recorded here -- request.model is
        # unvalidated client input at this point, and labeling a Prometheus
        # metric with it would let a client (or a scanner throwing garbage
        # model names at the endpoint) create unbounded label cardinality.
        # Once the model is confirmed to exist in the (operator-defined,
        # bounded) registry below, it's safe to use as a label.
        raise OpenAIError(
            f"The model `{request.model}` does not exist or you do not have access to it.",
            status_code=404,
            param="model",
            code="model_not_found",
        )
    start = time.monotonic()
    guardrails_label = str(_guardrails_requested(request))

    def _record_completion(status: str) -> None:
        CHAT_COMPLETIONS_TOTAL.labels(model=request.model, status=status).inc()
        CHAT_COMPLETION_DURATION_SECONDS.labels(
            model=request.model, guardrails=guardrails_label
        ).observe(time.monotonic() - start)

    try:
        ensure_model_allowed(auth, request.model)
        if "chat" not in registry.get(request.model).capabilities:
            raise OpenAIError(
                f"Model `{request.model}` does not support chat completions.",
                status_code=400,
                param="model",
                code="model_does_not_support_chat",
            )
        if pi_config.enabled:
            PROMPT_INJECTION_SCANNED_TOTAL.inc()
            pi_results = scan_request(request, pi_config)
            if pi_results:
                request, pi_action, pi_matches = apply_action(request, pi_results)
                PROMPT_INJECTION_ACTIONS_TOTAL.labels(action=pi_action.value).inc()
                for match in pi_matches:
                    PROMPT_INJECTION_MATCHES_TOTAL.labels(category=match.category.value, via=match.via).inc()
                if pi_action is InjectionAction.BLOCK:
                    logger.warning(
                        "prompt injection blocked key_id=%s model=%s categories=%s request_id=%s",
                        auth.key_id,
                        request.model,
                        sorted({m.category.value for m in pi_matches}),
                        get_request_id(),
                    )
                    raise OpenAIError(
                        "Your message was blocked by the prompt-injection guardrail.",
                        status_code=403,
                        error_type="permission_error",
                        code="prompt_injection_detected",
                    )
                if pi_action is InjectionAction.FLAG:
                    logger.info(
                        "prompt injection flagged key_id=%s model=%s categories=%s request_id=%s",
                        auth.key_id,
                        request.model,
                        sorted({m.category.value for m in pi_matches}),
                        get_request_id(),
                    )
                # REDACT: `request` above is already the rewritten copy from
                # apply_action() -- nothing further to do, it flows into the
                # branches below (NeMo guardrails, if enabled, or direct
                # upstream) exactly as if it had arrived pre-sanitized.

        requested_config_id = _requested_config_id(request)
        if requested_config_id is not None:
            ensure_guardrails_config_allowed(auth, requested_config_id)

        if request.stream:
            # Filled in by the guardrails/upstream call below once the
            # stream completes -- for the guardrails-routed branch it's a
            # word-count estimate (nemoguardrails doesn't expose real
            # per-token usage; see NemoLibraryGuardrailsService's class
            # docstring), for the direct-upstream branch it's real usage
            # from OpenAI's stream_options.include_usage when the upstream
            # supports it. Stays empty (0/0/0) otherwise either way -- the
            # request itself is still counted, just with no token counts,
            # same as before either of those existed.
            usage_holder: dict[str, int] = {}
            guardrails_stream = (
                await guardrails.stream_chat_completion(request, usage_holder=usage_holder)
                if _guardrails_requested(request)
                else None
            )
            if guardrails_stream is not None:
                # The guardrails backend called the underlying LLM itself, so
                # we don't call the upstream client again -- see
                # GuardrailsService. _record_completion/usage_tracker.record
                # fire when the stream actually finishes (on_close), not
                # here -- this function returns long before the stream is
                # drained, and real usage isn't known until it is. Held for
                # the whole stream via the model's own limiter (the same
                # instance the direct-upstream path below acquires), so a
                # guardrails-routed call and a direct call to the same model
                # count against one shared concurrency budget even though
                # NemoLibraryGuardrailsService reaches the upstream through
                # its own connection, not upstream_client.
                guardrails_stream = _with_output_leak_scan(
                    guardrails_stream, ol_config, key_id=auth.key_id, model=request.model
                )
                limiter = registry.get_concurrency_limiter(request.model)
                guardrails_stream = _with_concurrency_limit(limiter, guardrails_stream)
                logger.info(
                    "chat completion (stream, guardrails) key_id=%s model=%s request_id=%s",
                    auth.key_id,
                    request.model,
                    get_request_id(),
                )

                async def _on_close() -> None:
                    _record_completion("200")
                    await usage_tracker.record(
                        auth.key_id,
                        prompt_tokens=usage_holder.get("prompt_tokens", 0),
                        completion_tokens=usage_holder.get("completion_tokens", 0),
                        total_tokens=usage_holder.get("total_tokens", 0),
                    )

                return await _wrap_as_streaming_response(guardrails_stream, on_close=_on_close)

            entry = registry.get(request.model)
            api_key = resolve_api_key(entry)
            agen = upstream_client.stream_chat_completion(
                base_url=entry.base_url,
                api_key=api_key,
                upstream_model=entry.upstream_model,
                request=request,
                usage_holder=usage_holder,
            )
            agen = _with_output_leak_scan(agen, ol_config, key_id=auth.key_id, model=request.model)
            limiter = registry.get_concurrency_limiter(request.model)
            agen = _with_concurrency_limit(limiter, agen)
            logger.info(
                "chat completion (stream) key_id=%s model=%s request_id=%s",
                auth.key_id,
                request.model,
                get_request_id(),
            )

            async def _on_close() -> None:
                _record_completion("200")
                # usage_holder is populated from upstream's own final usage
                # chunk (OpenAI's stream_options.include_usage), when the
                # upstream supports it -- see UpstreamClient.stream_chat_completion.
                # Stays empty (0/0/0) otherwise, same as before this existed:
                # the request itself is still counted, just with no token
                # counts.
                await usage_tracker.record(
                    auth.key_id,
                    prompt_tokens=usage_holder.get("prompt_tokens", 0),
                    completion_tokens=usage_holder.get("completion_tokens", 0),
                    total_tokens=usage_holder.get("total_tokens", 0),
                )

            return await _wrap_as_streaming_response(agen, on_close=_on_close)

        if _guardrails_requested(request):
            # Same shared per-model limiter as the direct-upstream path
            # below -- see the comment on the streaming guardrails branch.
            limiter = registry.get_concurrency_limiter(request.model)
            async with limiter.acquire():
                guardrails_response = await guardrails.process_chat_completion(request)
        else:
            guardrails_response = None
        if guardrails_response is not None:
            await usage_tracker.record(
                auth.key_id,
                prompt_tokens=guardrails_response.usage.prompt_tokens,
                completion_tokens=guardrails_response.usage.completion_tokens,
                total_tokens=guardrails_response.usage.total_tokens,
            )
            logger.info(
                "chat completion (guardrails) key_id=%s model=%s total_tokens=%d request_id=%s",
                auth.key_id,
                request.model,
                guardrails_response.usage.total_tokens,
                get_request_id(),
            )
            guardrails_response = _apply_output_leak_guardrail(guardrails_response, ol_config, key_id=auth.key_id)
            _record_completion("200")
            return guardrails_response

        entry = registry.get(request.model)
        api_key = resolve_api_key(entry)
        limiter = registry.get_concurrency_limiter(request.model)
        async with limiter.acquire():
            response = await upstream_client.create_chat_completion(
                base_url=entry.base_url,
                api_key=api_key,
                upstream_model=entry.upstream_model,
                request=request,
            )
        # Report our own public model id back to the caller, not whatever the
        # upstream happened to echo (its upstream_model name may differ).
        response = response.model_copy(update={"model": request.model})

        await usage_tracker.record(
            auth.key_id,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
        )
        logger.info(
            "chat completion key_id=%s model=%s total_tokens=%d request_id=%s",
            auth.key_id,
            request.model,
            response.usage.total_tokens,
            get_request_id(),
        )
        response = _apply_output_leak_guardrail(response, ol_config, key_id=auth.key_id)
        _record_completion("200")
        return response
    except OpenAIError as exc:
        _record_completion(str(exc.status_code))
        raise
    except Exception:
        _record_completion("500")
        raise
