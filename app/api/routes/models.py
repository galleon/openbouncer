import time

from fastapi import APIRouter, Depends

from app.auth.dependency import AuthContext, require_api_key
from app.core.registry import ModelRegistry, get_model_registry
from app.schemas.models import ModelInfo, ModelListResponse

router = APIRouter()


@router.get("/v1/models", response_model=ModelListResponse)
async def list_models(
    registry: ModelRegistry = Depends(get_model_registry),
    auth: AuthContext = Depends(require_api_key),
) -> ModelListResponse:
    created = int(time.time())
    return ModelListResponse(
        data=[
            ModelInfo(id=entry.id, created=created, owned_by=entry.id.split("/")[0])
            for entry in registry.all()
            if entry.id in auth.allowed_models
        ]
    )
