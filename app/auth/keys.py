import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @field_validator("key_hash")
    @classmethod
    def _key_hash_must_be_sha256_hex(cls, value: str) -> str:
        if not _SHA256_HEX_PATTERN.match(value):
            raise ValueError("key_hash must be a 64-character hex-encoded SHA-256 digest")
        return value.lower()


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
