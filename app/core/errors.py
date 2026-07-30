import json

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class OpenAIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.param = param
        self.code = code


def normalize_upstream_error(
    status_code: int, raw_body: bytes, *, error_type: str = "api_error"
) -> OpenAIError:
    """Turn a non-2xx HTTP response body from any OpenAI-compatible upstream
    (model backend or guardrails service) into our own OpenAIError shape.
    """
    text = raw_body.decode("utf-8", errors="replace").strip()
    body = None
    if text:
        try:
            body = json.loads(text)
        except ValueError:
            body = None

    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        upstream_error = body["error"]
        return OpenAIError(
            upstream_error.get("message", "Upstream returned an error."),
            status_code=status_code,
            error_type=upstream_error.get("type") or error_type,
            param=upstream_error.get("param"),
            code=upstream_error.get("code"),
        )

    message = f"Upstream returned HTTP {status_code}."
    if text:
        message = f"{message} {text[:300]}"

    return OpenAIError(message, status_code=status_code, error_type=error_type, code=None)


def format_sse_error(error: OpenAIError) -> str:
    """Render an OpenAIError as an in-band SSE `data: ...` frame.

    Used once a stream has already committed to a 200 text/event-stream
    response, so an HTTP-level error is no longer possible -- the error has
    to be signaled inside the stream instead.
    """
    payload = {
        "error": {
            "message": error.message,
            "type": error.error_type,
            "param": error.param,
            "code": error.code,
        }
    }
    return f"data: {json.dumps(payload)}\n\n"


async def openai_error_handler(request: Request, exc: OpenAIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "param": exc.param,
                "code": exc.code,
            }
        },
    )


def _format_param(loc: tuple) -> str | None:
    parts = [str(p) for p in loc if p != "body"]
    return ".".join(parts) if parts else None


def _format_message(error: dict, param: str | None) -> str:
    if error.get("type") == "extra_forbidden" and param:
        return f"Unrecognized request argument supplied: {param}"
    return error.get("msg", "Invalid request.")


async def openai_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    first_error = exc.errors()[0]
    param = _format_param(first_error.get("loc", ()))
    message = _format_message(first_error, param)
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": None,
            }
        },
    )
