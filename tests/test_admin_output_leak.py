import pytest

from app.guardrails.output_leak import CONFIG_PATH_ENV_VAR as OUTPUT_LEAK_CONFIG_PATH_ENV_VAR
from app.guardrails.output_leak import get_output_leak_config as real_get_output_leak_config


@pytest.fixture
def scratch_output_leak_file(tmp_path, monkeypatch):
    """A real, file-backed output_leak.yaml the admin PATCH/test endpoints
    can persist to/read from -- same reasoning as
    test_admin_api.py's scratch_prompt_injection_file fixture (never
    touches the real config/output_leak.yaml during tests, and clears the
    lru_cache on both sides of the test so nothing leaks into unrelated
    tests)."""
    path = tmp_path / "output_leak.yaml"
    monkeypatch.setenv(OUTPUT_LEAK_CONFIG_PATH_ENV_VAR, str(path))
    real_get_output_leak_config.cache_clear()
    try:
        yield path
    finally:
        real_get_output_leak_config.cache_clear()


class TestGetOutputLeakConfig:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.get("/api/admin/output-leak")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"

    @pytest.mark.asyncio
    async def test_admin_sees_default_state(self, admin_client, scratch_output_leak_file):
        response = await admin_client.get("/api/admin/output-leak")
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["allow_list"] == []
        assert body["custom_patterns"] == []
        # "custom" isn't independently configurable -- it never appears
        # here, its action lives per-entry on custom_patterns instead.
        assert len(body["categories"]) == 6  # email/phone/ssn/credit_card/ip_address/secret_token
        assert "custom" not in body["categories"]
        assert all(action == "flag" for action in body["categories"].values())


class TestUpdateOutputLeakConfig:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.patch("/api/admin/output-leak", json={"enabled": True})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"

    @pytest.mark.asyncio
    async def test_partial_update_merges_categories(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak",
            json={"enabled": True, "categories": {"email": "block"}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["categories"]["email"] == "block"
        assert body["categories"]["phone"] == "flag"

        response = await admin_client.patch(
            "/api/admin/output-leak", json={"categories": {"ssn": "redact"}}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["categories"]["email"] == "block"
        assert body["categories"]["ssn"] == "redact"

    @pytest.mark.asyncio
    async def test_invalid_category_rejected(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak", json={"categories": {"not_a_real_category": "block"}}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_category"

    @pytest.mark.asyncio
    async def test_custom_category_key_rejected(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak", json={"categories": {"custom": "block"}}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_category"

    @pytest.mark.asyncio
    async def test_invalid_action_rejected(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak", json={"categories": {"email": "sabotage"}}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_action"

    @pytest.mark.asyncio
    async def test_custom_patterns_full_replace(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak",
            json={"custom_patterns": [{"name": "codename", "pattern": r"\bPhoenix\b", "action": "redact"}]},
        )
        assert response.status_code == 200
        assert response.json()["custom_patterns"] == [
            {"name": "codename", "pattern": r"\bPhoenix\b", "action": "redact"}
        ]

        # A second PATCH with a different list fully replaces, not merges.
        response = await admin_client.patch(
            "/api/admin/output-leak",
            json={"custom_patterns": [{"name": "other", "pattern": r"\bfoo\b", "action": "flag"}]},
        )
        assert response.status_code == 200
        assert response.json()["custom_patterns"] == [
            {"name": "other", "pattern": r"\bfoo\b", "action": "flag"}
        ]

    @pytest.mark.asyncio
    async def test_invalid_custom_pattern_regex_rejected(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak",
            json={"custom_patterns": [{"name": "bad", "pattern": "[unterminated", "action": "flag"}]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_custom_pattern"

    @pytest.mark.asyncio
    async def test_update_takes_effect_immediately_no_restart(self, admin_client, scratch_output_leak_file):
        response = await admin_client.patch(
            "/api/admin/output-leak",
            json={"enabled": True, "categories": {"email": "block"}},
        )
        assert response.status_code == 200

        get_response = await admin_client.get("/api/admin/output-leak")
        assert get_response.json()["enabled"] is True
        assert get_response.json()["categories"]["email"] == "block"


class TestOutputLeakTest:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.post("/api/admin/output-leak/test", json={"text": "hello"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"

    @pytest.mark.asyncio
    async def test_works_even_when_disabled(self, admin_client, scratch_output_leak_file):
        await admin_client.patch("/api/admin/output-leak", json={"categories": {"email": "block"}})
        get_response = await admin_client.get("/api/admin/output-leak")
        assert get_response.json()["enabled"] is False

        response = await admin_client.post(
            "/api/admin/output-leak/test", json={"text": "contact jane.doe@example.com"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "block"
        assert any(m["category"] == "email" for m in body["matches"])

    @pytest.mark.asyncio
    async def test_redact_action_includes_preview(self, admin_client, scratch_output_leak_file):
        await admin_client.patch("/api/admin/output-leak", json={"categories": {"email": "redact"}})
        response = await admin_client.post(
            "/api/admin/output-leak/test", json={"text": "contact jane.doe@example.com now"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "redact"
        assert body["redacted_preview"] == "contact [EMAIL] now"

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty(self, admin_client, scratch_output_leak_file):
        response = await admin_client.post(
            "/api/admin/output-leak/test", json={"text": "what's the weather today?"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "disabled"
        assert body["matches"] == []
        assert body["redacted_preview"] is None

    @pytest.mark.asyncio
    async def test_is_side_effect_free(self, admin_client, scratch_output_leak_file):
        before = (await admin_client.get("/api/admin/output-leak")).json()

        await admin_client.post("/api/admin/output-leak/test", json={"text": "contact jane.doe@example.com"})
        await admin_client.post("/api/admin/output-leak/test", json={"text": "contact jane.doe@example.com"})

        after = (await admin_client.get("/api/admin/output-leak")).json()
        assert after == before
