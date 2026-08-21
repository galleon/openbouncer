FROM python:3.12-slim

# Set to "true" to include the optional `nemoguardrails` dependency
# (GUARDRAILS_MODE=nemo_library only -- everything else, including the
# default `disabled` mode, nemo_microservice, and the local regex-based
# prompt-injection/output-leak guardrails, works without it). Opt in via
# `docker compose -f docker-compose.yml -f docker-compose.nemo.yml up
# --build`, or `--build-arg INSTALL_NEMO=true` directly -- left "false" by
# default so the image stays free of NeMo's NVIDIA/transformer stack unless
# a deployment actually needs it. See the README's Guardrails section.
ARG INSTALL_NEMO=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (separate layer from app code) so code-only
# changes don't invalidate the dependency-install cache.
COPY pyproject.toml uv.lock ./
RUN if [ "$INSTALL_NEMO" = "true" ]; then \
        uv sync --frozen --no-install-project --no-dev --extra nemo; \
    else \
        uv sync --frozen --no-install-project --no-dev; \
    fi

COPY app ./app
COPY config ./config
RUN if [ "$INSTALL_NEMO" = "true" ]; then \
        uv sync --frozen --no-dev --extra nemo; \
    else \
        uv sync --frozen --no-dev; \
    fi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=3)" || exit 1


# --proxy-headers is on by default but only trusts X-Forwarded-Proto from
# 127.0.0.1 -- behind the caddy service in docker-compose.yml, requests
# arrive from caddy's container IP on the internal docker network instead,
# so without --forwarded-allow-ips those headers are silently ignored and
# redirect responses (e.g. /ui -> /ui/) get built with the wrong scheme
# (http instead of https), which browsers reject. "*" is safe here since
# gateway is never exposed directly (see docker-compose.yml's loopback-only
# port binding) -- caddy is the only thing that can reach it besides the
# host itself.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
