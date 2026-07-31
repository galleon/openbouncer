import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.keys import get_key_store, parse_key_store
from app.auth.rate_limiter import RateLimiter, get_rate_limiter
from app.main import app
from tests.conftest import LOW_LIMIT_HASH, LOW_LIMIT_KEY, RESTRICTED_HASH, RESTRICTED_KEY

CHAT_BODY = {
    "model": "local/gemma4-nvfp4",
    "messages": [{"role": "user", "content": "hi"}],
}
EMBEDDINGS_BODY = {"model": "local/gemma4-nvfp4", "input": "hi"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/v1/models", None),
        ("POST", "/v1/chat/completions", CHAT_BODY),
        ("POST", "/v1/embeddings", EMBEDDINGS_BODY),
    ],
)
async def test_missing_api_key_rejected(unauthenticated_client, method, path, body):
    response = await unauthenticated_client.request(method, path, json=body)
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["type"] == "authentication_error"
    assert payload["error"]["code"] == "missing_api_key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/v1/models", None),
        ("POST", "/v1/chat/completions", CHAT_BODY),
        ("POST", "/v1/embeddings", EMBEDDINGS_BODY),
    ],
)
async def test_invalid_api_key_rejected(unauthenticated_client, method, path, body):
    response = await unauthenticated_client.request(
        method, path, json=body, headers={"Authorization": "Bearer sk-totally-made-up"}
    )
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["type"] == "authentication_error"
    assert payload["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_malformed_authorization_header_rejected(unauthenticated_client):
    response = await unauthenticated_client.get(
        "/v1/models", headers={"Authorization": "not-a-bearer-token"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


@pytest.mark.asyncio
async def test_forbidden_model_rejected_on_chat_completions(restricted_client):
    response = await restricted_client.post(
        "/v1/chat/completions",
        json={
            # local/bge-m3 exists in the registry but isn't in
            # restricted_client's allowed_models (see conftest.py).
            "model": "local/bge-m3",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["type"] == "permission_error"
    assert payload["error"]["code"] == "model_not_allowed"
    assert payload["error"]["param"] == "model"


@pytest.mark.asyncio
async def test_forbidden_model_rejected_on_embeddings(restricted_client):
    response = await restricted_client.post(
        "/v1/embeddings",
        # local/bge-m3 exists in the registry but isn't in restricted_client's
        # allowed_models (see conftest.py).
        json={"model": "local/bge-m3", "input": "hi"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_allowed"


@pytest.mark.asyncio
async def test_allowed_model_still_succeeds_for_restricted_key(restricted_client):
    response = await restricted_client.post("/v1/chat/completions", json=CHAT_BODY)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_models_endpoint_filtered_to_allowed_models(restricted_client):
    response = await restricted_client.get("/v1/models")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert ids == {"local/gemma4-nvfp4"}


@pytest.mark.asyncio
async def test_rate_limit_exceeded_returns_429(low_limit_client):
    r1 = await low_limit_client.post("/v1/chat/completions", json=CHAT_BODY)
    r2 = await low_limit_client.post("/v1/chat/completions", json=CHAT_BODY)
    r3 = await low_limit_client.post("/v1/chat/completions", json=CHAT_BODY)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    payload = r3.json()
    assert payload["error"]["type"] == "rate_limit_error"
    assert payload["error"]["code"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_rate_limit_is_scoped_per_key():
    # Both fixtures independently override get_key_store on the same `app`
    # object, so they can't be combined as separate fixtures in one test
    # (the second override would replace the first). Build one store with
    # both keys instead, and drive two clients against it directly.
    combined_store_yaml = f"""
keys:
  - id: low-limit-key
    key_hash: {LOW_LIMIT_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 2
  - id: restricted-key
    key_hash: {RESTRICTED_HASH}
    allowed_models: [local/gemma4-nvfp4]
    requests_per_minute: 1000000
"""
    store = parse_key_store(combined_store_yaml)
    limiter = RateLimiter()
    app.dependency_overrides[get_key_store] = lambda: store
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            low_limit_headers = {"Authorization": f"Bearer {LOW_LIMIT_KEY}"}
            restricted_headers = {"Authorization": f"Bearer {RESTRICTED_KEY}"}

            # Exhaust the low-limit key's budget.
            await ac.post("/v1/chat/completions", json=CHAT_BODY, headers=low_limit_headers)
            await ac.post("/v1/chat/completions", json=CHAT_BODY, headers=low_limit_headers)
            exhausted = await ac.post(
                "/v1/chat/completions", json=CHAT_BODY, headers=low_limit_headers
            )
            assert exhausted.status_code == 429

            # A different key is unaffected.
            response = await ac.post(
                "/v1/chat/completions", json=CHAT_BODY, headers=restricted_headers
            )
            assert response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_key_store, None)
        app.dependency_overrides.pop(get_rate_limiter, None)


@pytest.mark.asyncio
async def test_response_includes_request_id_header(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id
    assert len(request_id) == 32  # uuid4().hex


@pytest.mark.asyncio
async def test_request_id_differs_between_requests(client):
    first = await client.get("/healthz")
    second = await client.get("/healthz")
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


@pytest.mark.asyncio
async def test_request_id_present_even_on_auth_error(unauthenticated_client):
    response = await unauthenticated_client.get("/v1/models")
    assert response.status_code == 401
    assert response.headers.get("x-request-id")
