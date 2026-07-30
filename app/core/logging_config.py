import logging
import os

LOG_LEVEL_ENV_VAR = "LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"


def configure_logging() -> None:
    raw_level = os.environ.get(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL).upper()
    level = logging.getLevelName(raw_level)
    if not isinstance(level, int):
        # getLevelName(<unknown string>) returns "Level <name>" instead of
        # raising, so an unrecognized value would otherwise be silently
        # accepted as a bogus level -- fall back to the default instead.
        level = logging.getLevelName(DEFAULT_LOG_LEVEL)

    # force=True so this always takes effect even if something else (a test
    # runner, an import side effect) configured the root logger first.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
