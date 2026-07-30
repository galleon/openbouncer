import pytest
from pydantic import ValidationError

from app.core.registry import (
    CONFIG_PATH_ENV_VAR,
    CONFIG_YAML_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    load_model_registry,
    parse_model_registry,
)

ALLOWED_YAML = """
models:
  - id: nvidia/qwen3.6-nvfp4
    upstream_model: nvidia/Qwen3.6-27B-NVFP4
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    capabilities: [chat]
    concurrency_limit: 8

  - id: nvidia/gemme4-nvfp4
    upstream_model: nvidia/Gemma-4-26B-A4B-NVFP4
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    capabilities: [chat]
    concurrency_limit: 8

  - id: nvidia/nemotron-vision
    upstream_model: nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-NVFP4-QAD
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    capabilities: [chat, vision]
    concurrency_limit: 4
"""


def _single_model_yaml(model_id: str, **overrides) -> str:
    entry = {
        "id": model_id,
        "upstream_model": "nvidia/Qwen3.6-27B-NVFP4",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key_env": "NVIDIA_API_KEY",
        "capabilities": ["chat"],
        "concurrency_limit": 8,
    }
    entry.update(overrides)
    import yaml

    return yaml.safe_dump({"models": [entry]})


class TestAllowedModels:
    def test_all_three_allowed_models_load(self):
        registry = parse_model_registry(ALLOWED_YAML)
        ids = {entry.id for entry in registry.all()}
        assert ids == {
            "nvidia/qwen3.6-nvfp4",
            "nvidia/gemme4-nvfp4",
            "nvidia/nemotron-vision",
        }

    def test_upstream_mapping_is_correct(self):
        registry = parse_model_registry(ALLOWED_YAML)
        assert registry.get("nvidia/qwen3.6-nvfp4").upstream_model == "nvidia/Qwen3.6-27B-NVFP4"
        assert (
            registry.get("nvidia/gemme4-nvfp4").upstream_model
            == "nvidia/Gemma-4-26B-A4B-NVFP4"
        )
        assert (
            registry.get("nvidia/nemotron-vision").upstream_model
            == "nvidia/NVIDIA-Nemotron-Nano-12B-v2-VL-NVFP4-QAD"
        )

    def test_entry_exposes_required_fields(self):
        registry = parse_model_registry(ALLOWED_YAML)
        entry = registry.get("nvidia/nemotron-vision")
        assert entry.base_url == "https://integrate.api.nvidia.com/v1"
        assert entry.api_key_env == "NVIDIA_API_KEY"
        assert entry.capabilities == ["chat", "vision"]
        assert entry.concurrency_limit == 4

    def test_single_allowed_model_loads(self):
        registry = parse_model_registry(_single_model_yaml("nvidia/gemme4-nvfp4"))
        assert "nvidia/gemme4-nvfp4" in registry
        assert "nvidia/qwen3.6-nvfp4" not in registry


class TestArbitraryModelIds:
    """The registry has no fixed allowlist -- any operator-chosen id is
    valid, e.g. for a local Ollama server or an OpenRouter model (see
    config/models.yaml for worked examples of both)."""

    def test_third_party_model_id_accepted(self):
        registry = parse_model_registry(_single_model_yaml("openai/gpt-4o"))
        assert "openai/gpt-4o" in registry

    def test_id_without_provider_prefix_accepted(self):
        registry = parse_model_registry(_single_model_yaml("llama3.2"))
        assert "llama3.2" in registry

    def test_empty_model_id_rejected(self):
        with pytest.raises(ValidationError):
            parse_model_registry(_single_model_yaml(""))


class TestRejectedModels:
    def test_duplicate_model_ids_rejected(self):
        yaml_text = """
models:
  - id: nvidia/qwen3.6-nvfp4
    upstream_model: nvidia/Qwen3.6-27B-NVFP4
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    capabilities: [chat]
    concurrency_limit: 8
  - id: nvidia/qwen3.6-nvfp4
    upstream_model: nvidia/Qwen3.6-27B-NVFP4
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NVIDIA_API_KEY
    capabilities: [chat]
    concurrency_limit: 8
"""
        with pytest.raises(ValidationError):
            parse_model_registry(yaml_text)

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            parse_model_registry(
                _single_model_yaml("nvidia/qwen3.6-nvfp4", concurrency_limit=None)
            )

    def test_unsupported_capability_rejected(self):
        with pytest.raises(ValidationError):
            parse_model_registry(
                _single_model_yaml("nvidia/qwen3.6-nvfp4", capabilities=["audio"])
            )

    def test_non_positive_concurrency_limit_rejected(self):
        with pytest.raises(ValidationError):
            parse_model_registry(
                _single_model_yaml("nvidia/qwen3.6-nvfp4", concurrency_limit=0)
            )

    def test_empty_models_list_rejected(self):
        with pytest.raises(ValidationError):
            parse_model_registry("models: []")

    def test_unrecognized_field_rejected(self):
        with pytest.raises(ValidationError):
            parse_model_registry(
                _single_model_yaml("nvidia/qwen3.6-nvfp4", extra_field="nope")
            )


class TestLoadingSources:
    def test_default_config_file_contains_expected_models(self, monkeypatch):
        monkeypatch.delenv(CONFIG_YAML_ENV_VAR, raising=False)
        monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)
        registry = load_model_registry()
        ids = {entry.id for entry in registry.all()}
        assert ids == {
            "nvidia/qwen3.6-nvfp4",
            "nvidia/gemme4-nvfp4",
            "nvidia/nemotron-vision",
            "ollama/llama3.2",
            "openrouter/claude-3.5-sonnet",
            "ollama/nomic-embed-text",
        }

    def test_default_config_path_exists(self):
        assert DEFAULT_CONFIG_PATH.exists()

    def test_load_from_inline_env_yaml(self, monkeypatch):
        monkeypatch.setenv(CONFIG_YAML_ENV_VAR, _single_model_yaml("nvidia/gemme4-nvfp4"))
        registry = load_model_registry()
        assert {entry.id for entry in registry.all()} == {"nvidia/gemme4-nvfp4"}

    def test_load_from_env_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "models.yaml"
        config_file.write_text(_single_model_yaml("nvidia/nemotron-vision"))
        monkeypatch.delenv(CONFIG_YAML_ENV_VAR, raising=False)
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config_file))
        registry = load_model_registry()
        assert {entry.id for entry in registry.all()} == {"nvidia/nemotron-vision"}

    def test_inline_env_yaml_takes_precedence_over_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "models.yaml"
        config_file.write_text(_single_model_yaml("nvidia/nemotron-vision"))
        monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(config_file))
        monkeypatch.setenv(CONFIG_YAML_ENV_VAR, _single_model_yaml("nvidia/gemme4-nvfp4"))
        registry = load_model_registry()
        assert {entry.id for entry in registry.all()} == {"nvidia/gemme4-nvfp4"}
