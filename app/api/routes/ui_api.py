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
    WhoAmIResponse,
)

router = APIRouter()


@router.get("/api/ui/whoami", response_model=WhoAmIResponse)
async def whoami(auth: AuthContext = Depends(require_api_key)) -> WhoAmIResponse:
    # Deliberately gated by require_api_key, not require_admin -- any valid
    # key can call this, so the admin UI can distinguish "not admin" from
    # "auth failed" and render a clean denied-state rather than a page full
    # of broken 403s from every /api/admin/* call.
    return WhoAmIResponse(
        key_id=auth.key_id, is_admin=auth.is_admin, admin_scopes=sorted(auth.admin_scopes)
    )


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
