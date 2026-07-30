import pytest


@pytest.mark.asyncio
async def test_list_models(client):
    response = await client.get("/v1/models")
    assert response.status_code == 200

    body = response.json()
    assert body["object"] == "list"

    # The test key (see conftest.py) is only allowed these models, so the
    # response is filtered down to them even though the full registry (see
    # config/models.yaml) has more.
    expected = {
        "nvidia/qwen3.6-nvfp4": "nvidia",
        "nvidia/gemme4-nvfp4": "nvidia",
        "nvidia/nemotron-vision": "nvidia",
        "ollama/nomic-embed-text": "ollama",
    }
    returned = {item["id"]: item["owned_by"] for item in body["data"]}
    assert returned == expected

    for item in body["data"]:
        assert item["object"] == "model"
        assert isinstance(item["created"], int)
