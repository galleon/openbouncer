import json
import logging
import os

LOG_LEVEL_ENV_VAR = "LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"

# Attributes every LogRecord has regardless of what a caller logs --
# discovered dynamically (rather than hardcoding a list) so this stays
# correct across Python versions that add fields (e.g. taskName in 3.12).
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Structured (JSON) log output, one object per line.

    Plain %-style formatting silently drops anything passed via
    `logger.info(msg, extra={...})` unless the format string explicitly
    names each field -- which nothing in this codebase's format string
    did, so request_id/method/path/status_code/duration_ms etc. were
    computed by RequestLoggingMiddleware and then just discarded. JSON
    output includes every `extra` field automatically, and is what a real
    log aggregator (Loki, CloudWatch, ELK, ...) actually wants to ingest.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_LOG_RECORD_ATTRS
            }
        )
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    raw_level = os.environ.get(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL).upper()
    level = logging.getLevelName(raw_level)
    if not isinstance(level, int):
        # getLevelName(<unknown string>) returns "Level <name>" instead of
        # raising, so an unrecognized value would otherwise be silently
        # accepted as a bogus level -- fall back to the default instead.
        level = logging.getLevelName(DEFAULT_LOG_LEVEL)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    # force=True so this always takes effect even if something else (a test
    # runner, an import side effect) configured the root logger first.
    logging.basicConfig(level=level, handlers=[handler], force=True)
