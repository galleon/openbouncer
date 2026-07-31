from pydantic import BaseModel, ConfigDict


class AdminKeyItem(BaseModel):
    id: str
    is_admin: bool
    allowed_models: list[str]
    # None means unrestricted (the key can set guardrails.config_id to
    # anything) -- see APIKeyRecord.allowed_guardrails_configs.
    allowed_guardrails_configs: list[str] | None
    requests_per_minute: int
    # key_hash intentionally omitted -- no reason to expose hash material
    # over the API even though it isn't reversible to the raw key.


class AdminKeyListResponse(BaseModel):
    keys: list[AdminKeyItem]


class UpdateKeyGuardrailsConfigsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_guardrails_configs: list[str]


class AdminEditableSection(BaseModel):
    field: str
    label: str
    items: list[str]


class AdminGuardrailsConfigItem(BaseModel):
    config_id: str
    editable: bool
    sections: list[AdminEditableSection]
    error: str | None = None


class AdminGuardrailsConfigListResponse(BaseModel):
    configs: list[AdminGuardrailsConfigItem]


class UpdateGuardrailsConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: dict[str, list[str]]
