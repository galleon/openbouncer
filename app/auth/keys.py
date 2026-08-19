import asyncio
import hashlib
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.atomic_write import atomic_write_text
from app.core.audit import record_entry as record_audit_entry

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "api_keys.yaml"

CONFIG_PATH_ENV_VAR = "OPENBOUNCER_API_KEYS_CONFIG"
CONFIG_YAML_ENV_VAR = "OPENBOUNCER_API_KEYS_YAML"

DEFAULT_REQUESTS_PER_MINUTE = 60

# Fine-grained admin capabilities a key can be granted without making it a
# full `is_admin: true` super-admin -- e.g. a Prometheus scrape key that
# only needs "metrics:read", not the ability to rewrite guardrails policy
# or mint/delete other keys. See APIKeyRecord.admin_scopes and
# APIKeyRecord.effective_admin_scopes().
AdminScope = Literal[
    "keys:write",
    "guardrails:write",
    "prompt_injection:write",
    "output_leak:write",
    "metrics:read",
    "activity:read",
]
ALL_ADMIN_SCOPES: frozenset[str] = frozenset(
    (
        "keys:write",
        "guardrails:write",
        "prompt_injection:write",
        "output_leak:write",
        "metrics:read",
        "activity:read",
    )
)

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def hash_api_key(raw_key: str) -> str:
    """Hash a raw API key for storage/lookup. API keys are high-entropy random
    tokens (unlike passwords), so a fast, unsalted SHA-256 digest is adequate
    here -- brute-forcing a single hash back to the original key isn't
    feasible given the key's entropy, unlike with low-entropy passwords.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class APIKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    key_hash: str
    # Not cross-checked against config/models.yaml at load time -- the
    # model registry is operator-defined with no fixed set to validate
    # against. Requesting a model that's misspelled here or doesn't exist in
    # the registry is still caught at request time (model_not_found /
    # model_not_allowed), just not at config-load time.
    allowed_models: list[str] = Field(min_length=1)
    requests_per_minute: int = Field(default=DEFAULT_REQUESTS_PER_MINUTE, gt=0)
    is_admin: bool = False
    # None (the default -- and what every key had before this field existed)
    # means unrestricted: any config_id may be requested, exactly like
    # before this field was introduced, so adding it is a no-op for
    # existing configs. A key only becomes restricted once an admin
    # explicitly sets this to a (possibly empty) list via the admin API --
    # at that point it's an opt-in allowlist, same idea as allowed_models.
    allowed_guardrails_configs: list[str] | None = None
    # None (the default) means unlimited -- same "opt-in cap" posture as
    # allowed_guardrails_configs above. Enforced by app.auth.budget against
    # cumulative token usage in the current UTC calendar day/month, checked
    # on every request in require_api_key (see that module for why it's a
    # separate check()/record() split rather than one atomic call like rate
    # limiting's). Not billing-grade metering, same caveat as
    # app.auth.usage.UsageTracker.
    token_budget_daily: int | None = Field(default=None, gt=0)
    token_budget_monthly: int | None = Field(default=None, gt=0)
    # Fine-grained admin capabilities for a key that isn't a full
    # `is_admin: true` super-admin -- see ALL_ADMIN_SCOPES. Ignored (every
    # scope is implied) when is_admin is true; only matters for a
    # deliberately narrower key, e.g. a Prometheus scrape key with just
    # "metrics:read". Empty by default, same opt-in posture as
    # allowed_guardrails_configs.
    admin_scopes: list[AdminScope] = Field(default_factory=list)

    def effective_admin_scopes(self) -> frozenset[str]:
        """The actual set of admin scopes this key can use: every scope if
        is_admin, else exactly its own admin_scopes. Centralized here so
        "is_admin implies everything" is expressed in exactly one place."""
        return ALL_ADMIN_SCOPES if self.is_admin else frozenset(self.admin_scopes)

    @field_validator("key_hash")
    @classmethod
    def _key_hash_must_be_sha256_hex(cls, value: str) -> str:
        if not _SHA256_HEX_PATTERN.match(value):
            raise ValueError("key_hash must be a 64-character hex-encoded SHA-256 digest")
        return value.lower()

    @field_validator("allowed_guardrails_configs")
    @classmethod
    def _guardrails_config_ids_must_be_well_formed(
        cls, value: list[str] | None
    ) -> list[str] | None:
        if value is None:
            return None
        from app.guardrails.service import CONFIG_ID_PATTERN

        for config_id in value:
            if not CONFIG_ID_PATTERN.match(config_id):
                raise ValueError(
                    f"Invalid guardrails config_id in allowed_guardrails_configs: {config_id!r}"
                )
        return value


class APIKeyStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[APIKeyRecord] = Field(default_factory=list)

    @field_validator("keys")
    @classmethod
    def _ids_and_hashes_must_be_unique(cls, value: list[APIKeyRecord]) -> list[APIKeyRecord]:
        ids = [k.id for k in value]
        duplicate_ids = sorted({i for i in ids if ids.count(i) > 1})
        if duplicate_ids:
            raise ValueError(f"Duplicate key ids in API key config: {duplicate_ids}")

        hashes = [k.key_hash for k in value]
        duplicate_hashes = sorted({h for h in hashes if hashes.count(h) > 1})
        if duplicate_hashes:
            raise ValueError("Duplicate key_hash values in API key config")

        return value


class KeyStore:
    def __init__(self, keys: list[APIKeyRecord]) -> None:
        self._by_hash = {k.key_hash: k for k in keys}
        self._by_id = {k.id: k for k in keys}

    def get_by_hash(self, key_hash: str) -> APIKeyRecord | None:
        return self._by_hash.get(key_hash)

    def get_by_id(self, key_id: str) -> APIKeyRecord | None:
        return self._by_id.get(key_id)

    def all(self) -> list[APIKeyRecord]:
        return list(self._by_id.values())

    def __contains__(self, key_hash: str) -> bool:
        return key_hash in self._by_hash


def parse_key_store(raw_yaml: str) -> KeyStore:
    data = yaml.safe_load(raw_yaml) or {}
    config = APIKeyStoreConfig(**data)
    return KeyStore(config.keys)


def _resolve_raw_yaml() -> str | None:
    inline = os.environ.get(CONFIG_YAML_ENV_VAR)
    if inline:
        return inline

    explicit_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    path = Path(explicit_path) if explicit_path else DEFAULT_CONFIG_PATH
    if path.exists():
        return path.read_text()

    if explicit_path:
        # A path was explicitly configured but doesn't exist -- that's a
        # real misconfiguration, unlike the default path simply not existing.
        raise FileNotFoundError(f"API key config file not found: {path}")

    # No API key config anywhere: default to an empty store (deny all)
    # rather than shipping a bundled default file with real key material.
    return None


def load_key_store() -> KeyStore:
    raw = _resolve_raw_yaml()
    if raw is None:
        return KeyStore([])
    return parse_key_store(raw)


@lru_cache
def get_key_store() -> KeyStore:
    return load_key_store()


class AdminPersistenceError(RuntimeError):
    """Raised when the key store isn't file-backed, so an admin write has
    nowhere durable to go (OPENBOUNCER_API_KEYS_YAML always wins over the
    file, per _resolve_raw_yaml -- writing the file would be silently
    ignored on next load)."""


class KeyAlreadyExistsError(RuntimeError):
    """Raised by create_key() when the requested id is already taken."""


class LastKeysWriteAdminError(RuntimeError):
    """Raised by update_key_fields()/delete_key() when the change would
    leave no key with the `keys:write` admin scope (via `is_admin: true`
    or an explicit `admin_scopes` grant) -- refuses to lock the deployment
    out of its own key-management API. There's no equivalent guard for the
    other admin scopes (guardrails:write, prompt_injection:write, metrics:read,
    activity:read): losing those is inconvenient, but keys:write is the one
    scope that can always regrant every other scope to a key, so it's the
    one whose complete loss is unrecoverable without hand-editing
    config/api_keys.yaml on the host.
    """


def _has_keys_write(record: APIKeyRecord) -> bool:
    return "keys:write" in record.effective_admin_scopes()


def _would_strand_key_management(
    existing_record: APIKeyRecord,
    config: APIKeyStoreConfig,
    key_id: str,
    updated_record: APIKeyRecord,
) -> bool:
    if not _has_keys_write(existing_record):
        return False  # this key never had it -- nothing to lose
    if _has_keys_write(updated_record):
        return False  # still has it after the change
    return not any(k.id != key_id and _has_keys_write(k) for k in config.keys)


# Key ids appear in URL paths (/api/admin/keys/{key_id}/...) and log lines
# -- restricted to a safe charset for new keys, same idea as
# app.guardrails.service.CONFIG_ID_PATTERN. Checked at the route layer (see
# app/api/routes/admin.py), not as an APIKeyRecord field validator, so it
# can't retroactively reject an existing config/api_keys.yaml with a
# legacy id that predates this pattern.
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Serializes read-modify-write across concurrent admin requests -- without
# this, two overlapping PATCHes could both read the pre-change file and the
# second write would clobber the first's update. Process-local: this
# doesn't coordinate two gateway replicas writing the same api_keys.yaml at
# once -- see the README's "Multi-replica deployments" section.
_write_lock = asyncio.Lock()


def _writable_path() -> Path:
    if os.environ.get(CONFIG_YAML_ENV_VAR):
        raise AdminPersistenceError(
            f"Cannot persist API key changes: {CONFIG_YAML_ENV_VAR} is set, so the key "
            "store is loaded from an inline environment variable and a file write here "
            "would be silently ignored on next load (the inline env var always wins -- "
            f"see _resolve_raw_yaml()). Unset it or switch this deployment to "
            f"{CONFIG_PATH_ENV_VAR} pointing at a real file."
        )
    explicit_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    return Path(explicit_path) if explicit_path else DEFAULT_CONFIG_PATH


def _load_writable_config() -> APIKeyStoreConfig:
    path = _writable_path()
    raw = path.read_text() if path.exists() else "keys: []\n"
    return APIKeyStoreConfig(**(yaml.safe_load(raw) or {}))


def _persist_config(
    config: APIKeyStoreConfig, *, actor_key_id: str, action: str, summary: str
) -> None:
    path = _writable_path()
    before = path.read_text() if path.exists() else "keys: []\n"
    # exclude_none so keys that were never restricted (the common case)
    # keep round-tripping without gaining an explicit
    # `allowed_guardrails_configs: null` line just because some *other*
    # key in the file got edited.
    new_yaml = yaml.safe_dump(config.model_dump(mode="json", exclude_none=True), sort_keys=False)
    atomic_write_text(path, new_yaml)
    # Recorded only after the write above succeeds -- see app.core.audit's
    # module docstring for why.
    record_audit_entry(
        actor_key_id=actor_key_id,
        resource_type="api_keys",
        resource_id=None,
        action=action,
        summary=summary,
        path=path,
        before=before,
        after=new_yaml,
    )
    get_key_store.cache_clear()


def generate_api_key() -> str:
    """Matches the README's manual key-generation one-liner."""
    return "sk-" + secrets.token_urlsafe(32)


async def create_key(record: APIKeyRecord, *, actor_key_id: str) -> APIKeyRecord:
    """Persists a brand-new key and invalidates get_key_store()'s cache so
    it's live on the very next request -- no restart needed. Raises
    KeyAlreadyExistsError if record.id is already taken, AdminPersistenceError
    if the store isn't file-backed.
    """
    async with _write_lock:
        config = _load_writable_config()
        if any(k.id == record.id for k in config.keys):
            raise KeyAlreadyExistsError(record.id)
        new_config = APIKeyStoreConfig(keys=[*config.keys, record])
        _persist_config(
            new_config,
            actor_key_id=actor_key_id,
            action="create_key",
            summary=f"Created key '{record.id}' (is_admin={record.is_admin})",
        )
        return record


async def update_key_fields(
    key_id: str,
    *,
    actor_key_id: str,
    action: str = "update_key",
    summary: str | None = None,
    **fields: object,
) -> APIKeyRecord:
    """Persists a partial update to an existing key and invalidates
    get_key_store()'s cache. Raises KeyError if key_id doesn't exist,
    AdminPersistenceError if the store isn't file-backed,
    LastKeysWriteAdminError if the update would remove `keys:write` admin
    access from the last key that has it. `action`/`summary` let callers
    that are semantically more specific than a generic field update
    (rotate_key, update_key_allowed_guardrails_configs) record that in the
    audit log instead of a generic "updated fields: [...]".
    """
    async with _write_lock:
        config = _load_writable_config()
        existing = next((k for k in config.keys if k.id == key_id), None)
        if existing is None:
            raise KeyError(key_id)

        updated_record = existing.model_copy(update=fields)
        if _would_strand_key_management(existing, config, key_id, updated_record):
            raise LastKeysWriteAdminError(key_id)

        new_keys = [updated_record if k.id == key_id else k for k in config.keys]
        # Re-validate the whole store (not just the one record) before
        # writing, so a bug here can't persist an inconsistent file.
        new_config = APIKeyStoreConfig(keys=new_keys)
        _persist_config(
            new_config,
            actor_key_id=actor_key_id,
            action=action,
            summary=summary or f"Updated key '{key_id}': fields={sorted(fields.keys())}",
        )
        return next(k for k in new_keys if k.id == key_id)


async def delete_key(key_id: str, *, actor_key_id: str) -> None:
    """Removes a key and invalidates get_key_store()'s cache. Raises
    KeyError if key_id doesn't exist, AdminPersistenceError if the store
    isn't file-backed, LastKeysWriteAdminError if it's the last key with
    `keys:write` admin access.
    """
    async with _write_lock:
        config = _load_writable_config()
        existing = next((k for k in config.keys if k.id == key_id), None)
        if existing is None:
            raise KeyError(key_id)
        if _has_keys_write(existing) and not any(
            k.id != key_id and _has_keys_write(k) for k in config.keys
        ):
            raise LastKeysWriteAdminError(key_id)
        new_keys = [k for k in config.keys if k.id != key_id]
        _persist_config(
            APIKeyStoreConfig(keys=new_keys),
            actor_key_id=actor_key_id,
            action="delete_key",
            summary=f"Deleted key '{key_id}'",
        )


async def rotate_key(key_id: str, *, actor_key_id: str) -> tuple[APIKeyRecord, str]:
    """Issues a new raw key for an existing key id, replacing its stored
    hash. Returns (record, raw_key) -- the raw key is never persisted or
    retrievable again after this. Raises KeyError if key_id doesn't exist.
    """
    raw_key = generate_api_key()
    record = await update_key_fields(
        key_id,
        actor_key_id=actor_key_id,
        action="rotate_key",
        summary=f"Rotated key '{key_id}'",
        key_hash=hash_api_key(raw_key),
    )
    return record, raw_key


async def update_key_allowed_guardrails_configs(
    key_id: str, allowed_guardrails_configs: list[str], *, actor_key_id: str
) -> APIKeyRecord:
    """Persists a key's allowed_guardrails_configs to disk and invalidates
    get_key_store()'s cache so the change is live on the very next request
    -- no restart needed. Raises KeyError if key_id doesn't exist,
    AdminPersistenceError if the store isn't file-backed.
    """
    return await update_key_fields(
        key_id,
        actor_key_id=actor_key_id,
        action="update_key_guardrails_configs",
        summary=f"Set key '{key_id}' allowed_guardrails_configs={allowed_guardrails_configs}",
        allowed_guardrails_configs=list(allowed_guardrails_configs),
    )
