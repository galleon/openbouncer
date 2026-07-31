import logging

from fastapi import APIRouter, Depends

from app.auth.dependency import AuthContext, ensure_model_allowed, require_api_key
from app.auth.usage import UsageTracker, get_usage_tracker
from app.core.errors import OpenAIError
from app.core.registry import ModelRegistry, get_model_registry, resolve_api_key
from app.core.request_context import get_request_id
from app.schemas.embeddings import EmbeddingRequest, EmbeddingResponse
from app.upstream.client import UpstreamClient, get_upstream_client

logger = logging.getLogger(__name__)

router = APIRouter()

# Embeddings never go through GuardrailsService, unlike chat completions --
# guardrails (content safety, dialog rails) are about moderating generated
# conversational text, not vector representations of input text. This route
# intentionally has no dependency on app.guardrails at all, regardless of
# GUARDRAILS_MODE.


@router.post("/v1/embeddings", response_model=EmbeddingResponse)
async def create_embeddings(
    request: EmbeddingRequest,
    registry: ModelRegistry = Depends(get_model_registry),
    upstream_client: UpstreamClient = Depends(get_upstream_client),
    auth: AuthContext = Depends(require_api_key),
    usage_tracker: UsageTracker = Depends(get_usage_tracker),
) -> EmbeddingResponse:
    entry = registry.get(request.model)
    if entry is None:
        raise OpenAIError(
            f"The model `{request.model}` does not exist or you do not have access to it.",
            status_code=404,
            param="model",
            code="model_not_found",
        )
    ensure_model_allowed(auth, request.model)
    if "embeddings" not in entry.capabilities:
        raise OpenAIError(
            f"Model `{request.model}` does not support embeddings.",
            status_code=400,
            param="model",
            code="model_does_not_support_embeddings",
        )

    api_key = resolve_api_key(entry)
    limiter = registry.get_concurrency_limiter(request.model)
    async with limiter.acquire():
        response = await upstream_client.create_embeddings(
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
        total_tokens=response.usage.total_tokens,
    )
    logger.info(
        "embeddings key_id=%s model=%s total_tokens=%d request_id=%s",
        auth.key_id,
        request.model,
        response.usage.total_tokens,
        get_request_id(),
    )

    return response
