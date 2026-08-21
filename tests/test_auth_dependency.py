import hashlib

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.dependency import (
    AuthContext,
    ensure_guardrails_config_allowed,
    ensure_model_allowed,
    ensure_sovereignty_allowed,
    require_api_key,
    require_scope,
)
from app.auth.budget import BudgetTracker
from app.auth.keys import ALL_ADMIN_SCOPES, APIKeyRecord, parse_key_store
from app.auth.rate_limiter import RateLimiter
from app.core.errors import OpenAIError
from app.core.registry import ModelEntry

RAW_KEY = "sk-unit-test-0123456789"
KEY_HASH = hashlib.sha256(RAW_KEY.encode()).hexdigest()

STORE_YAML = f"""
keys:
  - id: unit-test-key
    key_hash: {KEY_HASH}
    allowed_models: [nvidia/qwen3.6-nvfp4, nvidia/gemme4-nvfp4]
    requests_per_minute: 2
"""


def _credentials(raw_key: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw_key)


class TestRequireApiKey:
    @pytest.mark.asyncio
    async def test_missing_credentials_rejected(self):
        store = parse_key_store(STORE_YAML)
        limiter = RateLimiter()
        with pytest.raises(OpenAIError) as exc_info:
            await require_api_key(credentials=None, key_store=store, rate_limiter=limiter)
        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "missing_api_key"
        assert exc_info.value.error_type == "authentication_error"

    @pytest.mark.asyncio
    async def test_blank_credentials_rejected(self):
        store = parse_key_store(STORE_YAML)
        limiter = RateLimiter()
        with pytest.raises(OpenAIError) as exc_info:
            await require_api_key(
                credentials=_credentials(""), key_store=store, rate_limiter=limiter
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "missing_api_key"

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self):
        store = parse_key_store(STORE_YAML)
        limiter = RateLimiter()
        with pytest.raises(OpenAIError) as exc_info:
            await require_api_key(
                credentials=_credentials("sk-not-a-real-key"),
                key_store=store,
                rate_limiter=limiter,
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "invalid_api_key"

    @pytest.mark.asyncio
    async def test_valid_key_returns_auth_context(self):
        store = parse_key_store(STORE_YAML)
        limiter = RateLimiter()
        auth = await require_api_key(
            credentials=_credentials(RAW_KEY), key_store=store, rate_limiter=limiter
        )
        assert isinstance(auth, AuthContext)
        assert auth.key_id == "unit-test-key"
        assert auth.allowed_models == ["nvidia/qwen3.6-nvfp4", "nvidia/gemme4-nvfp4"]

    @pytest.mark.asyncio
    async def test_rate_limit_enforced_per_key_config(self):
        store = parse_key_store(STORE_YAML)  # requests_per_minute: 2
        limiter = RateLimiter()

        await require_api_key(
            credentials=_credentials(RAW_KEY), key_store=store, rate_limiter=limiter
        )
        await require_api_key(
            credentials=_credentials(RAW_KEY), key_store=store, rate_limiter=limiter
        )
        with pytest.raises(OpenAIError) as exc_info:
            await require_api_key(
                credentials=_credentials(RAW_KEY), key_store=store, rate_limiter=limiter
            )
        assert exc_info.value.status_code == 429
        assert exc_info.value.code == "rate_limit_exceeded"
        assert exc_info.value.error_type == "rate_limit_error"


BUDGET_STORE_YAML = f"""
keys:
  - id: budget-key
    key_hash: {KEY_HASH}
    allowed_models: [nvidia/qwen3.6-nvfp4]
    requests_per_minute: 1000000
    token_budget_daily: 1000
"""

RAW_KEY_NO_BUDGET = "sk-unit-test-no-budget-0123456789"
KEY_HASH_NO_BUDGET = hashlib.sha256(RAW_KEY_NO_BUDGET.encode()).hexdigest()
UNLIMITED_STORE_YAML = f"""
keys:
  - id: unlimited-key
    key_hash: {KEY_HASH_NO_BUDGET}
    allowed_models: [nvidia/qwen3.6-nvfp4]
    requests_per_minute: 1000000
"""


class TestTokenBudget:
    @pytest.mark.asyncio
    async def test_key_with_no_budget_configured_is_never_checked(self):
        # Confirms the budget_tracker isn't even consulted for a key with
        # no token_budget_daily/_monthly set -- passing a tracker that
        # already reports "over budget" for everything proves the `if`
        # guard in require_api_key short-circuits before calling it.
        store = parse_key_store(UNLIMITED_STORE_YAML)
        limiter = RateLimiter()

        class _AlwaysOverBudget:
            async def check(self, *args, **kwargs):
                return False

            async def record(self, *args, **kwargs):
                pass

        auth = await require_api_key(
            credentials=_credentials(RAW_KEY_NO_BUDGET),
            key_store=store,
            rate_limiter=limiter,
            budget_tracker=_AlwaysOverBudget(),
        )
        assert auth.key_id == "unlimited-key"

    @pytest.mark.asyncio
    async def test_within_budget_allows_the_request(self):
        store = parse_key_store(BUDGET_STORE_YAML)
        limiter = RateLimiter()
        budget_tracker = BudgetTracker()

        auth = await require_api_key(
            credentials=_credentials(RAW_KEY),
            key_store=store,
            rate_limiter=limiter,
            budget_tracker=budget_tracker,
        )
        assert auth.key_id == "budget-key"

    @pytest.mark.asyncio
    async def test_over_budget_rejected_with_429(self):
        store = parse_key_store(BUDGET_STORE_YAML)  # token_budget_daily: 1000
        limiter = RateLimiter()
        budget_tracker = BudgetTracker()
        await budget_tracker.record("budget-key", 1000)

        with pytest.raises(OpenAIError) as exc_info:
            await require_api_key(
                credentials=_credentials(RAW_KEY),
                key_store=store,
                rate_limiter=limiter,
                budget_tracker=budget_tracker,
            )
        assert exc_info.value.status_code == 429
        assert exc_info.value.code == "token_budget_exceeded"
        assert exc_info.value.error_type == "rate_limit_error"


class TestEnsureModelAllowed:
    def test_allowed_model_passes_silently(self):
        auth = AuthContext(key_id="k", allowed_models=["nvidia/qwen3.6-nvfp4"])
        ensure_model_allowed(auth, "nvidia/qwen3.6-nvfp4")

    def test_disallowed_model_rejected(self):
        auth = AuthContext(key_id="k", allowed_models=["nvidia/qwen3.6-nvfp4"])
        with pytest.raises(OpenAIError) as exc_info:
            ensure_model_allowed(auth, "nvidia/nemotron-vision")
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_type == "permission_error"
        assert exc_info.value.code == "model_not_allowed"
        assert exc_info.value.param == "model"


class TestAuthContextHasScope:
    def test_scope_present_is_true(self):
        auth = AuthContext(key_id="k", allowed_models=[], admin_scopes=frozenset({"metrics:read"}))
        assert auth.has_scope("metrics:read") is True

    def test_scope_absent_is_false(self):
        auth = AuthContext(key_id="k", allowed_models=[], admin_scopes=frozenset({"metrics:read"}))
        assert auth.has_scope("keys:write") is False

    def test_default_has_no_scopes(self):
        auth = AuthContext(key_id="k", allowed_models=[])
        assert auth.has_scope("metrics:read") is False


class TestRequireScope:
    @pytest.mark.asyncio
    async def test_key_with_scope_passes_through(self):
        auth = AuthContext(key_id="k", allowed_models=[], admin_scopes=frozenset({"metrics:read"}))
        dependency = require_scope("metrics:read")
        result = await dependency(auth=auth)
        assert result is auth

    @pytest.mark.asyncio
    async def test_key_without_scope_rejected(self):
        auth = AuthContext(key_id="k", allowed_models=[], admin_scopes=frozenset({"metrics:read"}))
        dependency = require_scope("keys:write")
        with pytest.raises(OpenAIError) as exc_info:
            await dependency(auth=auth)
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_type == "permission_error"
        assert exc_info.value.code == "admin_required"

    @pytest.mark.asyncio
    async def test_full_admin_satisfies_every_scope(self):
        # is_admin: true keys carry the fully-expanded scope set (see
        # APIKeyRecord.effective_admin_scopes()), not a special-cased
        # bypass -- require_scope has no separate "or is_admin" branch.
        auth = AuthContext(key_id="k", allowed_models=[], is_admin=True, admin_scopes=ALL_ADMIN_SCOPES)
        for scope in ALL_ADMIN_SCOPES:
            result = await require_scope(scope)(auth=auth)
            assert result is auth

    @pytest.mark.asyncio
    async def test_key_with_no_scopes_rejected(self):
        auth = AuthContext(key_id="k", allowed_models=[])
        with pytest.raises(OpenAIError) as exc_info:
            await require_scope("metrics:read")(auth=auth)
        assert exc_info.value.status_code == 403
        assert exc_info.value.code == "admin_required"


class TestEffectiveAdminScopes:
    def test_is_admin_implies_every_scope(self):
        record = APIKeyRecord(
            id="k", key_hash=KEY_HASH, allowed_models=["m"], is_admin=True
        )
        assert record.effective_admin_scopes() == ALL_ADMIN_SCOPES

    def test_non_admin_uses_explicit_admin_scopes_only(self):
        record = APIKeyRecord(
            id="k",
            key_hash=KEY_HASH,
            allowed_models=["m"],
            admin_scopes=["metrics:read"],
        )
        assert record.effective_admin_scopes() == frozenset({"metrics:read"})

    def test_non_admin_with_no_admin_scopes_has_none(self):
        record = APIKeyRecord(id="k", key_hash=KEY_HASH, allowed_models=["m"])
        assert record.effective_admin_scopes() == frozenset()

    @pytest.mark.asyncio
    async def test_require_api_key_populates_scoped_key_correctly(self):
        scoped_hash = hashlib.sha256(b"sk-scoped").hexdigest()
        store = parse_key_store(
            f"""
keys:
  - id: scoped-key
    key_hash: {scoped_hash}
    allowed_models: [m]
    admin_scopes: [metrics:read, activity:read]
"""
        )
        limiter = RateLimiter()
        auth = await require_api_key(
            credentials=_credentials("sk-scoped"), key_store=store, rate_limiter=limiter
        )
        assert auth.admin_scopes == frozenset({"metrics:read", "activity:read"})
        assert auth.is_admin is False


class TestEnsureGuardrailsConfigAllowed:
    def test_allowed_config_passes_silently(self):
        auth = AuthContext(key_id="k", allowed_models=[], allowed_guardrails_configs=["self_check_input"])
        ensure_guardrails_config_allowed(auth, "self_check_input")

    def test_disallowed_config_rejected(self):
        auth = AuthContext(key_id="k", allowed_models=[], allowed_guardrails_configs=["self_check_input"])
        with pytest.raises(OpenAIError) as exc_info:
            ensure_guardrails_config_allowed(auth, "topic_safety")
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_type == "permission_error"
        assert exc_info.value.code == "guardrails_config_not_allowed"
        assert exc_info.value.param == "guardrails.config_id"

    def test_empty_allowlist_rejects_everything(self):
        auth = AuthContext(key_id="k", allowed_models=[], allowed_guardrails_configs=[])
        with pytest.raises(OpenAIError):
            ensure_guardrails_config_allowed(auth, "self_check_input")

    def test_none_allowlist_is_unrestricted(self):
        # The default -- every key had this behavior before the field
        # existed, so it must stay a no-op unless a key is explicitly
        # restricted.
        auth = AuthContext(key_id="k", allowed_models=[], allowed_guardrails_configs=None)
        ensure_guardrails_config_allowed(auth, "self_check_input")
        ensure_guardrails_config_allowed(auth, "anything-at-all")


def _model_entry(**overrides) -> ModelEntry:
    fields = dict(
        id="local/gemma4-nvfp4",
        upstream_model="nvidia/Gemma-4-26B-A4B-NVFP4",
        base_url="http://vllm-gemma4:8000/v1",
        api_key_env="UPSTREAM_VLLM_API_KEY",
        capabilities=["chat"],
        concurrency_limit=4,
    )
    fields.update(overrides)
    return ModelEntry(**fields)


class TestEnsureSovereigntyAllowed:
    def test_no_constraint_is_unrestricted(self):
        # The default -- no key had this restriction before the field
        # existed, so it must stay a no-op unless a key explicitly sets it.
        auth = AuthContext(key_id="k", allowed_models=[], required_sovereignty=None)
        ensure_sovereignty_allowed(auth, _model_entry(sovereignty=None))
        ensure_sovereignty_allowed(auth, _model_entry(sovereignty={"data_residency": "US"}))

    def test_matching_tag_passes_silently(self):
        auth = AuthContext(key_id="k", allowed_models=[], required_sovereignty={"data_residency": "EU"})
        ensure_sovereignty_allowed(auth, _model_entry(sovereignty={"data_residency": "EU", "hosting": "on-prem"}))

    def test_mismatched_tag_rejected(self):
        auth = AuthContext(key_id="k", allowed_models=[], required_sovereignty={"data_residency": "EU"})
        with pytest.raises(OpenAIError) as exc_info:
            ensure_sovereignty_allowed(auth, _model_entry(sovereignty={"data_residency": "US"}))
        assert exc_info.value.status_code == 403
        assert exc_info.value.error_type == "permission_error"
        assert exc_info.value.code == "sovereignty_violation"
        assert exc_info.value.param == "model"

    def test_model_with_no_tags_declared_fails_a_constrained_key(self):
        # Absence isn't an implicit match -- see ensure_sovereignty_allowed's
        # docstring.
        auth = AuthContext(key_id="k", allowed_models=[], required_sovereignty={"data_residency": "EU"})
        with pytest.raises(OpenAIError) as exc_info:
            ensure_sovereignty_allowed(auth, _model_entry(sovereignty=None))
        assert exc_info.value.code == "sovereignty_violation"

    def test_all_required_tags_must_match(self):
        auth = AuthContext(
            key_id="k",
            allowed_models=[],
            required_sovereignty={"data_residency": "EU", "hosting": "on-prem"},
        )
        # Only one of the two required tags matches.
        with pytest.raises(OpenAIError):
            ensure_sovereignty_allowed(auth, _model_entry(sovereignty={"data_residency": "EU", "hosting": "cloud"}))

    def test_extra_tags_on_the_model_do_not_matter(self):
        auth = AuthContext(key_id="k", allowed_models=[], required_sovereignty={"data_residency": "EU"})
        ensure_sovereignty_allowed(
            auth, _model_entry(sovereignty={"data_residency": "EU", "legal_jurisdiction": "FR", "license": "apache-2.0"})
        )
