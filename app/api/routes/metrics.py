from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.auth.dependency import AuthContext, require_admin
from app.auth.usage import UsageTracker, get_usage_tracker
from app.core.metrics import (
    MODEL_INFLIGHT_REQUESTS,
    MODEL_QUEUED_REQUESTS,
    USAGE_REQUESTS_TOTAL,
    USAGE_TOKENS_TOTAL,
)
from app.core.registry import ModelRegistry, get_model_registry

router = APIRouter()


@router.get("/metrics")
async def metrics(
    registry: ModelRegistry = Depends(get_model_registry),
    usage_tracker: UsageTracker = Depends(get_usage_tracker),
    auth: AuthContext = Depends(require_admin),
) -> Response:
    # Gated by require_admin, same as /api/admin/* -- request-rate-by-key
    # is usage data, treated with the same sensitivity as the admin
    # endpoints. Prometheus's scrape config authenticates with an admin
    # key's bearer token (see docker-compose's observability profile).

    # These Gauges mirror state that already lives elsewhere
    # (ModelConcurrencyLimiter, UsageTracker) rather than being
    # incremented inline wherever that state changes -- set their current
    # value here, at scrape time.
    for entry in registry.all():
        limiter = registry.get_concurrency_limiter(entry.id)
        MODEL_INFLIGHT_REQUESTS.labels(model=entry.id).set(limiter.in_flight)
        MODEL_QUEUED_REQUESTS.labels(model=entry.id).set(limiter.queued)

    for key_id, stats in usage_tracker.all().items():
        USAGE_REQUESTS_TOTAL.labels(key_id=key_id).set(stats.requests)
        USAGE_TOKENS_TOTAL.labels(key_id=key_id, kind="prompt").set(stats.prompt_tokens)
        USAGE_TOKENS_TOTAL.labels(key_id=key_id, kind="completion").set(stats.completion_tokens)
        USAGE_TOKENS_TOTAL.labels(key_id=key_id, kind="total").set(stats.total_tokens)

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
