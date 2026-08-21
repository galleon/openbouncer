import shutil
from pathlib import Path

import pytest

from app.guardrails.editable_config import GuardrailsConfigParseError, write_editable_sections
from app.guardrails.service import DisabledGuardrailsService

REAL_GUARDRAILS_CONFIGS_DIR = Path(__file__).parent.parent / "guardrails_configs"


@pytest.fixture
def self_check_input_copy(tmp_path):
    dest = tmp_path / "self_check_input"
    shutil.copytree(REAL_GUARDRAILS_CONFIGS_DIR / "self_check_input", dest)
    return dest


class TestWriteEditableSectionsWithoutTheOptionalPackage:
    """Simulates `nemoguardrails` not being installed (the `nemo` extra
    omitted, see pyproject.toml) by patching the module's own RailsConfig
    binding to None -- same technique as
    tests/test_guardrails_service.py's equivalent class."""

    @pytest.mark.asyncio
    async def test_raises_before_writing_anything(self, self_check_input_copy, monkeypatch):
        import app.guardrails.editable_config as editable_config_module

        monkeypatch.setattr(editable_config_module, "RailsConfig", None)

        config_path = self_check_input_copy / "config.yml"
        original = config_path.read_text()

        with pytest.raises(GuardrailsConfigParseError, match="nemoguardrails"):
            await write_editable_sections(
                self_check_input_copy,
                "self_check_input",
                {"policy": ["new rule"]},
                DisabledGuardrailsService(),
                actor_key_id="test-admin",
            )

        # Known upfront, before any write -- the file must be untouched,
        # not written-then-rolled-back.
        assert config_path.read_text() == original
