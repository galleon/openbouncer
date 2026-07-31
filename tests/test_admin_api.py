import hashlib
import shutil
from pathlib import Path

import pytest
from nemoguardrails.testing import FakeLLMModel

from app.auth.keys import CONFIG_PATH_ENV_VAR, CONFIG_YAML_ENV_VAR
from app.auth.keys import get_key_store as real_get_key_store
from app.guardrails.catalog import GuardrailsCatalogService, get_guardrails_catalog_service
from app.guardrails.service import NemoLibraryGuardrailsService, get_guardrails_service
from app.main import app
from app.schemas.chat import ChatCompletionRequest

REAL_GUARDRAILS_CONFIGS_DIR = Path(__file__).parent.parent / "guardrails_configs"

SCRATCH_KEY_HASH = hashlib.sha256(b"sk-scratch-key").hexdigest()
SCRATCH_YAML = f"""
keys:
  - id: scratch-key
    key_hash: {SCRATCH_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 60
"""


@pytest.fixture
def scratch_keys_file(tmp_path, monkeypatch):
    """A real, file-backed api_keys.yaml the admin PATCH endpoint can
    persist to -- admin.py's persistence layer reads/writes the file
    directly (see app.auth.keys._writable_path), independent of whatever
    get_key_store is dependency-overridden to for the request's own auth,
    so this needs to be set up separately from admin_client's fixture."""
    path = tmp_path / "api_keys.yaml"
    path.write_text(SCRATCH_YAML)
    monkeypatch.delenv(CONFIG_YAML_ENV_VAR, raising=False)
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(path))
    # update_key_allowed_guardrails_configs() clears the module-level
    # get_key_store() lru_cache as part of "live without restart" -- if
    # anything (including this fixture's own tests) repopulates that cache
    # while CONFIG_PATH_ENV_VAR points at this scratch file, the cached
    # KeyStore would outlive monkeypatch's teardown and poison every other
    # test's real (non-overridden) get_key_store() calls afterward.
    real_get_key_store.cache_clear()
    try:
        yield path
    finally:
        real_get_key_store.cache_clear()


@pytest.fixture
def guardrails_configs_copy(tmp_path, monkeypatch):
    """A scratch copy of the real guardrails_configs/ (not
    tests/fixtures/guardrails_configs/, which is a simplified shape that
    doesn't cover topic_safety or self_check_input_output at all), with the
    catalog/library service dependencies overridden to point at it -- never
    touches the real guardrails_configs/ during tests."""
    dest = tmp_path / "guardrails_configs"
    shutil.copytree(REAL_GUARDRAILS_CONFIGS_DIR, dest)
    monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")

    catalog = GuardrailsCatalogService(config_store_path=str(dest))
    service = NemoLibraryGuardrailsService(config_store_path=str(dest))
    app.dependency_overrides[get_guardrails_catalog_service] = lambda: catalog
    app.dependency_overrides[get_guardrails_service] = lambda: service
    try:
        yield dest, service
    finally:
        app.dependency_overrides.pop(get_guardrails_catalog_service, None)
        app.dependency_overrides.pop(get_guardrails_service, None)


class TestListKeys:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.get("/api/admin/keys")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_required"

    @pytest.mark.asyncio
    async def test_admin_sees_keys_without_hash(self, admin_client):
        response = await admin_client.get("/api/admin/keys")
        assert response.status_code == 200
        body = response.json()
        by_id = {k["id"]: k for k in body["keys"]}
        assert "admin-key" in by_id
        assert "key_hash" not in by_id["admin-key"]
        assert by_id["admin-key"]["is_admin"] is True
        assert by_id["admin-key"]["allowed_guardrails_configs"] == ["no_rails", "self_check_output"]

        # unrestricted-key (see conftest.py) never had allowed_guardrails_configs
        # set -- the API must round-trip that as null, not an empty list,
        # since those mean different things (unrestricted vs. locked out).
        assert by_id["unrestricted-key"]["allowed_guardrails_configs"] is None


class TestUpdateKeyGuardrailsConfigs:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.patch(
            "/api/admin/keys/test-key/guardrails-configs",
            json={"allowed_guardrails_configs": ["self_check_input"]},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_config_id_rejected(self, admin_client):
        response = await admin_client.patch(
            "/api/admin/keys/admin-key/guardrails-configs",
            json={"allowed_guardrails_configs": ["does_not_exist"]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "guardrails_config_not_found"

    @pytest.mark.asyncio
    async def test_malformed_config_id_rejected(self, admin_client):
        response = await admin_client.patch(
            "/api/admin/keys/admin-key/guardrails-configs",
            json={"allowed_guardrails_configs": ["not a valid id!"]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_config_id"

    @pytest.mark.asyncio
    async def test_inline_env_backed_store_rejected(self, admin_client):
        # conftest.py sets OPENBOUNCER_API_KEYS_YAML globally, so the store
        # is inline-env-backed by default in every test unless overridden --
        # this is exactly the case the persistence layer must reject cleanly.
        response = await admin_client.patch(
            "/api/admin/keys/admin-key/guardrails-configs",
            json={"allowed_guardrails_configs": ["self_check_input"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "key_store_not_file_backed"

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.patch(
            "/api/admin/keys/does-not-exist/guardrails-configs",
            json={"allowed_guardrails_configs": ["self_check_input"]},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "key_not_found"

    @pytest.mark.asyncio
    async def test_happy_path_persists_and_is_live_without_restart(
        self, admin_client, scratch_keys_file
    ):
        response = await admin_client.patch(
            "/api/admin/keys/scratch-key/guardrails-configs",
            json={"allowed_guardrails_configs": ["self_check_input", "topic_safety"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "scratch-key"
        assert body["allowed_guardrails_configs"] == ["self_check_input", "topic_safety"]

        # Persisted to disk...
        on_disk = scratch_keys_file.read_text()
        assert "self_check_input" in on_disk
        assert "topic_safety" in on_disk

        # ...and live immediately: get_key_store() (module-level lru_cache,
        # not the request-scoped override admin_client uses) must reflect
        # it on the very next call, no restart.
        record = real_get_key_store().get_by_id("scratch-key")
        assert record.allowed_guardrails_configs == ["self_check_input", "topic_safety"]


class TestListGuardrailsConfigs:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.get("/api/admin/guardrails/configs")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_lists_all_four_presets_with_sections(
        self, admin_client, guardrails_configs_copy
    ):
        response = await admin_client.get("/api/admin/guardrails/configs")
        assert response.status_code == 200
        by_id = {c["config_id"]: c for c in response.json()["configs"]}
        assert set(by_id) == {
            "self_check_input",
            "self_check_output",
            "self_check_input_output",
            "topic_safety",
        }
        assert by_id["topic_safety"]["editable"] is True
        assert {s["field"] for s in by_id["topic_safety"]["sections"]} == {"allowed_topics"}
        assert {s["field"] for s in by_id["self_check_input_output"]["sections"]} == {
            "input_policy",
            "output_policy",
        }
        assert len(by_id["topic_safety"]["sections"][0]["items"]) == 3  # the 3 bundled topics


class TestUpdateGuardrailsConfig:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.patch(
            "/api/admin/guardrails/configs/topic_safety",
            json={"sections": {"allowed_topics": ["a"]}},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_config_id_rejected(self, admin_client, guardrails_configs_copy):
        response = await admin_client.patch(
            "/api/admin/guardrails/configs/does_not_exist",
            json={"sections": {"x": ["a"]}},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "guardrails_config_not_found"

    @pytest.mark.asyncio
    async def test_round_trip(self, admin_client, guardrails_configs_copy):
        response = await admin_client.patch(
            "/api/admin/guardrails/configs/topic_safety",
            json={"sections": {"allowed_topics": ["cooking", "gardening"]}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sections"][0]["items"] == ["cooking", "gardening"]

        get_response = await admin_client.get("/api/admin/guardrails/configs")
        topic_safety = next(
            c for c in get_response.json()["configs"] if c["config_id"] == "topic_safety"
        )
        assert topic_safety["sections"][0]["items"] == ["cooking", "gardening"]

    @pytest.mark.asyncio
    async def test_preserves_surrounding_text(self, admin_client, guardrails_configs_copy):
        dest, _service = guardrails_configs_copy
        config_path = dest / "topic_safety" / "config.yml"
        before = config_path.read_text()

        await admin_client.patch(
            "/api/admin/guardrails/configs/topic_safety",
            json={"sections": {"allowed_topics": ["a", "b", "c"]}},
        )

        after = config_path.read_text()
        before_prefix, _, before_rest = before.partition("Allowed topics:\n")
        after_prefix, _, after_rest = after.partition("Allowed topics:\n")
        assert before_prefix == after_prefix

        # Everything from the blank line after the bullet list onward
        # (the "Any other topic..." closing text) must be byte-identical.
        before_tail = before_rest.split("\n\n", 1)[1]
        after_tail = after_rest.split("\n\n", 1)[1]
        assert before_tail == after_tail

    @pytest.mark.asyncio
    async def test_empty_items_rejected_and_file_untouched(
        self, admin_client, guardrails_configs_copy
    ):
        dest, _service = guardrails_configs_copy
        config_path = dest / "topic_safety" / "config.yml"
        before = config_path.read_text()

        response = await admin_client.patch(
            "/api/admin/guardrails/configs/topic_safety",
            json={"sections": {"allowed_topics": []}},
        )
        assert response.status_code == 422
        assert config_path.read_text() == before

    @pytest.mark.asyncio
    async def test_wrong_field_set_rejected_and_file_untouched(
        self, admin_client, guardrails_configs_copy
    ):
        dest, _service = guardrails_configs_copy
        config_path = dest / "topic_safety" / "config.yml"
        before = config_path.read_text()

        response = await admin_client.patch(
            "/api/admin/guardrails/configs/topic_safety",
            json={"sections": {"wrong_field": ["a"]}},
        )
        assert response.status_code == 422
        assert config_path.read_text() == before

    @pytest.mark.asyncio
    async def test_malformed_file_fails_cleanly_without_corruption(
        self, admin_client, guardrails_configs_copy
    ):
        dest, _service = guardrails_configs_copy
        config_path = dest / "topic_safety" / "config.yml"
        corrupted = config_path.read_text().replace("Allowed topics:", "Approved subjects:")
        config_path.write_text(corrupted)

        response = await admin_client.patch(
            "/api/admin/guardrails/configs/topic_safety",
            json={"sections": {"allowed_topics": ["x"]}},
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "guardrails_config_invalid"
        assert config_path.read_text() == corrupted

    @pytest.mark.asyncio
    async def test_update_invalidates_cache_live(self, admin_client, guardrails_configs_copy):
        # self_check_output (unlike topic_safety) has only one configured
        # model ("main"), which nemoguardrails' llm= override in
        # LLMRails.__init__ actually patches -- topic_safety's second
        # "topic_guard" model would still try a real network call the test
        # sandbox can't make. responses=[bot reply, self-check answer]
        # matches tests/test_nemo_library_guardrails.py's pattern.
        dest, _service = guardrails_configs_copy
        fake_service = NemoLibraryGuardrailsService(
            config_store_path=str(dest),
            llm_factory=lambda: FakeLLMModel(responses=["Hello there!", "No"]),
        )
        app.dependency_overrides[get_guardrails_service] = lambda: fake_service

        request = ChatCompletionRequest(
            model="test-model",
            messages=[{"role": "user", "content": "hi"}],
            guardrails={"config_id": "self_check_output"},
        )
        await fake_service.process_chat_completion(request)
        assert "self_check_output" in fake_service._rails_cache

        response = await admin_client.patch(
            "/api/admin/guardrails/configs/self_check_output",
            json={"sections": {"policy": ["a", "b"]}},
        )
        assert response.status_code == 200
        assert "self_check_output" not in fake_service._rails_cache
