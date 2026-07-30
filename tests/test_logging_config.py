import logging

from app.core.logging_config import DEFAULT_LOG_LEVEL, LOG_LEVEL_ENV_VAR, configure_logging


def test_defaults_to_info(monkeypatch):
    monkeypatch.delenv(LOG_LEVEL_ENV_VAR, raising=False)
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_respects_log_level_env_var(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "DEBUG")
    configure_logging()
    assert logging.getLogger().level == logging.DEBUG


def test_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "warning")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING


def test_unknown_level_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "NOT_A_REAL_LEVEL")
    configure_logging()
    assert logging.getLogger().level == logging.getLevelName(DEFAULT_LOG_LEVEL)
