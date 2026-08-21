import json
import os
from pathlib import Path

import pytest

from app.core.guardrail_events import record_event


async def _record(**overrides):
    fields = dict(
        request_id="req-1",
        key_id="some-key",
        guardrail="prompt_injection",
        model="local/gemma4-nvfp4",
        category="instruction_override",
        pattern_name="ignore_instructions",
        action="block",
        via="direct",
        snippet="ignore all previous instructions",
    )
    fields.update(overrides)
    return await record_event(**fields)


class TestListGuardrailEvents:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.get("/api/admin/guardrail-events")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"

    @pytest.mark.asyncio
    async def test_scoped_to_metrics_read_only_is_rejected(self, observer_client):
        # observer_client (see conftest.py) has metrics:read + activity:read
        # -- activity:read is exactly what this endpoint requires, so this
        # should actually succeed; kept as a smoke test that the scope
        # check is "activity:read", not "metrics:read".
        response = await observer_client.get("/api/admin/guardrail-events")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_lists_recorded_events(self, admin_client):
        await _record()
        response = await admin_client.get("/api/admin/guardrail-events")
        assert response.status_code == 200
        body = response.json()
        assert len(body["events"]) == 1
        event = body["events"][0]
        assert event["key_id"] == "some-key"
        assert event["guardrail"] == "prompt_injection"
        assert event["category"] == "instruction_override"
        assert event["action"] == "block"
        assert event["via"] == "direct"
        assert event["snippet"] == "ignore all previous instructions"
        assert event["request_id"] == "req-1"

    @pytest.mark.asyncio
    async def test_empty_when_nothing_recorded(self, admin_client):
        response = await admin_client.get("/api/admin/guardrail-events")
        assert response.status_code == 200
        assert response.json()["events"] == []

    @pytest.mark.asyncio
    async def test_most_recent_first(self, admin_client):
        for i in range(3):
            await _record(pattern_name=f"pattern-{i}")
        response = await admin_client.get("/api/admin/guardrail-events")
        patterns = [e["pattern_name"] for e in response.json()["events"]]
        assert patterns == ["pattern-2", "pattern-1", "pattern-0"]

    @pytest.mark.asyncio
    async def test_limit_query_param(self, admin_client):
        for i in range(5):
            await _record(pattern_name=f"pattern-{i}")
        response = await admin_client.get("/api/admin/guardrail-events?limit=2")
        assert len(response.json()["events"]) == 2

    @pytest.mark.asyncio
    async def test_filter_by_key_id(self, admin_client):
        await _record(key_id="key-a")
        await _record(key_id="key-b")
        response = await admin_client.get("/api/admin/guardrail-events?key_id=key-a")
        events = response.json()["events"]
        assert len(events) == 1
        assert events[0]["key_id"] == "key-a"

    @pytest.mark.asyncio
    async def test_filter_by_guardrail(self, admin_client):
        await _record(guardrail="prompt_injection")
        await _record(guardrail="output_leak", category="email", via=None)
        response = await admin_client.get("/api/admin/guardrail-events?guardrail=output_leak")
        events = response.json()["events"]
        assert len(events) == 1
        assert events[0]["guardrail"] == "output_leak"

    @pytest.mark.asyncio
    async def test_invalid_guardrail_filter_rejected(self, admin_client):
        response = await admin_client.get("/api/admin/guardrail-events?guardrail=not_a_real_one")
        # 400, not 422 -- this codebase's own RequestValidationError handler
        # (app.core.errors.openai_validation_exception_handler) always
        # returns 400, matching the OpenAI error shape rather than FastAPI's
        # default 422.
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_filter_by_action(self, admin_client):
        await _record(action="block")
        await _record(action="flag")
        response = await admin_client.get("/api/admin/guardrail-events?action=flag")
        events = response.json()["events"]
        assert len(events) == 1
        assert events[0]["action"] == "flag"

    @pytest.mark.asyncio
    async def test_invalid_action_filter_rejected(self, admin_client):
        response = await admin_client.get("/api/admin/guardrail-events?action=sabotage")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_limit_rejected(self, admin_client):
        response = await admin_client.get("/api/admin/guardrail-events?limit=0")
        assert response.status_code == 400
        response = await admin_client.get("/api/admin/guardrail-events?limit=201")
        assert response.status_code == 400


class TestVerifyGuardrailEventsChain:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.get("/api/admin/guardrail-events/verify")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_scoped_to_activity_read_is_allowed(self, observer_client):
        # Same scope as GET /api/admin/guardrail-events -- see that
        # endpoint's own scope test above.
        response = await observer_client.get("/api/admin/guardrail-events/verify")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_when_nothing_recorded(self, admin_client):
        response = await admin_client.get("/api/admin/guardrail-events/verify")
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is True
        assert body["verified_count"] == 0
        assert body["broken_at_id"] is None

    @pytest.mark.asyncio
    async def test_valid_after_recording_events(self, admin_client):
        await _record()
        await _record()
        response = await admin_client.get("/api/admin/guardrail-events/verify")
        body = response.json()
        assert body["valid"] is True
        assert body["verified_count"] == 2

    @pytest.mark.asyncio
    async def test_detects_tampering(self, admin_client):
        await _record()
        await _record()
        log_path = Path(os.environ["OPENBOUNCER_GUARDRAIL_EVENTS_PATH"])
        lines = log_path.read_text().splitlines()
        tampered = json.loads(lines[0])
        tampered["action"] = "flag"
        lines[0] = json.dumps(tampered)
        log_path.write_text("\n".join(lines) + "\n")

        response = await admin_client.get("/api/admin/guardrail-events/verify")
        body = response.json()
        assert body["valid"] is False
        assert body["broken_reason"] is not None
