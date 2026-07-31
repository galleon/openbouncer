import logging
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.keys import KeyStore, get_key_store, hash_api_key
from app.auth.rate_limiter import SupportsRateLimiting, get_rate_limiter
from app.core.errors import OpenAIError
from app.core.request_context import get_request_id

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    key_id: str
    allowed_models: list[str]
    is_admin: bool = False
    # None means unrestricted -- see APIKeyRecord.allowed_guardrails_configs
    # in app.auth.keys for why this is nullable rather than defaulting to
    # an empty (deny-everything) list.
    allowed_guardrails_configs: list[str] | None = None


def _redact(raw_key: str) -> str:
    if len(raw_key) <= 8:
        return "***"
    return f"{raw_key[:3]}...{raw_key[-4:]}"


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    key_store: KeyStore = Depends(get_key_store),
    rate_limiter: SupportsRateLimiting = Depends(get_rate_limiter),
) -> AuthContext:
    if credentials is None or not credentials.credentials:
        logger.warning(
            "Auth failed: missing API key [request_id=%s]", get_request_id()
        )
        raise OpenAIError(
            "You didn't provide an API key. Provide one via the Authorization "
            "header using Bearer auth (i.e. `Authorization: Bearer YOUR_KEY`).",
            status_code=401,
            error_type="authentication_error",
            code="missing_api_key",
        )

    raw_key = credentials.credentials
    record = key_store.get_by_hash(hash_api_key(raw_key))
    if record is None:
        logger.warning(
            "Auth failed: invalid API key %s [request_id=%s]",
            _redact(raw_key),
            get_request_id(),
        )
        raise OpenAIError(
            f"Incorrect API key provided: {_redact(raw_key)}.",
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
        )

    allowed = await rate_limiter.check(record.id, record.requests_per_minute)
    if not allowed:
        logger.warning(
            "Auth failed: rate limit exceeded for key_id=%s [request_id=%s]",
            record.id,
            get_request_id(),
        )
        raise OpenAIError(
            "Rate limit reached for this API key. Please retry after some time.",
            status_code=429,
            error_type="rate_limit_error",
            code="rate_limit_exceeded",
        )

    return AuthContext(
        key_id=record.id,
        allowed_models=list(record.allowed_models),
        is_admin=record.is_admin,
        allowed_guardrails_configs=(
            list(record.allowed_guardrails_configs)
            if record.allowed_guardrails_configs is not None
            else None
        ),
    )


async def require_admin(auth: AuthContext = Depends(require_api_key)) -> AuthContext:
    if not auth.is_admin:
        logger.warning(
            "Forbidden: key_id=%s lacks admin access [request_id=%s]",
            auth.key_id,
            get_request_id(),
        )
        raise OpenAIError(
            "Your API key does not have admin access.",
            status_code=403,
            error_type="permission_error",
            code="admin_required",
        )
    return auth


def ensure_model_allowed(auth: AuthContext, model: str) -> None:
    if model not in auth.allowed_models:
        logger.warning(
            "Forbidden model: key_id=%s requested model=%s [request_id=%s]",
            auth.key_id,
            model,
            get_request_id(),
        )
        raise OpenAIError(
            f"Your API key does not have access to the model `{model}`.",
            status_code=403,
            error_type="permission_error",
            param="model",
            code="model_not_allowed",
        )


def ensure_guardrails_config_allowed(auth: AuthContext, config_id: str) -> None:
    if auth.allowed_guardrails_configs is None:
        # Unrestricted -- see APIKeyRecord.allowed_guardrails_configs.
        return
    if config_id not in auth.allowed_guardrails_configs:
        logger.warning(
            "Forbidden guardrails config: key_id=%s requested config_id=%s [request_id=%s]",
            auth.key_id,
            config_id,
            get_request_id(),
        )
        raise OpenAIError(
            f"Your API key does not have access to the guardrails config `{config_id}`.",
            status_code=403,
            error_type="permission_error",
            param="guardrails.config_id",
            code="guardrails_config_not_allowed",
        )
