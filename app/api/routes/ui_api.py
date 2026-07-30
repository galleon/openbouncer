from fastapi import APIRouter, Depends

from app.auth.dependency import AuthContext, require_api_key
from app.core.registry import ModelRegistry, get_model_registry
from app.guardrails.catalog import GuardrailsCatalogService, get_guardrails_catalog_service
from app.schemas.ui import (
    GuardrailsCatalogResponse,
    GuardrailsConfigItem,
    GuardrailsPresetItem,
    UIModelItem,
    UIModelsResponse,
)

router = APIRouter()


@router.get("/api/ui/models", response_model=UIModelsResponse)
async def list_ui_models(
    registry: ModelRegistry = Depends(get_model_registry),
    auth: AuthContext = Depends(require_api_key),
) -> UIModelsResponse:
    return UIModelsResponse(
        data=[
            UIModelItem(id=entry.id, capabilities=list(entry.capabilities))
            for entry in registry.all()
            if entry.id in auth.allowed_models
        ]
    )


@router.get("/api/ui/guardrails/configs", response_model=GuardrailsCatalogResponse)
async def list_guardrails_configs(
    catalog: GuardrailsCatalogService = Depends(get_guardrails_catalog_service),
    auth: AuthContext = Depends(require_api_key),
) -> GuardrailsCatalogResponse:
    return GuardrailsCatalogResponse(
        configs=[
            GuardrailsConfigItem(config_id=summary.config_id)
            for summary in catalog.list_configs()
        ],
        presets=[
            GuardrailsPresetItem(
                id=preset.id, label=preset.label, example_message=preset.example_message
            )
            for preset in catalog.list_presets()
        ],
    )
