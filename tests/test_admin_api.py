import hashlib
import shutil
from pathlib import Path

import pytest
from nemoguardrails import RailsConfig
from nemoguardrails.testing import FakeLLMModel

from app.auth.keys import CONFIG_PATH_ENV_VAR, CONFIG_YAML_ENV_VAR, DEFAULT_REQUESTS_PER_MINUTE
from app.auth.keys import get_key_store as real_get_key_store
from app.auth.keys import hash_api_key, parse_key_store
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


class TestCreateKey:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.post(
            "/api/admin/keys",
            json={"id": "new-key", "allowed_models": ["local/gemma4-nvfp4"]},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_inline_env_backed_store_rejected(self, admin_client):
        response = await admin_client.post(
            "/api/admin/keys",
            json={"id": "new-key", "allowed_models": ["local/gemma4-nvfp4"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "key_store_not_file_backed"

    @pytest.mark.asyncio
    async def test_invalid_id_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.post(
            "/api/admin/keys",
            json={"id": "not a valid id!", "allowed_models": ["local/gemma4-nvfp4"]},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_key_id"

    @pytest.mark.asyncio
    async def test_empty_allowed_models_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.post(
            "/api/admin/keys",
            json={"id": "new-key", "allowed_models": []},
        )
        # Body-schema violation (min_length=1) -> the generic
        # RequestValidationError handler, which this codebase maps to 400
        # (see app.core.errors.openai_validation_exception_handler), not a
        # route-level OpenAIError(422) like the guardrails config_id checks.
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_duplicate_id_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.post(
            "/api/admin/keys",
            json={"id": "scratch-key", "allowed_models": ["local/gemma4-nvfp4"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "key_already_exists"

    @pytest.mark.asyncio
    async def test_unknown_guardrails_config_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.post(
            "/api/admin/keys",
            json={
                "id": "custom-key",
                "allowed_models": ["local/gemma4-nvfp4"],
                "allowed_guardrails_configs": ["does_not_exist"],
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "guardrails_config_not_found"

    @pytest.mark.asyncio
    async def test_happy_path_returns_raw_key_and_is_live_without_restart(
        self, admin_client, scratch_keys_file
    ):
        response = await admin_client.post(
            "/api/admin/keys",
            json={"id": "brand-new-key", "allowed_models": ["local/gemma4-nvfp4"]},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["key"]["id"] == "brand-new-key"
        assert "key_hash" not in body["key"]
        assert body["key"]["requests_per_minute"] == DEFAULT_REQUESTS_PER_MINUTE
        assert body["key"]["is_admin"] is False
        raw_key = body["api_key"]
        assert raw_key.startswith("sk-")

        on_disk = scratch_keys_file.read_text()
        assert "brand-new-key" in on_disk

        # Live immediately, no restart needed: the raw key returned here
        # authenticates against the real (non-overridden) get_key_store().
        record = real_get_key_store().get_by_hash(hash_api_key(raw_key))
        assert record is not None
        assert record.id == "brand-new-key"

    @pytest.mark.asyncio
    async def test_explicit_fields_round_trip(self, admin_client, scratch_keys_file):
        response = await admin_client.post(
            "/api/admin/keys",
            json={
                "id": "custom-key",
                "allowed_models": ["local/gemma4-nvfp4"],
                "requests_per_minute": 42,
                "is_admin": True,
                "allowed_guardrails_configs": [],
            },
        )
        assert response.status_code == 201
        body = response.json()["key"]
        assert body["requests_per_minute"] == 42
        assert body["is_admin"] is True
        assert body["allowed_guardrails_configs"] == []


class TestUpdateKey:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.patch("/api/admin/keys/test-key", json={"is_admin": True})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.patch(
            "/api/admin/keys/does-not-exist", json={"is_admin": True}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "key_not_found"

    @pytest.mark.asyncio
    async def test_empty_body_is_a_no_op(self, admin_client, scratch_keys_file):
        before = parse_key_store(scratch_keys_file.read_text())
        response = await admin_client.patch("/api/admin/keys/scratch-key", json={})
        assert response.status_code == 200
        # Compare parsed/defaulted records, not raw bytes: a PATCH always
        # re-serializes the whole store (e.g. gains an explicit
        # `is_admin: false` for a field that was previously implicit via
        # APIKeyRecord's default), even when no field the request touched
        # actually changed.
        after = parse_key_store(scratch_keys_file.read_text())
        assert after.get_by_id("scratch-key") == before.get_by_id("scratch-key")

    @pytest.mark.asyncio
    async def test_partial_update_only_touches_provided_fields(
        self, admin_client, scratch_keys_file
    ):
        response = await admin_client.patch(
            "/api/admin/keys/scratch-key", json={"requests_per_minute": 5}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["requests_per_minute"] == 5
        assert body["allowed_models"] == ["local/gemma4-nvfp4"]

    @pytest.mark.asyncio
    async def test_grant_admin_is_live_without_restart(self, admin_client, scratch_keys_file):
        response = await admin_client.patch("/api/admin/keys/scratch-key", json={"is_admin": True})
        assert response.status_code == 200
        assert response.json()["is_admin"] is True
        record = real_get_key_store().get_by_id("scratch-key")
        assert record.is_admin is True

    @pytest.mark.asyncio
    async def test_empty_allowed_models_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.patch(
            "/api/admin/keys/scratch-key", json={"allowed_models": []}
        )
        assert response.status_code == 400


class TestRotateKey:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.post("/api/admin/keys/test-key/rotate")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.post("/api/admin/keys/does-not-exist/rotate")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "key_not_found"

    @pytest.mark.asyncio
    async def test_old_key_stops_new_key_starts(self, admin_client, scratch_keys_file):
        old_hash = real_get_key_store().get_by_id("scratch-key").key_hash
        assert real_get_key_store().get_by_hash(old_hash) is not None

        response = await admin_client.post("/api/admin/keys/scratch-key/rotate")
        assert response.status_code == 200
        body = response.json()
        assert body["key"]["id"] == "scratch-key"
        new_raw_key = body["api_key"]
        assert new_raw_key.startswith("sk-")

        assert real_get_key_store().get_by_hash(old_hash) is None
        new_record = real_get_key_store().get_by_hash(hash_api_key(new_raw_key))
        assert new_record is not None
        assert new_record.id == "scratch-key"


class TestDeleteKey:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.delete("/api/admin/keys/test-key")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, admin_client, scratch_keys_file):
        response = await admin_client.delete("/api/admin/keys/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "key_not_found"

    @pytest.mark.asyncio
    async def test_happy_path_removes_key_and_is_live_without_restart(
        self, admin_client, scratch_keys_file
    ):
        assert real_get_key_store().get_by_id("scratch-key") is not None

        response = await admin_client.delete("/api/admin/keys/scratch-key")
        assert response.status_code == 204

        assert real_get_key_store().get_by_id("scratch-key") is None

        list_response = await admin_client.get("/api/admin/keys")
        assert "scratch-key" not in {k["id"] for k in list_response.json()["keys"]}

    @pytest.mark.asyncio
    async def test_second_delete_is_404(self, admin_client, scratch_keys_file):
        first = await admin_client.delete("/api/admin/keys/scratch-key")
        assert first.status_code == 204
        second = await admin_client.delete("/api/admin/keys/scratch-key")
        assert second.status_code == 404


class TestListGuardrailsConfigs:
    @pytest.mark.asyncio
    async def test_non_admin_rejected(self, client):
        response = await client.get("/api/admin/guardrails/configs")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_lists_all_bundled_presets_with_sections(
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
            "jailbreak_input",
            "topic_blocklist",
            "pii_regex",
        }
        assert by_id["topic_safety"]["editable"] is True
        assert {s["field"] for s in by_id["topic_safety"]["sections"]} == {"allowed_topics"}
        assert {s["field"] for s in by_id["self_check_input_output"]["sections"]} == {
            "input_policy",
            "output_policy",
        }
        assert len(by_id["topic_safety"]["sections"][0]["items"]) == 3  # the 3 bundled topics
        assert {s["field"] for s in by_id["jailbreak_input"]["sections"]} == {"policy"}
        assert {s["field"] for s in by_id["topic_blocklist"]["sections"]} == {"blocked_topics"}
        assert {s["field"] for s in by_id["pii_regex"]["sections"]} == {
            "input_patterns",
            "output_patterns",
        }
        assert len(by_id["pii_regex"]["sections"][0]["items"]) == 3  # the 3 starter patterns


class TestBundledPresetsLoad:
    @pytest.mark.parametrize(
        "config_id",
        [
            "self_check_input",
            "self_check_output",
            "self_check_input_output",
            "topic_safety",
            "jailbreak_input",
            "topic_blocklist",
            "pii_regex",
        ],
    )
    def test_loads_via_rails_config(self, monkeypatch, config_id):
        # A cheap, no-LLM-call smoke test that the shipped config.yml is
        # actually well-formed -- RailsConfig.from_path only parses/
        # validates config shape (Colang flows, rails, and -- for
        # pii_regex -- that its regex patterns compile), it never calls a
        # model.
        monkeypatch.setenv("UPSTREAM_VLLM_API_KEY", "test-key")
        RailsConfig.from_path(str(REAL_GUARDRAILS_CONFIGS_DIR / config_id))


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


class TestUpdateNewPresets:
    @pytest.mark.asyncio
    async def test_jailbreak_input_round_trip(self, admin_client, guardrails_configs_copy):
        response = await admin_client.patch(
            "/api/admin/guardrails/configs/jailbreak_input",
            json={"sections": {"policy": ["asks to roleplay as an unfiltered AI"]}},
        )
        assert response.status_code == 200
        assert response.json()["sections"][0]["items"] == [
            "asks to roleplay as an unfiltered AI"
        ]

    @pytest.mark.asyncio
    async def test_topic_blocklist_round_trip(self, admin_client, guardrails_configs_copy):
        response = await admin_client.patch(
            "/api/admin/guardrails/configs/topic_blocklist",
            json={"sections": {"blocked_topics": ["politics", "religion"]}},
        )
        assert response.status_code == 200
        assert response.json()["sections"][0]["items"] == ["politics", "religion"]

    @pytest.mark.asyncio
    async def test_pii_regex_round_trip(self, admin_client, guardrails_configs_copy):
        response = await admin_client.patch(
            "/api/admin/guardrails/configs/pii_regex",
            json={
                "sections": {
                    "input_patterns": [r"\b\d{3}-\d{2}-\d{4}\b"],
                    "output_patterns": [r"\b\d{3}-\d{2}-\d{4}\b"],
                }
            },
        )
        assert response.status_code == 200
        by_field = {s["field"]: s["items"] for s in response.json()["sections"]}
        assert by_field["input_patterns"] == [r"\b\d{3}-\d{2}-\d{4}\b"]
        assert by_field["output_patterns"] == [r"\b\d{3}-\d{2}-\d{4}\b"]

        get_response = await admin_client.get("/api/admin/guardrails/configs")
        pii_regex = next(
            c for c in get_response.json()["configs"] if c["config_id"] == "pii_regex"
        )
        by_field = {s["field"]: s["items"] for s in pii_regex["sections"]}
        assert by_field["input_patterns"] == [r"\b\d{3}-\d{2}-\d{4}\b"]

    @pytest.mark.asyncio
    async def test_pii_regex_invalid_pattern_rejected_and_file_untouched(
        self, admin_client, guardrails_configs_copy
    ):
        # Exercises write_editable_sections' reload-and-rollback safety
        # net against a real failure mode unique to pii_regex: a syntactically
        # broken regex passes the splice/YAML-validity checks (it's just a
        # string) but makes RailsConfig.from_path's
        # RegexDetectionOptions.compile_patterns validator raise, so the
        # write must be rejected and rolled back like any other bad edit.
        dest, _service = guardrails_configs_copy
        config_path = dest / "pii_regex" / "config.yml"
        before = config_path.read_text()

        response = await admin_client.patch(
            "/api/admin/guardrails/configs/pii_regex",
            json={
                "sections": {
                    "input_patterns": ["[unclosed("],
                    "output_patterns": [r"\b\d{3}-\d{2}-\d{4}\b"],
                }
            },
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "guardrails_config_invalid"
        assert config_path.read_text() == before
