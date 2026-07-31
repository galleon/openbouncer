from pathlib import Path

import pytest

from app.guardrails.catalog import DEFAULT_PRESETS, GuardrailsCatalogService, get_guardrails_catalog_service
from app.main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "guardrails_configs"


@pytest.fixture
def guardrails_catalog_override():
    service = GuardrailsCatalogService(config_store_path=str(FIXTURES_DIR))
    app.dependency_overrides[get_guardrails_catalog_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_guardrails_catalog_service, None)


class TestUIModelsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self, unauthenticated_client):
        response = await unauthenticated_client.get("/api/ui/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "missing_api_key"

    @pytest.mark.asyncio
    async def test_returns_allowed_models_with_capabilities(self, client):
        response = await client.get("/api/ui/models")
        assert response.status_code == 200
        body = response.json()

        by_id = {item["id"]: item["capabilities"] for item in body["data"]}
        assert by_id == {
            "local/gemma4-nvfp4": ["chat"],
            "local/bge-m3": ["embeddings"],
        }

    @pytest.mark.asyncio
    async def test_filters_by_key_allowed_models(self, restricted_client):
        response = await restricted_client.get("/api/ui/models")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["data"]}
        assert ids == {"local/gemma4-nvfp4"}


class TestGuardrailsConfigsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self, unauthenticated_client):
        response = await unauthenticated_client.get("/api/ui/guardrails/configs")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_returns_discovered_configs_and_presets(
        self, client, guardrails_catalog_override
    ):
        response = await client.get("/api/ui/guardrails/configs")
        assert response.status_code == 200
        body = response.json()

        config_ids = {c["config_id"] for c in body["configs"]}
        assert config_ids == {"no_rails", "self_check_output"}

        preset_ids = [p["id"] for p in body["presets"]]
        assert preset_ids == [p.id for p in DEFAULT_PRESETS]

        default_preset = next(p for p in body["presets"] if p["id"] == "default")
        assert default_preset["example_message"] is None

        jailbreak_preset = next(p for p in body["presets"] if p["id"] == "jailbreak-attempt")
        assert jailbreak_preset["example_message"]

    @pytest.mark.asyncio
    async def test_empty_when_no_configs_directory(self, client, tmp_path):
        service = GuardrailsCatalogService(config_store_path=str(tmp_path / "does-not-exist"))
        app.dependency_overrides[get_guardrails_catalog_service] = lambda: service
        try:
            response = await client.get("/api/ui/guardrails/configs")
            assert response.status_code == 200
            body = response.json()
            assert body["configs"] == []
            assert len(body["presets"]) == len(DEFAULT_PRESETS)
        finally:
            app.dependency_overrides.pop(get_guardrails_catalog_service, None)
