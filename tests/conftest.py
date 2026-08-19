import hashlib
import os

import pytest
from httpx import ASGITransport, AsyncClient

# Configured once, before any test runs (conftest.py is imported at
# collection time, before app.auth.keys.get_key_store() is ever invoked), so
# every test that goes through the real /v1/* routes authenticates as this
# key by default. Granted access to all registry models with an effectively
# unlimited rate limit, since the whole test suite runs in one process and
# shares the same cached RateLimiter -- a realistic per-minute limit would
# make unrelated tests flaky once enough of the suite's requests accumulate.
TEST_API_KEY = "sk-test-0123456789abcdef0123456789abcdef"
TEST_API_KEY_HASH = hashlib.sha256(TEST_API_KEY.encode("utf-8")).hexdigest()

os.environ["OPENBOUNCER_API_KEYS_YAML"] = f"""
keys:
  - id: test-key
    key_hash: {TEST_API_KEY_HASH}
    allowed_models:
      - local/gemma4-nvfp4
      - local/bge-m3
    allowed_guardrails_configs:
      - no_rails
      - self_check_output
      - does_not_exist
    requests_per_minute: 1000000
"""

from app.auth.keys import get_key_store, parse_key_store  # noqa: E402
from app.auth.rate_limiter import RateLimiter, get_rate_limiter  # noqa: E402
from app.main import app  # noqa: E402  (must import after env vars are set above)

RESTRICTED_KEY = "sk-restricted-abcdefabcdefabcdefabcdef"
RESTRICTED_HASH = hashlib.sha256(RESTRICTED_KEY.encode()).hexdigest()
RESTRICTED_STORE_YAML = f"""
keys:
  - id: restricted-key
    key_hash: {RESTRICTED_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
"""

LOW_LIMIT_KEY = "sk-lowlimit-abcdefabcdefabcdefabcdef"
LOW_LIMIT_HASH = hashlib.sha256(LOW_LIMIT_KEY.encode()).hexdigest()
LOW_LIMIT_STORE_YAML = f"""
keys:
  - id: low-limit-key
    key_hash: {LOW_LIMIT_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 2
"""

ADMIN_API_KEY = "sk-admin-abcdefabcdefabcdefabcdef"
ADMIN_API_KEY_HASH = hashlib.sha256(ADMIN_API_KEY.encode()).hexdigest()
UNRESTRICTED_KEY_HASH = hashlib.sha256(b"sk-unrestricted-key").hexdigest()
ADMIN_STORE_YAML = f"""
keys:
  - id: admin-key
    key_hash: {ADMIN_API_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    is_admin: true
    allowed_guardrails_configs: [no_rails, self_check_output]
    requests_per_minute: 1000000
  - id: unrestricted-key
    key_hash: {UNRESTRICTED_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
"""

OBSERVER_KEY = "sk-observer-abcdefabcdefabcdefabcdef"
OBSERVER_KEY_HASH = hashlib.sha256(OBSERVER_KEY.encode()).hexdigest()
OBSERVER_STORE_YAML = f"""
keys:
  - id: observer-key
    key_hash: {OBSERVER_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    admin_scopes: [metrics:read, activity:read]
    requests_per_minute: 1000000
"""

GUARDRAILS_KEY = "sk-guardrails-abcdefabcdefabcdefabcdef"
GUARDRAILS_KEY_HASH = hashlib.sha256(GUARDRAILS_KEY.encode()).hexdigest()
GUARDRAILS_KEY_STORE_YAML = f"""
keys:
  - id: guardrails-key
    key_hash: {GUARDRAILS_KEY_HASH}
    allowed_models: [local/gemma4-nvfp4]
    allowed_guardrails_configs: [no_rails]
    requests_per_minute: 1000000
"""


@pytest.fixture(autouse=True)
def _isolated_audit_log(tmp_path, monkeypatch):
    """Every admin write goes through app.core.audit.record_entry, which
    appends to OPENBOUNCER_AUDIT_LOG_PATH (the bundled config/audit_log.jsonl
    by default) -- redirect it to a per-test scratch path so the suite never
    writes into the real repo file. Autouse + function-scoped tmp_path so
    every test gets an isolated log with no cross-test leakage."""
    monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))


@pytest.fixture(autouse=True)
def _isolated_guardrail_events(tmp_path, monkeypatch):
    """Same reasoning as _isolated_audit_log above, for
    app.core.guardrail_events (the per-request guardrail decision log) --
    redirects it to a per-test scratch path so the suite never writes into
    the real repo file."""
    monkeypatch.setenv("OPENBOUNCER_GUARDRAIL_EVENTS_PATH", str(tmp_path / "guardrail_events.jsonl"))


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_API_KEY}"},
    ) as ac:
        yield ac


@pytest.fixture
async def unauthenticated_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def restricted_client():
    store = parse_key_store(RESTRICTED_STORE_YAML)
    app.dependency_overrides[get_key_store] = lambda: store
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {RESTRICTED_KEY}"},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_key_store, None)


@pytest.fixture
async def low_limit_client():
    store = parse_key_store(LOW_LIMIT_STORE_YAML)
    limiter = RateLimiter()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {LOW_LIMIT_KEY}"},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_key_store, None)
        app.dependency_overrides.pop(get_rate_limiter, None)


@pytest.fixture
async def admin_client():
    store = parse_key_store(ADMIN_STORE_YAML)
    app.dependency_overrides[get_key_store] = lambda: store
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {ADMIN_API_KEY}"},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_key_store, None)


@pytest.fixture
async def observer_client():
    """A non-admin key scoped to metrics:read + activity:read only -- e.g.
    an SRE who should see dashboards but must not be able to mint/delete
    keys or rewrite guardrails/prompt-injection policy. Proves the
    admin_scopes mechanism actually separates those capabilities instead of
    the old all-or-nothing is_admin gate."""
    store = parse_key_store(OBSERVER_STORE_YAML)
    app.dependency_overrides[get_key_store] = lambda: store
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {OBSERVER_KEY}"},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_key_store, None)


@pytest.fixture
async def guardrails_key_client():
    store = parse_key_store(GUARDRAILS_KEY_STORE_YAML)
    app.dependency_overrides[get_key_store] = lambda: store
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {GUARDRAILS_KEY}"},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_key_store, None)
