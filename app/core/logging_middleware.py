import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL
from app.core.request_context import request_id_var

logger = logging.getLogger("openbouncer.access")


def _route_template(scope: Scope) -> str:
    # scope["route"] is set in place by Starlette's router once it matches
    # a route, so it's only available *after* `await self.app(...)` below
    # returns -- reading it via the raw request path instead would make
    # path-parameterized routes (e.g. /api/admin/keys/{key_id}/...) create
    # one Prometheus time series per distinct key_id/config_id ever
    # requested (and, worse, one per garbage path a scanner throws at the
    # server, since those never match a route at all) -- unbounded label
    # cardinality, a classic way to blow up a Prometheus instance.
    route = scope.get("route")
    path_format = getattr(route, "path_format", None)
    if path_format:
        return path_format
    return "unmatched"


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
            duration_seconds = time.monotonic() - start
            duration_ms = round(duration_seconds * 1000, 2)
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
            route_template = _route_template(scope)
            HTTP_REQUESTS_TOTAL.labels(
                method=method, path=route_template, status=str(status_code)
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=route_template).observe(
                duration_seconds
            )
            request_id_var.reset(token)
