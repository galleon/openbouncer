from pathlib import Path

from app.guardrails.catalog import (
    DEFAULT_PRESETS,
    GuardrailsCatalogService,
    _config_store_path_from_env,
)
from app.guardrails.service import NEMO_LIBRARY_CONFIG_PATH_ENV_VAR

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "guardrails_configs"


class TestListConfigs:
    def test_discovers_configs_in_fixtures_dir(self):
        service = GuardrailsCatalogService(config_store_path=str(FIXTURES_DIR))
        ids = {c.config_id for c in service.list_configs()}
        assert ids == {"no_rails", "self_check_output"}

    def test_results_are_sorted(self):
        service = GuardrailsCatalogService(config_store_path=str(FIXTURES_DIR))
        ids = [c.config_id for c in service.list_configs()]
        assert ids == sorted(ids)

    def test_nonexistent_store_path_returns_empty(self, tmp_path):
        service = GuardrailsCatalogService(config_store_path=str(tmp_path / "nope"))
        assert service.list_configs() == []

    def test_skips_non_directory_entries(self, tmp_path):
        (tmp_path / "not_a_config").mkdir()
        (tmp_path / "not_a_config" / "config.yml").write_text("models: []\n")
        (tmp_path / "stray_file.txt").write_text("hello\n")

        service = GuardrailsCatalogService(config_store_path=str(tmp_path))
        ids = {c.config_id for c in service.list_configs()}
        assert ids == {"not_a_config"}

    def test_skips_directories_missing_config_file(self, tmp_path):
        (tmp_path / "empty_dir").mkdir()
        (tmp_path / "real_config").mkdir()
        (tmp_path / "real_config" / "config.yaml").write_text("models: []\n")

        service = GuardrailsCatalogService(config_store_path=str(tmp_path))
        ids = {c.config_id for c in service.list_configs()}
        assert ids == {"real_config"}

    def test_skips_directories_with_invalid_config_id_characters(self, tmp_path):
        valid = tmp_path / "valid-config_1"
        valid.mkdir()
        (valid / "config.yml").write_text("models: []\n")

        invalid = tmp_path / "not a valid id!"
        invalid.mkdir()
        (invalid / "config.yml").write_text("models: []\n")

        service = GuardrailsCatalogService(config_store_path=str(tmp_path))
        ids = {c.config_id for c in service.list_configs()}
        assert ids == {"valid-config_1"}

    def test_does_not_parse_config_contents(self, tmp_path):
        # Even a config.yml that would fail RailsConfig validation should
        # still be listed -- the catalog only checks the directory layout.
        broken = tmp_path / "broken_config"
        broken.mkdir()
        (broken / "config.yml").write_text("this: [is, not, }}} valid nemoguardrails config\n")

        service = GuardrailsCatalogService(config_store_path=str(tmp_path))
        ids = {c.config_id for c in service.list_configs()}
        assert ids == {"broken_config"}


class TestListPresets:
    def test_returns_fixed_presets(self):
        service = GuardrailsCatalogService(config_store_path=str(FIXTURES_DIR))
        presets = service.list_presets()
        ids = [p.id for p in presets]
        assert ids == [p.id for p in DEFAULT_PRESETS]

    def test_presets_are_independent_copies(self):
        service = GuardrailsCatalogService(config_store_path=str(FIXTURES_DIR))
        presets = service.list_presets()
        presets.append("mutate me")
        assert len(service.list_presets()) == len(DEFAULT_PRESETS)


def test_config_store_path_from_env_defaults(monkeypatch):
    monkeypatch.delenv(NEMO_LIBRARY_CONFIG_PATH_ENV_VAR, raising=False)
    from app.guardrails.catalog import DEFAULT_NEMO_LIBRARY_CONFIG_PATH

    assert _config_store_path_from_env() == DEFAULT_NEMO_LIBRARY_CONFIG_PATH


def test_config_store_path_from_env_respects_override(monkeypatch):
    monkeypatch.setenv(NEMO_LIBRARY_CONFIG_PATH_ENV_VAR, "/custom/path")
    assert _config_store_path_from_env() == "/custom/path"
