import hashlib

import pytest
from pydantic import ValidationError

from app.auth.keys import (
    CONFIG_PATH_ENV_VAR,
    CONFIG_YAML_ENV_VAR,
    KeyStore,
    hash_api_key,
    load_key_store,
    parse_key_store,
)

KEY_A = "sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
KEY_B = "sk-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
HASH_A = hashlib.sha256(KEY_A.encode()).hexdigest()
HASH_B = hashlib.sha256(KEY_B.encode()).hexdigest()

VALID_YAML = f"""
keys:
  - id: key-a
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
    requests_per_minute: 30
  - id: key-b
    key_hash: {HASH_B}
    allowed_models: [nvidia/gemme4-nvfp4, nvidia/nemotron-vision]
    requests_per_minute: 10
"""


class TestHashApiKey:
    def test_matches_sha256(self):
        assert hash_api_key(KEY_A) == HASH_A

    def test_different_keys_hash_differently(self):
        assert hash_api_key(KEY_A) != hash_api_key(KEY_B)


class TestParseKeyStore:
    def test_valid_config_loads(self):
        store = parse_key_store(VALID_YAML)
        record = store.get_by_hash(HASH_A)
        assert record is not None
        assert record.id == "key-a"
        assert record.allowed_models == ["nvidia/qwen3.6-nvfp4"]
        assert record.requests_per_minute == 30

    def test_lookup_by_id(self):
        store = parse_key_store(VALID_YAML)
        assert store.get_by_id("key-b").key_hash == HASH_B

    def test_unknown_hash_returns_none(self):
        store = parse_key_store(VALID_YAML)
        assert store.get_by_hash("0" * 64) is None

    def test_empty_config_yields_empty_store(self):
        store = parse_key_store("keys: []")
        assert store.all() == []

    def test_missing_keys_field_yields_empty_store(self):
        store = parse_key_store("{}")
        assert store.all() == []

    def test_default_requests_per_minute(self):
        store = parse_key_store(
            f"""
keys:
  - id: k
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
"""
        )
        assert store.get_by_id("k").requests_per_minute == 60

    def test_duplicate_ids_rejected(self):
        yaml_text = f"""
keys:
  - id: dup
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
  - id: dup
    key_hash: {HASH_B}
    allowed_models: [nvidia/qwen3.6-nvfp4]
"""
        with pytest.raises(ValidationError):
            parse_key_store(yaml_text)

    def test_duplicate_hashes_rejected(self):
        yaml_text = f"""
keys:
  - id: a
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
  - id: b
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
"""
        with pytest.raises(ValidationError):
            parse_key_store(yaml_text)

    def test_arbitrary_model_ids_in_allowed_models_accepted(self):
        # allowed_models isn't cross-checked against the model registry at
        # config-load time (the registry has no fixed set to check against);
        # a request for a misspelled/nonexistent model is still rejected at
        # request time (model_not_found / model_not_allowed).
        yaml_text = f"""
keys:
  - id: a
    key_hash: {HASH_A}
    allowed_models: [openai/gpt-4o, ollama/llama3.2]
"""
        store = parse_key_store(yaml_text)
        assert store.get_by_id("a").allowed_models == ["openai/gpt-4o", "ollama/llama3.2"]

    def test_empty_allowed_models_rejected(self):
        yaml_text = f"""
keys:
  - id: a
    key_hash: {HASH_A}
    allowed_models: []
"""
        with pytest.raises(ValidationError):
            parse_key_store(yaml_text)

    @pytest.mark.parametrize(
        "bad_hash",
        ["too-short", "0" * 63, "g" * 64, "0" * 65],
    )
    def test_malformed_hash_rejected(self, bad_hash):
        yaml_text = f"""
keys:
  - id: a
    key_hash: {bad_hash}
    allowed_models: [nvidia/qwen3.6-nvfp4]
"""
        with pytest.raises(ValidationError):
            parse_key_store(yaml_text)

    def test_unrecognized_field_rejected(self):
        yaml_text = f"""
keys:
  - id: a
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
    extra_field: nope
"""
        with pytest.raises(ValidationError):
            parse_key_store(yaml_text)

    def test_non_positive_rate_limit_rejected(self):
        yaml_text = f"""
keys:
  - id: a
    key_hash: {HASH_A}
    allowed_models: [nvidia/qwen3.6-nvfp4]
    requests_per_minute: 0
"""
        with pytest.raises(ValidationError):
            parse_key_store(yaml_text)


class TestLoadKeyStore:
    def test_defaults_to_empty_store_when_unconfigured(self, monkeypatch, tmp_path):
        monkeypatch.delenv(CONFIG_YAML_ENV_VAR, raising=False)
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        # Point the default path at a location that doesn't exist without
        # touching the real default (avoids depending on repo-root state).
        import app.auth.keys as keys_module

        monkeypatch.setattr(keys_module, "DEFAULT_CONFIG_PATH", tmp_path / "does_not_exist.yaml")
        store = load_key_store()
        assert store.all() == []

    def test_loads_from_inline_env_yaml(self, monkeypatch):
        monkeypatch.setenv(CONFIG_YAML_ENV_VAR, VALID_YAML)
        store = load_key_store()
        assert store.get_by_hash(HASH_A) is not None

    def test_loads_from_env_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "keys.yaml"
        config_file.write_text(VALID_YAML)
        monkeypatch.delenv(CONFIG_YAML_ENV_VAR, raising=False)
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config_file))
        store = load_key_store()
        assert store.get_by_hash(HASH_B) is not None

    def test_explicit_missing_path_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv(CONFIG_YAML_ENV_VAR, raising=False)
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "nope.yaml"))
        with pytest.raises(FileNotFoundError):
            load_key_store()

    def test_inline_yaml_takes_precedence_over_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "keys.yaml"
        config_file.write_text(f"""
keys:
  - id: from-file
    key_hash: {HASH_B}
    allowed_models: [nvidia/qwen3.6-nvfp4]
""")
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config_file))
        monkeypatch.setenv(CONFIG_YAML_ENV_VAR, VALID_YAML)
        store = load_key_store()
        assert store.get_by_id("from-file") is None
        assert store.get_by_id("key-a") is not None


def test_key_store_contains_operator():
    store: KeyStore = parse_key_store(VALID_YAML)
    assert HASH_A in store
    assert ("0" * 64) not in store
