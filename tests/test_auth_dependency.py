import hashlib

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.dependency import AuthContext, ensure_model_allowed, require_api_key
from app.auth.keys import parse_key_store
from app.auth.rate_limiter import RateLimiter
from app.core.errors import OpenAIError

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
