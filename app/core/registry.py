import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import OpenAIError

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "models.yaml"

CONFIG_PATH_ENV_VAR = "OPENBOUNCER_MODELS_CONFIG"
CONFIG_YAML_ENV_VAR = "OPENBOUNCER_MODELS_YAML"
# Alternate, deployment-facing name for CONFIG_PATH_ENV_VAR (used in the
# Docker/compose config surface). Both are honored; CONFIG_PATH_ENV_VAR wins
# if both happen to be set.
MODEL_CONFIG_PATH_ENV_VAR = "MODEL_CONFIG_PATH"

Capability = Literal["chat", "vision", "embeddings"]


class ModelEntry(BaseModel):
    """A single model the gateway exposes, and how to reach its upstream.

    There is no fixed allowlist of ids -- the registry is entirely
    operator-defined via config/models.yaml (or its env-var overrides).
    `base_url` can point at any OpenAI-compatible endpoint: a cloud API, a
    local Ollama server, OpenRouter, a self-hosted vLLM/NIM container, etc.
    See config/models.yaml for worked examples of each.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    upstream_model: str
    base_url: str
    api_key_env: str
    capabilities: list[Capability] = Field(min_length=1)
    concurrency_limit: int = Field(gt=0)


class ModelRegistryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelEntry] = Field(min_length=1)

    @field_validator("models")
    @classmethod
    def _ids_must_be_unique(cls, value: list[ModelEntry]) -> list[ModelEntry]:
        ids = [entry.id for entry in value]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"Duplicate model ids in registry config: {duplicates}")
        return value


def resolve_api_key(entry: ModelEntry) -> str:
    """Look up an entry's upstream API key from its configured env var.

    Shared by every route that calls an upstream model (chat, embeddings)
    so they all fail the same way on a misconfigured deployment.
    """
    api_key = os.environ.get(entry.api_key_env)
    if not api_key:
        raise OpenAIError(
            f"No API key configured for model `{entry.id}` "
            f"(expected environment variable `{entry.api_key_env}`).",
            status_code=500,
            error_type="api_error",
            code="missing_api_key",
        )
    return api_key


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry]):
        self._by_id = {entry.id: entry for entry in entries}

    def all(self) -> list[ModelEntry]:
        return list(self._by_id.values())

    def get(self, model_id: str) -> ModelEntry | None:
        return self._by_id.get(model_id)

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._by_id


def parse_model_registry(raw_yaml: str) -> ModelRegistry:
    data = yaml.safe_load(raw_yaml) or {}
    config = ModelRegistryConfig(**data)
    return ModelRegistry(config.models)


def _resolve_raw_yaml() -> str:
    inline = os.environ.get(CONFIG_YAML_ENV_VAR)
    if inline:
        return inline

    configured_path = os.environ.get(CONFIG_PATH_ENV_VAR) or os.environ.get(
        MODEL_CONFIG_PATH_ENV_VAR
    )
    path = Path(configured_path) if configured_path else DEFAULT_CONFIG_PATH
    return path.read_text()


def load_model_registry() -> ModelRegistry:
    return parse_model_registry(_resolve_raw_yaml())


@lru_cache
def get_model_registry() -> ModelRegistry:
    return load_model_registry()
