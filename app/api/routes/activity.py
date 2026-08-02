import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Query

from app.auth.dependency import AuthContext, require_admin
from app.core.errors import OpenAIError
from app.prometheus.client import PrometheusClient, PrometheusError, get_prometheus_client
from app.schemas.activity import (
    ActivityOverviewResponse,
    ActivityTotals,
    ModelTimeSeries,
    TimeSeriesPoint,
    TopKeyItem,
    TopModelItem,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# step: bucket width for the time-series chart, kept coarse enough that a
# browser-rendered SVG chart doesn't choke on point count. The by-model
# series uses increase() over a window equal to step, so each bar is an
# actual request count for that bucket (not a rate) -- matches the
# stacked-bar chart's semantics and keeps Y-axis values readable instead of
# vanishingly small per-second rates.
_RANGE_CONFIG = {
    "1h": {"seconds": 3600, "step": "1m"},
    "24h": {"seconds": 86400, "step": "15m"},
    "7d": {"seconds": 7 * 86400, "step": "2h"},
    "30d": {"seconds": 30 * 86400, "step": "6h"},
}


def _instant_value(result: list[dict]) -> float | None:
    """Extracts the single scalar from an aggregate instant query
    (`sum(...)` with no `by (...)`) -- empty when nothing matched (e.g. no
    traffic yet in the window), which is a legitimate "no data" case, not
    an error.
    """
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


@router.get("/api/admin/activity/overview", response_model=ActivityOverviewResponse)
async def activity_overview(
    window: str = Query("24h", alias="range", pattern="^(1h|24h|7d|30d)$"),
    prometheus: PrometheusClient | None = Depends(get_prometheus_client),
    auth: AuthContext = Depends(require_admin),
) -> ActivityOverviewResponse:
    if prometheus is None:
        raise OpenAIError(
            "Observability isn't configured for this deployment (PROMETHEUS_URL unset).",
            status_code=503,
            error_type="api_error",
            code="observability_not_configured",
        )

    config = _RANGE_CONFIG[window]
    now = time.time()

    # A fixed set of PromQL queries -- never client-supplied -- run in
    # parallel since they're independent reads against the same Prometheus.
    try:
        (
            requests_result,
            requests_ok_result,
            tokens_result,
            latency_sum_result,
            latency_count_result,
            by_model_result,
            top_keys_result,
            top_models_result,
        ) = await asyncio.gather(
            prometheus.query(f"sum(increase(openbouncer_chat_completions_total[{window}]))"),
            prometheus.query(
                f'sum(increase(openbouncer_chat_completions_total{{status="200"}}[{window}]))'
            ),
            prometheus.query(
                f'sum(increase(openbouncer_usage_tokens_total{{kind="total"}}[{window}]))'
            ),
            prometheus.query(
                f"sum(increase(openbouncer_chat_completion_duration_seconds_sum[{window}]))"
            ),
            prometheus.query(
                f"sum(increase(openbouncer_chat_completion_duration_seconds_count[{window}]))"
            ),
            prometheus.query_range(
                f"sum(increase(openbouncer_chat_completions_total[{config['step']}])) by (model)",
                start=now - config["seconds"],
                end=now,
                step=config["step"],
            ),
            prometheus.query(
                'topk(5, sum(openbouncer_usage_tokens_total{kind="total"}) by (key_id))'
            ),
            prometheus.query(
                f"topk(5, sum(increase(openbouncer_chat_completions_total[{window}])) by (model))"
            ),
        )
    except PrometheusError as exc:
        logger.warning("Activity overview query failed: %s", exc)
        raise OpenAIError(
            "Could not reach the observability backend.",
            status_code=503,
            error_type="api_error",
            code="observability_unavailable",
        ) from exc

    total_requests = _instant_value(requests_result) or 0.0
    total_requests_ok = _instant_value(requests_ok_result) or 0.0
    total_tokens = _instant_value(tokens_result) or 0.0
    latency_sum = _instant_value(latency_sum_result)
    latency_count = _instant_value(latency_count_result)

    totals = ActivityTotals(
        requests=int(total_requests),
        tokens=int(total_tokens),
        success_rate=(total_requests_ok / total_requests) if total_requests > 0 else None,
        avg_latency_seconds=(latency_sum / latency_count) if latency_count else None,
    )

    requests_by_model = [
        ModelTimeSeries(
            model=series.get("metric", {}).get("model", "unknown"),
            points=[
                TimeSeriesPoint(t=int(float(t)), v=float(v))
                for t, v in series.get("values", [])
            ],
        )
        for series in by_model_result
    ]

    top_keys_by_tokens = [
        TopKeyItem(
            key_id=series.get("metric", {}).get("key_id", "unknown"),
            tokens=float(series["value"][1]),
        )
        for series in top_keys_result
    ]
    top_models_by_requests = [
        TopModelItem(
            model=series.get("metric", {}).get("model", "unknown"),
            requests=float(series["value"][1]),
        )
        for series in top_models_result
    ]

    return ActivityOverviewResponse(
        range=window,
        totals=totals,
        requests_by_model=requests_by_model,
        top_keys_by_tokens=top_keys_by_tokens,
        top_models_by_requests=top_models_by_requests,
    )
