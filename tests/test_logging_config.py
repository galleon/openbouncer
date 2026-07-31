import json
import logging

from app.core.logging_config import DEFAULT_LOG_LEVEL, LOG_LEVEL_ENV_VAR, JsonFormatter, configure_logging


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


class TestJsonFormatter:
    def _format(self, **extra) -> dict:
        record = logging.LogRecord(
            name="openbouncer.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="request end",
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return json.loads(JsonFormatter().format(record))

    def test_includes_standard_fields(self):
        payload = self._format()
        assert payload["level"] == "INFO"
        assert payload["logger"] == "openbouncer.access"
        assert payload["message"] == "request end"
        assert "timestamp" in payload

    def test_includes_extra_fields(self):
        # This is the actual bug being fixed: extra={...} passed to
        # logger.info used to be silently dropped by the old %-style
        # format string, which never referenced these field names.
        payload = self._format(
            request_id="abc123", method="POST", path="/v1/chat/completions", status_code=200, duration_ms=12.5
        )
        assert payload["request_id"] == "abc123"
        assert payload["method"] == "POST"
        assert payload["path"] == "/v1/chat/completions"
        assert payload["status_code"] == 200
        assert payload["duration_ms"] == 12.5

    def test_does_not_leak_internal_logrecord_attrs(self):
        payload = self._format(request_id="abc123")
        # Only the fields we explicitly set + the standard summary fields
        # should show up -- not LogRecord internals like `msg`/`args`/`exc_info`.
        assert set(payload) == {"timestamp", "level", "logger", "message", "request_id"}
