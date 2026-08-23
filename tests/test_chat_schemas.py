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


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather.",
        "parameters": {"type": "object", "properties": {"location": {"type": "string"}}},
    },
}


class TestToolCalling:
    def test_tools_and_tool_choice_accepted(self):
        req = ChatCompletionRequest(**_request(tools=[WEATHER_TOOL], tool_choice="auto"))
        assert req.tools[0].function.name == "get_weather"
        assert req.tool_choice == "auto"

    def test_named_tool_choice_accepted(self):
        req = ChatCompletionRequest(
            **_request(tools=[WEATHER_TOOL], tool_choice={"type": "function", "function": {"name": "get_weather"}})
        )
        assert req.tool_choice.function.name == "get_weather"

    def test_invalid_tool_choice_string_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(tools=[WEATHER_TOOL], tool_choice="sometimes"))

    def test_tool_missing_function_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(tools=[{"type": "function"}]))

    def test_tool_type_must_be_function(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(tools=[{"type": "retrieval", "function": WEATHER_TOOL["function"]}])
            )

    def test_assistant_message_with_tool_calls_and_null_content_accepted(self):
        req = ChatCompletionRequest(
            **_request(
                messages=[
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"location": "Paris"}'},
                            }
                        ],
                    }
                ]
            )
        )
        assert req.messages[0].content is None
        assert req.messages[0].tool_calls[0].function.name == "get_weather"

    def test_tool_role_message_with_tool_call_id_accepted(self):
        req = ChatCompletionRequest(
            **_request(
                messages=[
                    {"role": "user", "content": "weather?"},
                    {"role": "tool", "tool_call_id": "call_1", "content": '{"temp_c": 18}'},
                ]
            )
        )
        assert req.messages[1].tool_call_id == "call_1"

    def test_tool_calls_on_non_assistant_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(
                    messages=[
                        {
                            "role": "user",
                            "content": "hi",
                            "tool_calls": [
                                {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                            ],
                        }
                    ]
                )
            )

    def test_null_content_on_user_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(messages=[{"role": "user", "content": None}]))

    def test_null_content_on_system_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(**_request(messages=[{"role": "system", "content": None}]))

    def test_null_content_on_tool_message_rejected(self):
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                **_request(messages=[{"role": "tool", "tool_call_id": "call_1", "content": None}])
            )

    def test_null_content_with_no_tool_calls_on_assistant_message_accepted(self):
        # Not required to co-occur with tool_calls -- some deployments
        # store an empty-content assistant turn this way too.
        req = ChatCompletionRequest(**_request(messages=[{"role": "assistant", "content": None}]))
        assert req.messages[0].content is None

    def test_bare_tool_role_message_without_tool_call_id_still_accepted(self):
        # Pre-dates tool-calling support in this schema -- see ChatMessage's
        # tool_call_id docstring for why this stays permissive.
        req = ChatCompletionRequest(**_request(messages=[{"role": "tool", "content": "some result"}]))
        assert req.messages[0].tool_call_id is None

    def test_tools_omitted_defaults_to_none(self):
        req = ChatCompletionRequest(**_request())
        assert req.tools is None
        assert req.tool_choice is None
