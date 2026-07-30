from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import OpenAIError, openai_error_handler, openai_validation_exception_handler
from app.core.logging_config import configure_logging
from app.core.logging_middleware import RequestLoggingMiddleware

UI_STATIC_DIR = Path(__file__).resolve().parent / "ui" / "static"


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.include_router(api_router)
    app.add_exception_handler(RequestValidationError, openai_validation_exception_handler)
    app.add_exception_handler(OpenAIError, openai_error_handler)
    app.add_middleware(RequestLoggingMiddleware)
    app.mount("/ui", StaticFiles(directory=str(UI_STATIC_DIR), html=True), name="ui")
    return app


app = create_app()
