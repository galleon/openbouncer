from pydantic import BaseModel, ConfigDict, Field


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


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    allowed_models: list[str] = Field(min_length=1)
    # None -> falls back to keys.DEFAULT_REQUESTS_PER_MINUTE, same as
    # hand-editing config/api_keys.yaml without the field.
    requests_per_minute: int | None = Field(default=None, gt=0)
    is_admin: bool = False
    allowed_guardrails_configs: list[str] | None = None


class CreateKeyResponse(BaseModel):
    key: AdminKeyItem
    # The raw key, returned exactly once -- only a hash is ever persisted,
    # so this is the only chance the caller has to see it.
    api_key: str


class UpdateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # All fields default to "unset" (None) so the route can build a
    # partial-update dict via model_dump(exclude_unset=True) -- an omitted
    # field is left alone, not nulled out.
    allowed_models: list[str] | None = Field(default=None, min_length=1)
    requests_per_minute: int | None = Field(default=None, gt=0)
    is_admin: bool | None = None


class RotateKeyResponse(BaseModel):
    key: AdminKeyItem
    api_key: str


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
