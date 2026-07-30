import asyncio
import json
import logging
from functools import lru_cache
from typing import AsyncIterator, TypeVar

import httpx
from pydantic import BaseModel

from app.core.errors import OpenAIError, format_sse_error, normalize_upstream_error
from app.schemas.chat import ChatCompletionRequest, ChatCompletionResponse
from app.schemas.embeddings import EmbeddingRequest, EmbeddingResponse

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.5

# Only network failures that happen before any response is received are safe
# to retry (nothing has been processed by the server yet). HTTP error
# responses (4xx/5xx) mean the server did respond, so those are normalized
# into errors instead of retried.
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
)


def _extract_sse_data(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    value = line[len("data:") :]
    if value.startswith(" "):
        value = value[1:]
    return value


class UpstreamClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._external_client = http_client
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds

    async def create_chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        upstream_model: str,
        request: ChatCompletionRequest,
    ) -> ChatCompletionResponse:
        if request.stream:
            raise OpenAIError(
                "This method does not support streaming; use stream_chat_completion instead.",
                status_code=400,
                param="stream",
            )

        payload = request.model_dump(mode="json", exclude_none=True, exclude={"guardrails"})
        payload["model"] = upstream_model
        payload["stream"] = False

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}

        response = await self._post_with_retries(url, payload, headers)
        return self._parse_upstream_json_response(response, ChatCompletionResponse)

    async def create_embeddings(
        self,
        *,
        base_url: str,
        api_key: str,
        upstream_model: str,
        request: EmbeddingRequest,
    ) -> EmbeddingResponse:
        payload = request.model_dump(mode="json", exclude_none=True)
        payload["model"] = upstream_model

        url = f"{base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {api_key}"}

        response = await self._post_with_retries(url, payload, headers)
        return self._parse_upstream_json_response(response, EmbeddingResponse)

    async def stream_chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        upstream_model: str,
        request: ChatCompletionRequest,
    ) -> AsyncIterator[str]:
        """Yields SSE `data: ...\\n\\n` frames, always ending with `data: [DONE]\\n\\n`.

        Failures that occur before any frame is produced (connect/timeout
        failures after retries, or a non-2xx upstream response) raise
        OpenAIError, since the caller can still turn that into a normal JSON
        error response at that point. Failures that occur after the first
        frame has already been yielded cannot change the HTTP status anymore
        (the client already committed to a 200 text/event-stream response),
        so they are instead emitted as an in-band SSE error frame followed by
        the terminal [DONE] frame.
        """
        payload = request.model_dump(mode="json", exclude_none=True, exclude={"guardrails"})
        payload["model"] = upstream_model
        payload["stream"] = True

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}

        client = self._external_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient()

        try:
            stream_cm, response = await self._open_stream_with_retries(
                client, url, payload, headers
            )
            async with stream_cm:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise normalize_upstream_error(response.status_code, body)

                try:
                    async for line in response.aiter_lines():
                        data = _extract_sse_data(line)
                        if data is None:
                            continue
                        if data == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            json.loads(data)
                        except ValueError:
                            logger.warning("Skipping malformed upstream SSE data frame")
                            continue
                        yield f"data: {data}\n\n"
                except Exception as exc:
                    logger.warning("Upstream stream interrupted after start: %s", exc)
                    yield format_sse_error(
                        OpenAIError(
                            f"Upstream connection was interrupted: {exc}",
                            status_code=502,
                            error_type="api_error",
                            code="upstream_disconnected",
                        )
                    )
                    yield "data: [DONE]\n\n"
                    return

                yield "data: [DONE]\n\n"
        finally:
            if owns_client:
                await client.aclose()

    async def _open_stream_with_retries(
        self, client: httpx.AsyncClient, url: str, payload: dict, headers: dict
    ):
        attempt = 0
        while True:
            stream_cm = client.stream(
                "POST", url, json=payload, headers=headers, timeout=self._timeout
            )
            try:
                response = await stream_cm.__aenter__()
            except RETRYABLE_EXCEPTIONS as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise OpenAIError(
                        f"Upstream request failed after {attempt} attempts: {exc}",
                        status_code=503,
                        error_type="api_error",
                        code="upstream_unavailable",
                    ) from exc
                logger.warning(
                    "Retrying upstream stream connection (attempt %d/%d) after %s",
                    attempt,
                    self._max_retries,
                    type(exc).__name__,
                )
                await asyncio.sleep(self._backoff_seconds * (2 ** (attempt - 1)))
                continue
            return _EnteredStreamContext(stream_cm, response), response

    async def _post_with_retries(
        self, url: str, payload: dict, headers: dict
    ) -> httpx.Response:
        client = self._external_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient()

        try:
            attempt = 0
            while True:
                try:
                    return await client.post(
                        url, json=payload, headers=headers, timeout=self._timeout
                    )
                except RETRYABLE_EXCEPTIONS as exc:
                    attempt += 1
                    if attempt > self._max_retries:
                        raise OpenAIError(
                            f"Upstream request failed after {attempt} attempts: {exc}",
                            status_code=503,
                            error_type="api_error",
                            code="upstream_unavailable",
                        ) from exc
                    logger.warning(
                        "Retrying upstream request (attempt %d/%d) after %s",
                        attempt,
                        self._max_retries,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(self._backoff_seconds * (2 ** (attempt - 1)))
        finally:
            if owns_client:
                await client.aclose()

    def _parse_upstream_json_response(
        self, response: httpx.Response, response_model: type[ResponseModelT]
    ) -> ResponseModelT:
        if response.status_code >= 400:
            raise normalize_upstream_error(response.status_code, response.content)

        try:
            data = response.json()
        except ValueError as exc:
            raise OpenAIError(
                "Upstream returned a non-JSON response.",
                status_code=502,
                error_type="api_error",
                code="invalid_upstream_response",
            ) from exc

        try:
            return response_model.model_validate(data)
        except Exception as exc:
            raise OpenAIError(
                "Upstream response did not match the expected schema.",
                status_code=502,
                error_type="api_error",
                code="invalid_upstream_response",
            ) from exc


class _EnteredStreamContext:
    """Wraps an httpx stream context manager whose __aenter__ already ran.

    Lets us retry the connect phase (opening a fresh stream() per attempt)
    while still exposing an `async with` for the caller to release resources
    exactly once, regardless of how many attempts were made getting there.
    """

    def __init__(self, stream_cm, response: httpx.Response) -> None:
        self._stream_cm = stream_cm
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._stream_cm.__aexit__(exc_type, exc, tb)


@lru_cache
def get_upstream_client() -> UpstreamClient:
    return UpstreamClient()
