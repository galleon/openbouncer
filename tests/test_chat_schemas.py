import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatCompletionRequest


def _request(**overrides):
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    payload.update(overrides)
    return payload


class TestTextOnlyContent:
    def test_string_content_is_valid(self):
        req = ChatCompletionRequest(**_request())
        assert req.messages[0].content == "Hello"

    def test_text_content_parts_are_valid(self):
        payload = _request(
            messages=[{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]
        )
        req = ChatCompletionRequest(**payload)
        assert req.messages[0].content[0].text == "Hello"

    @pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
    def test_all_supported_roles_accepted(self, role):
        req = ChatCompletionRequest(
            **_request(messages=[{"role": role, "content": "hi"}])
        )
        assert req.messages[0].role == role

    def test_unsupported_role_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(messages=[{"role": "developer", "content": "hi"}])
            )

    def test_unsupported_top_level_field_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ChatCompletionRequest(**_request(n=3))
        assert any(e["type"] == "extra_forbidden" for e in exc_info.value.errors())

    def test_unsupported_message_field_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(
                    messages=[{"role": "user", "content": "hi", "name": "bob"}]
                )
            )

    def test_optional_fields_accepted(self):
        req = ChatCompletionRequest(
            **_request(
                temperature=0.5,
                top_p=0.9,
                max_tokens=100,
                stream=True,
                stop=["\n"],
                user="user-123",
            )
        )
        assert req.temperature == 0.5
        assert req.top_p == 0.9
        assert req.max_tokens == 100
        assert req.stream is True
        assert req.stop == ["\n"]
        assert req.user == "user-123"

    def test_temperature_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(temperature=2.5))

    def test_top_p_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(top_p=1.5))

    def test_empty_messages_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(messages=[]))


class TestImageInputContent:
    def test_image_url_part_is_valid(self):
        payload = _request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        },
                    ],
                }
            ]
        )
        req = ChatCompletionRequest(**payload)
        assert req.messages[0].content[1].image_url.url == "https://example.com/cat.png"

    def test_image_url_detail_defaults_to_auto(self):
        payload = _request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/cat.png"},
                        }
                    ],
                }
            ]
        )
        req = ChatCompletionRequest(**payload)
        assert req.messages[0].content[0].image_url.detail == "auto"

    def test_image_url_explicit_detail_accepted(self):
        payload = _request(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.com/cat.png",
                                "detail": "high",
                            },
                        }
                    ],
                }
            ]
        )
        req = ChatCompletionRequest(**payload)
        assert req.messages[0].content[0].image_url.detail == "high"

    def test_image_url_missing_url_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(
                    messages=[
                        {"role": "user", "content": [{"type": "image_url", "image_url": {}}]}
                    ]
                )
            )

    def test_image_url_invalid_detail_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://example.com/cat.png",
                                        "detail": "ultra",
                                    },
                                }
                            ],
                        }
                    ]
                )
            )

    def test_image_url_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://example.com/cat.png",
                                        "resolution": "4k",
                                    },
                                }
                            ],
                        }
                    ]
                )
            )

    def test_unsupported_content_part_type_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "video_url", "video_url": {"url": "https://example.com/cat.mp4"}}
                            ],
                        }
                    ]
                )
            )
