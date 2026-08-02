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

# step/step_seconds: bucket width for the time-series chart, kept coarse
# enough that a browser-rendered SVG chart doesn't choke on point count --
# 24h buckets by the hour, 30d buckets by the day, so each range plots a
# sensible number of bars. The by-model series uses increase() over a
# window equal to step, so each bar is an actual request count for that
# bucket (not a rate) -- matches the stacked-bar chart's semantics and
# keeps Y-axis values readable instead of vanishingly small per-second
# rates.
_RANGE_CONFIG = {
    "1h": {"seconds": 3600, "step": "1m", "step_seconds": 60},
    "24h": {"seconds": 86400, "step": "1h", "step_seconds": 3600},
    "7d": {"seconds": 7 * 86400, "step": "2h", "step_seconds": 7200},
    "30d": {"seconds": 30 * 86400, "step": "1d", "step_seconds": 86400},
}


def _fill_grid(
    points: list[TimeSeriesPoint], start: int, end: int, step_seconds: int
) -> list[TimeSeriesPoint]:
    """Reindexes a sparse series onto every bucket from start to end, filling
    gaps with 0 -- Prometheus only emits a point where increase() actually
    had samples to work with, so a model used only in the last day of a
    30-day range would otherwise produce a chart that silently starts on
    whatever day traffic began instead of spanning the full requested range.
    """
    by_t = {p.t: p.v for p in points}
    return [
        TimeSeriesPoint(t=t, v=by_t.get(t, 0.0)) for t in range(start, end + 1, step_seconds)
    ]


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
    step_seconds = config["step_seconds"]
    now = time.time()
    # Align the query window to whole buckets (e.g. the top of the hour, or
    # midnight) instead of "now" itself, so grid points line up exactly
    # with what _fill_grid below expects and chart ticks land on round
    # boundaries instead of e.g. "14:37".
    range_end = (int(now) // step_seconds) * step_seconds
    range_start = range_end - config["seconds"]

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
                start=range_start,
                end=range_end,
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
            points=_fill_grid(
                [
                    TimeSeriesPoint(t=int(float(t)), v=float(v))
                    for t, v in series.get("values", [])
                ],
                range_start,
                range_end,
                step_seconds,
            ),
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
