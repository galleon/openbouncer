import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.request_context import request_id_var

logger = logging.getLogger("openbouncer.access")


class RequestLoggingMiddleware:
    """Assigns each HTTP request a request_id, exposes it via the
    request_id contextvar for the rest of the request lifecycle (auth,
    routes, error handlers), echoes it back as an `X-Request-Id` response
    header, and logs a start/end access log line.

    Implemented as a plain ASGI middleware (not Starlette's
    BaseHTTPMiddleware) so it works transparently with StreamingResponse --
    BaseHTTPMiddleware buffers/re-wraps the response through an internal
    task+queue, which risks breaking the client-disconnect-cancels-upstream
    behavior the streaming endpoints rely on.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        method = scope.get("method")
        path = scope.get("path")
        start = time.monotonic()
        status_code: int | None = None

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append("x-request-id", request_id)
            await send(message)

        logger.info(
            "request start",
            extra={"request_id": request_id, "method": method, "path": path},
        )
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            logger.info(
                "request end",
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)
