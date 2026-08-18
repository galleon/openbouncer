from pydantic import BaseModel


class UIModelItem(BaseModel):
    id: str
    capabilities: list[str]


class UIModelsResponse(BaseModel):
    data: list[UIModelItem]


class GuardrailsConfigItem(BaseModel):
    config_id: str


class GuardrailsPresetItem(BaseModel):
    id: str
    label: str
    example_message: str | None = None


class GuardrailsCatalogResponse(BaseModel):
    configs: list[GuardrailsConfigItem]
    presets: list[GuardrailsPresetItem]


class WhoAmIResponse(BaseModel):
    key_id: str
    is_admin: bool
    # Effective scopes (see app.auth.keys.ALL_ADMIN_SCOPES) -- every scope
    # when is_admin is true, otherwise exactly this key's own grants. Lets
    # the admin/activity UI show only the sections a scoped (non-full-admin)
    # key can actually use instead of an all-or-nothing admin gate.
    admin_scopes: list[str]
