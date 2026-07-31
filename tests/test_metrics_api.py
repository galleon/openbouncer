import re

import pytest


def _metric_value(body: str, name: str, labels: str) -> float:
    # prometheus_client's default REGISTRY is a process-wide singleton, and
    # get_usage_tracker() is @lru_cache'd too -- both persist (and
    # accumulate) across every test in the session, not just this file.
    # Assertions here compare deltas around known requests rather than
    # absolute values, so they're correct regardless of what ran before.
    # A labeled series that's never been recorded yet legitimately doesn't
    # appear in the output at all -- treat that as 0, not a missing metric.
    pattern = re.escape(f"{name}{{{labels}}}") + r" ([0-9.eE+-]+)"
    match = re.search(pattern, body)
    return float(match.group(1)) if match else 0.0


@pytest.mark.asyncio
async def test_non_admin_rejected(client):
    response = await client.get("/metrics")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_required"


@pytest.mark.asyncio
async def test_admin_gets_prometheus_text(admin_client):
    response = await admin_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    # The metric families are always declared (even with zero series
    # recorded for the request-scoped ones), and the model gauges are
    # populated for every registered model regardless of traffic.
    assert "openbouncer_http_requests_total" in body
    assert "openbouncer_chat_completions_total" in body
    assert "openbouncer_guardrails_requests_total" in body
    assert "openbouncer_model_inflight_requests" in body
    assert "openbouncer_model_queued_requests" in body
    assert "openbouncer_usage_requests_total" in body
    assert 'openbouncer_model_inflight_requests{model="local/gemma4-nvfp4"} 0.0' in body


@pytest.mark.asyncio
async def test_http_requests_total_increments_after_traffic(admin_client):
    before = _metric_value(
        (await admin_client.get("/metrics")).text,
        "openbouncer_http_requests_total",
        'method="GET",path="/healthz",status="200"',
    )

    await admin_client.get("/healthz")
    await admin_client.get("/healthz")

    after = _metric_value(
        (await admin_client.get("/metrics")).text,
        "openbouncer_http_requests_total",
        'method="GET",path="/healthz",status="200"',
    )
    assert after - before == 2


@pytest.mark.asyncio
async def test_usage_and_chat_completions_reflect_a_real_request(admin_client):
    labels_usage = 'key_id="admin-key"'
    labels_chat = 'model="local/gemma4-nvfp4",status="200"'
    before_body = (await admin_client.get("/metrics")).text
    before_usage = _metric_value(before_body, "openbouncer_usage_requests_total", labels_usage)
    before_chat = _metric_value(before_body, "openbouncer_chat_completions_total", labels_chat)

    response = await admin_client.post(
        "/v1/chat/completions",
        json={
            "model": "local/gemma4-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200

    after_body = (await admin_client.get("/metrics")).text
    after_usage = _metric_value(after_body, "openbouncer_usage_requests_total", labels_usage)
    after_chat = _metric_value(after_body, "openbouncer_chat_completions_total", labels_chat)

    assert after_usage - before_usage == 1
    assert after_chat - before_chat == 1
