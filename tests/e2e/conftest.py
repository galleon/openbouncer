"""Fixtures for the browser-based UI smoke tests (see test_ui_smoke.py).

Opt-in, not part of the default `uv run pytest` run (see the `e2e` marker
and `addopts` in pyproject.toml) -- these need a downloaded browser binary
(`uv sync --extra e2e && uv run playwright install chromium`), not just a
Python package, so they're kept out of the fast suite CI already runs on
every push.
"""

import hashlib
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright not installed -- run `uv sync --extra e2e && uv run playwright install chromium`",
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# A real, working raw key/hash pair for the one admin key the live e2e
# server is seeded with -- not a secret (only ever used against a
# throwaway server on localhost, torn down at the end of the test
# session).
ADMIN_RAW_KEY = "sk-e2e-admin-0123456789abcdef0123456789abcdef"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Server at {url} did not come up in time: {last_error}")


@pytest.fixture(scope="session")
def live_server_url(tmp_path_factory):
    """Runs the real OpenBouncer app via `uvicorn`, in a genuinely separate
    OS process -- not in-process against the same `app.main:app` object
    the rest of the pytest session already imported. Two reasons:

    1. Playwright drives a real browser making real HTTP requests, so
       (unlike the rest of this suite's ASGITransport-based tests) an
       actual listening server is required, not an in-process ASGI call.
    2. This session's other tests may have already poisoned an
       `@lru_cache`d dependency (get_key_store, get_prompt_injection_config,
       ...) with state from a monkeypatched env var whose teardown the
       cache itself has no way to know about. A subprocess with its own
       fresh environment sidesteps that entirely, and as a side effect
       exercises the actual thing that gets deployed rather than a
       specially-poked instance.

    Session-scoped: this is a read-mostly smoke suite (does the page
    render, does entering a key unlock sections) -- not a test of
    concurrent/stateful admin writes, which already have real coverage in
    test_admin_api.py against the ASGI app directly, no browser needed.
    """
    tmp_dir = tmp_path_factory.mktemp("e2e")

    key_hash = hashlib.sha256(ADMIN_RAW_KEY.encode()).hexdigest()
    keys_path = tmp_dir / "api_keys.yaml"
    keys_path.write_text(
        f"""
keys:
  - id: e2e-admin
    key_hash: {key_hash}
    allowed_models: [local/gemma4-nvfp4]
    is_admin: true
"""
    )

    port = _free_port()
    env = os.environ.copy()
    # Must not win over OPENBOUNCER_API_KEYS_CONFIG below (see
    # app.auth.keys._resolve_raw_yaml -- the inline-YAML env var always
    # wins over a config path).
    env.pop("OPENBOUNCER_API_KEYS_YAML", None)
    env.update(
        {
            "OPENBOUNCER_API_KEYS_CONFIG": str(keys_path),
            "GUARDRAILS_MODE": "disabled",
            # Same isolation as tests/conftest.py's autouse fixtures for
            # the rest of the suite -- never touch the real repo files.
            "OPENBOUNCER_AUDIT_LOG_PATH": str(tmp_dir / "audit_log.jsonl"),
            "OPENBOUNCER_GUARDRAIL_EVENTS_PATH": str(tmp_dir / "guardrail_events.jsonl"),
            "OPENBOUNCER_PROMPT_INJECTION_CONFIG": str(tmp_dir / "prompt_injection.yaml"),
            "OPENBOUNCER_OUTPUT_LEAK_CONFIG": str(tmp_dir / "output_leak.yaml"),
        }
    )

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_up(f"{base_url}/healthz")
    except Exception:
        process.terminate()
        output = process.stdout.read().decode(errors="replace") if process.stdout else ""
        process.wait(timeout=5)
        raise RuntimeError(f"uvicorn failed to start for the e2e smoke tests:\n{output}")

    yield base_url

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="session")
def admin_raw_key() -> str:
    return ADMIN_RAW_KEY
