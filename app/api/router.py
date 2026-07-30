from fastapi import APIRouter

from app.api.routes import chat, embeddings, health, models, ui_api

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(models.router, tags=["models"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(embeddings.router, tags=["embeddings"])
api_router.include_router(ui_api.router, tags=["ui"])
