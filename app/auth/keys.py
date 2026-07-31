import asyncio
import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.atomic_write import atomic_write_text

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "api_keys.yaml"

CONFIG_PATH_ENV_VAR = "OPENBOUNCER_API_KEYS_CONFIG"
CONFIG_YAML_ENV_VAR = "OPENBOUNCER_API_KEYS_YAML"

DEFAULT_REQUESTS_PER_MINUTE = 60

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


# Serializes read-modify-write across concurrent admin requests -- without
# this, two overlapping PATCHes could both read the pre-change file and the
# second write would clobber the first's update.
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


async def update_key_allowed_guardrails_configs(
    key_id: str, allowed_guardrails_configs: list[str]
) -> APIKeyRecord:
    """Persists a key's allowed_guardrails_configs to disk and invalidates
    get_key_store()'s cache so the change is live on the very next request
    -- no restart needed. Raises KeyError if key_id doesn't exist,
    AdminPersistenceError if the store isn't file-backed.
    """
    async with _write_lock:
        path = _writable_path()
        raw = path.read_text() if path.exists() else "keys: []\n"
        config = APIKeyStoreConfig(**(yaml.safe_load(raw) or {}))
        if not any(k.id == key_id for k in config.keys):
            raise KeyError(key_id)

        new_keys = [
            k.model_copy(update={"allowed_guardrails_configs": list(allowed_guardrails_configs)})
            if k.id == key_id
            else k
            for k in config.keys
        ]
        # Re-validate the whole store (not just the one record) before
        # writing, so a bug here can't persist an inconsistent file.
        new_config = APIKeyStoreConfig(keys=new_keys)
        # exclude_none so keys that were never restricted (the common case)
        # keep round-tripping without gaining an explicit
        # `allowed_guardrails_configs: null` line just because some *other*
        # key in the file got edited.
        new_yaml = yaml.safe_dump(
            new_config.model_dump(mode="json", exclude_none=True), sort_keys=False
        )
        atomic_write_text(path, new_yaml)

        get_key_store.cache_clear()
        return next(k for k in new_keys if k.id == key_id)
