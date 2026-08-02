from fastapi import APIRouter

from app.api.routes import activity, admin, chat, embeddings, health, metrics, models, ui_api

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(models.router, tags=["models"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(embeddings.router, tags=["embeddings"])
api_router.include_router(ui_api.router, tags=["ui"])
api_router.include_router(admin.router, tags=["admin"])
api_router.include_router(metrics.router, tags=["metrics"])
api_router.include_router(activity.router, tags=["activity"])
