"""Small httpx-based client for Prometheus's HTTP API.

Deliberately narrow: only the two read-only query endpoints
(app/api/routes/activity.py's fixed set of PromQL queries), not a general
Prometheus SDK. No retries -- these are telemetry reads for an admin
dashboard, not upstream model calls on the request-serving path, so a
single failed attempt just surfaces as "observability unavailable" rather
than needing UpstreamClient's retry/backoff machinery.
"""

import os
from functools import lru_cache

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0

PROMETHEUS_URL_ENV_VAR = "PROMETHEUS_URL"


class PrometheusError(RuntimeError):
    """Raised when Prometheus is unreachable or returns a non-success response."""


class PrometheusClient:
    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._external_client = http_client
        self._timeout = timeout

    async def query(self, promql: str) -> list[dict]:
        """Instant query -- returns Prometheus's `data.result` (a vector)."""
        return await self._get("/api/v1/query", {"query": promql})

    async def query_range(
        self, promql: str, *, start: float, end: float, step: str
    ) -> list[dict]:
        """Range query -- returns Prometheus's `data.result` (a matrix)."""
        return await self._get(
            "/api/v1/query_range",
            {"query": promql, "start": start, "end": end, "step": step},
        )

    async def _get(self, path: str, params: dict) -> list[dict]:
        client = self._external_client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient()

        try:
            try:
                response = await client.get(
                    f"{self._base_url}{path}", params=params, timeout=self._timeout
                )
            except httpx.HTTPError as exc:
                raise PrometheusError(f"Could not reach Prometheus: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code >= 400:
            raise PrometheusError(
                f"Prometheus returned {response.status_code}: {response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise PrometheusError("Prometheus returned a non-JSON response.") from exc

        if body.get("status") != "success":
            raise PrometheusError(
                f"Prometheus query failed: {body.get('error', 'unknown error')}"
            )

        return body.get("data", {}).get("result", [])


@lru_cache
def get_prometheus_client() -> PrometheusClient | None:
    """None means "not configured" -- see PROMETHEUS_URL_ENV_VAR. Only dgx's
    docker-compose.yml sets this (Prometheus isn't part of the generic
    template); app/api/routes/activity.py turns None into a clean 503
    rather than this being a hard main-vs-dgx code fork.
    """
    base_url = os.environ.get(PROMETHEUS_URL_ENV_VAR)
    if not base_url:
        return None
    return PrometheusClient(base_url=base_url)
