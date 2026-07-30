import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.auth.dependency import AuthContext, ensure_model_allowed, require_api_key
from app.auth.usage import UsageTracker, get_usage_tracker
from app.core.errors import OpenAIError
from app.core.registry import ModelRegistry, get_model_registry, resolve_api_key
from app.core.request_context import get_request_id
from app.guardrails.service import GuardrailsService, get_guardrails_service
from app.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)
from app.upstream.client import UpstreamClient, get_upstream_client

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_text(content: str | list) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if part.type == "text")


def _count_tokens(text: str) -> int:
    return len(text.split())


def _guardrails_requested(request: ChatCompletionRequest) -> bool:
    return request.guardrails is not None and request.guardrails.enabled


async def _relay_stream(
    agen: AsyncIterator[str], first_chunk: str
) -> AsyncIterator[str]:
    try:
        yield first_chunk
        async for chunk in agen:
            yield chunk
    finally:
        await agen.aclose()


async def _wrap_as_streaming_response(agen: AsyncIterator[str]) -> StreamingResponse:
    try:
        first_chunk = await agen.__anext__()
    except StopAsyncIteration:
        first_chunk = "data: [DONE]\n\n"

    return StreamingResponse(
        _relay_stream(agen, first_chunk),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatCompletionRequest,
    registry: ModelRegistry = Depends(get_model_registry),
    upstream_client: UpstreamClient = Depends(get_upstream_client),
    auth: AuthContext = Depends(require_api_key),
    usage_tracker: UsageTracker = Depends(get_usage_tracker),
    guardrails: GuardrailsService = Depends(get_guardrails_service),
) -> ChatCompletionResponse | StreamingResponse:
    if request.model not in registry:
        raise OpenAIError(
            f"The model `{request.model}` does not exist or you do not have access to it.",
            status_code=404,
            param="model",
            code="model_not_found",
        )
    ensure_model_allowed(auth, request.model)

    if request.stream:
        guardrails_stream = (
            await guardrails.stream_chat_completion(request)
            if _guardrails_requested(request)
            else None
        )
        if guardrails_stream is not None:
            # The guardrails backend called the underlying LLM itself, so we
            # don't call the upstream client again -- see GuardrailsService.
            await usage_tracker.record(auth.key_id)
            logger.info(
                "chat completion (stream, guardrails) key_id=%s model=%s request_id=%s",
                auth.key_id,
                request.model,
                get_request_id(),
            )
            return await _wrap_as_streaming_response(guardrails_stream)

        entry = registry.get(request.model)
        api_key = resolve_api_key(entry)
        agen = upstream_client.stream_chat_completion(
            base_url=entry.base_url,
            api_key=api_key,
            upstream_model=entry.upstream_model,
            request=request,
        )
        # Streaming responses don't currently surface a final aggregate
        # usage total (that requires OpenAI's stream_options.include_usage,
        # which isn't implemented yet -- see UpstreamClient), so we only
        # account for the request itself, with no token counts.
        await usage_tracker.record(auth.key_id)
        logger.info(
            "chat completion (stream) key_id=%s model=%s request_id=%s",
            auth.key_id,
            request.model,
            get_request_id(),
        )
        return await _wrap_as_streaming_response(agen)

    guardrails_response = (
        await guardrails.process_chat_completion(request)
        if _guardrails_requested(request)
        else None
    )
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
        return guardrails_response

    last_user_message = next(
        (_extract_text(m.content) for m in reversed(request.messages) if m.role == "user"),
        "",
    )
    reply_content = f"You said: {last_user_message}" if last_user_message else "Hello!"

    prompt_tokens = sum(_count_tokens(_extract_text(m.content)) for m in request.messages)
    completion_tokens = _count_tokens(reply_content)

    response = ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=request.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=reply_content),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )

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

    return response
