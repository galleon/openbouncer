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
