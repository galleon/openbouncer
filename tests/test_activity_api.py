import httpx
import pytest
import respx

from app.main import app
from app.prometheus.client import PrometheusClient, get_prometheus_client

PROM_BASE_URL = "http://test-prometheus:9090"


def _vector(value: str | None) -> dict:
    result = [] if value is None else [{"metric": {}, "value": [1700000000, value]}]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _topk_vector(items: dict[str, str], label: str) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {label: key}, "value": [1700000000, value]}
                for key, value in items.items()
            ],
        },
    }


def _matrix(series: dict[str, list[tuple[int, str]]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"model": model}, "values": [[t, v] for t, v in points]}
                for model, points in series.items()
            ],
        },
    }


def _prometheus_side_effect(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/query_range":
        # A single real sample at the window's end -- the endpoint now
        # backfills the rest of the grid with zeros, so the fixed timestamp
        # this used to hardcode would just fall outside the real (now-based)
        # window and get silently dropped by that backfill.
        end = int(float(request.url.params["end"]))
        return httpx.Response(200, json=_matrix({"local/gemma4-nvfp4": [(end, "1")]}))

    query = request.url.params["query"]
    if "topk" in query and "key_id" in query:
        return httpx.Response(200, json=_topk_vector({"admin": "500"}, "key_id"))
    if "topk" in query:
        return httpx.Response(200, json=_topk_vector({"local/gemma4-nvfp4": "10"}, "model"))
    if 'status="200"' in query:
        return httpx.Response(200, json=_vector("8"))
    if "chat_completions_total" in query:
        return httpx.Response(200, json=_vector("10"))
    if "usage_tokens_total" in query:
        return httpx.Response(200, json=_vector("12345"))
    if "duration_seconds_sum" in query:
        return httpx.Response(200, json=_vector("20"))
    if "duration_seconds_count" in query:
        return httpx.Response(200, json=_vector("10"))
    raise AssertionError(f"Unexpected PromQL query in test: {query}")


@pytest.fixture
def prometheus_configured():
    client = PrometheusClient(base_url=PROM_BASE_URL)
    app.dependency_overrides[get_prometheus_client] = lambda: client
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_prometheus_client, None)


@pytest.fixture
def prometheus_unconfigured():
    app.dependency_overrides[get_prometheus_client] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_prometheus_client, None)


@pytest.mark.asyncio
async def test_non_admin_rejected(client, prometheus_configured):
    response = await client.get("/api/admin/activity/overview")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_required"


@pytest.mark.asyncio
@respx.mock
async def test_scoped_activity_read_key_is_allowed(observer_client, prometheus_configured):
    # A key with just the "activity:read" admin scope (not is_admin) can
    # read the dashboard -- see the observer_client fixture in conftest.py.
    respx.get(url__startswith=PROM_BASE_URL).mock(side_effect=_prometheus_side_effect)
    response = await observer_client.get("/api/admin/activity/overview")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_unconfigured_returns_503(admin_client, prometheus_unconfigured):
    response = await admin_client.get("/api/admin/activity/overview")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "observability_not_configured"


@pytest.mark.asyncio
async def test_rejects_invalid_range(admin_client, prometheus_configured):
    response = await admin_client.get("/api/admin/activity/overview?range=3y")
    assert response.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_overview_shapes_prometheus_data(admin_client, prometheus_configured):
    respx.get(url__startswith=PROM_BASE_URL).mock(side_effect=_prometheus_side_effect)

    response = await admin_client.get("/api/admin/activity/overview?range=24h")
    assert response.status_code == 200
    body = response.json()

    assert body["range"] == "24h"
    assert body["totals"]["requests"] == 10
    assert body["totals"]["tokens"] == 12345
    # 8 successful out of 10 total.
    assert body["totals"]["success_rate"] == pytest.approx(0.8)
    # 20s summed duration over 10 completions.
    assert body["totals"]["avg_latency_seconds"] == pytest.approx(2.0)

    assert len(body["requests_by_model"]) == 1
    assert body["requests_by_model"][0]["model"] == "local/gemma4-nvfp4"
    points = body["requests_by_model"][0]["points"]
    # 24h range at a 1h step backfills to 25 grid points (start..end inclusive);
    # only the last (the mocked sample) is non-zero.
    assert len(points) == 25
    assert [p["v"] for p in points[:-1]] == [0.0] * 24
    assert points[-1]["v"] == 1.0

    assert body["top_keys_by_tokens"] == [{"key_id": "admin", "tokens": 500.0}]
    assert body["top_models_by_requests"] == [{"model": "local/gemma4-nvfp4", "requests": 10.0}]


@pytest.mark.asyncio
@respx.mock
async def test_overview_handles_empty_results(admin_client, prometheus_configured):
    # No traffic yet in the window -- every series is empty rather than an
    # error; totals should degrade to 0/None, not crash.
    def _empty_side_effect(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query_range":
            return httpx.Response(200, json=_matrix({}))
        return httpx.Response(200, json=_vector(None))

    respx.get(url__startswith=PROM_BASE_URL).mock(side_effect=_empty_side_effect)

    response = await admin_client.get("/api/admin/activity/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["requests"] == 0
    assert body["totals"]["tokens"] == 0
    assert body["totals"]["success_rate"] is None
    assert body["totals"]["avg_latency_seconds"] is None
    assert body["requests_by_model"] == []
    assert body["top_keys_by_tokens"] == []
    assert body["top_models_by_requests"] == []


@pytest.mark.asyncio
@respx.mock
async def test_prometheus_connection_failure_returns_503(admin_client, prometheus_configured):
    respx.get(url__startswith=PROM_BASE_URL).mock(side_effect=httpx.ConnectError("refused"))

    response = await admin_client.get("/api/admin/activity/overview")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "observability_unavailable"


@pytest.mark.asyncio
@respx.mock
async def test_prometheus_error_status_returns_503(admin_client, prometheus_configured):
    respx.get(url__startswith=PROM_BASE_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "error", "error": "bad query", "errorType": "bad_data"}
        )
    )

    response = await admin_client.get("/api/admin/activity/overview")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "observability_unavailable"
