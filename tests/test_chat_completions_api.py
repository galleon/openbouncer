import pytest


@pytest.mark.asyncio
async def test_chat_completions_text_only(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nvidia/qwen3.6-nvfp4",
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "You said: hello there"


@pytest.mark.asyncio
async def test_chat_completions_image_input(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nvidia/nemotron-vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        },
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "describe this" in body["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_chat_completions_rejects_unsupported_top_level_field(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nvidia/qwen3.6-nvfp4",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 5,
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "n" in body["error"]["param"]
    assert "n" in body["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completions_rejects_image_input_missing_url(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nvidia/nemotron-vision",
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {}}]}
            ],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] is None


@pytest.mark.asyncio
async def test_chat_completions_rejects_unsupported_content_part_type(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nvidia/nemotron-vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": "https://example.com/cat.mp4"}}
                    ],
                }
            ],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_chat_completions_accepts_all_allowed_models(client):
    for model_id in (
        "nvidia/qwen3.6-nvfp4",
        "nvidia/gemme4-nvfp4",
        "nvidia/nemotron-vision",
    ):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["model"] == model_id


@pytest.mark.asyncio
async def test_chat_completions_rejects_unregistered_model(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "model_not_found"
    assert body["error"]["param"] == "model"


@pytest.mark.asyncio
async def test_chat_completions_rejects_upstream_model_name(client):
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nvidia/Qwen3.6-27B-NVFP4",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "model_not_found"
