import json

import httpx
import pytest
import respx

WEBHOOK_URL = "https://hooks.example.com/alert"


class TestAlertWebhookTest:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.post("/api/admin/alerts/test")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"

    @pytest.mark.asyncio
    async def test_not_configured_reports_configured_false(self, admin_client, monkeypatch):
        monkeypatch.delenv("OPENBOUNCER_ALERT_WEBHOOK_URL", raising=False)
        response = await admin_client.post("/api/admin/alerts/test")
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is False
        assert body["delivered"] is False
        assert body["status_code"] is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_delivery_reported(self, admin_client, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_WEBHOOK_URL", WEBHOOK_URL)
        route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))

        response = await admin_client.post("/api/admin/alerts/test")
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["delivered"] is True
        assert body["status_code"] == 200
        assert body["error"] is None
        assert route.called

        payload = json.loads(route.calls[0].request.content)
        assert "test alert" in payload["text"]
        assert payload["key_id"] == "test"

    @pytest.mark.asyncio
    @respx.mock
    async def test_webhook_error_response_reported_not_raised(self, admin_client, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_WEBHOOK_URL", WEBHOOK_URL)
        respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(404, text="not found"))

        response = await admin_client.post("/api/admin/alerts/test")
        assert response.status_code == 200  # the *test endpoint* itself always succeeds
        body = response.json()
        assert body["configured"] is True
        assert body["delivered"] is False
        assert body["status_code"] == 404
        assert "not found" in body["error"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_webhook_connection_error_reported_not_raised(self, admin_client, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_ALERT_WEBHOOK_URL", WEBHOOK_URL)
        respx.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectError("connection refused"))

        response = await admin_client.post("/api/admin/alerts/test")
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["delivered"] is False
        assert body["status_code"] is None
        assert body["error"] is not None

    @pytest.mark.asyncio
    async def test_scoped_to_activity_read_succeeds(self, observer_client, monkeypatch):
        # observer_client (see conftest.py) has metrics:read + activity:read
        # -- activity:read is exactly what this endpoint requires.
        monkeypatch.delenv("OPENBOUNCER_ALERT_WEBHOOK_URL", raising=False)
        response = await observer_client.post("/api/admin/alerts/test")
        assert response.status_code == 200
