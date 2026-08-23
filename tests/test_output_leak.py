import json

import pytest
from pydantic import ValidationError

from app.guardrails.output_leak import (
    CustomPattern,
    OutputLeakAction,
    OutputLeakCategory,
    OutputLeakConfig,
    accumulate_tool_call_deltas,
    apply_action,
    assembled_tool_calls,
    extract_stream_delta_text,
    load_output_leak_config,
    parse_output_leak_config,
    redact_text,
    requires_buffering,
    resolve_overall_action,
    scan_message_content,
    scan_response,
    scan_text,
    scan_tool_call_argument_text,
    scan_tool_calls,
)
from app.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionUsage,
    FunctionCall,
    ImageContentPart,
    ImageURL,
    ResponseChatMessage,
    TextContentPart,
    ToolCall,
)


def _config(**category_overrides: OutputLeakAction) -> OutputLeakConfig:
    return OutputLeakConfig(enabled=True, categories=dict(category_overrides))


def _response(content) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test",
        created=1700000000,
        model="local/gemma4-nvfp4",
        choices=[ChatCompletionChoice(index=0, message=ResponseChatMessage(role="assistant", content=content))],
        usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class TestBuiltinCategories:
    @pytest.mark.parametrize(
        "text,category",
        [
            ("reach me at jane.doe@example.com please", OutputLeakCategory.EMAIL),
            ("call me at 415-555-0132 tomorrow", OutputLeakCategory.PHONE),
            ("your SSN on file is 123-45-6789", OutputLeakCategory.SSN),
            ("the server's address is 10.0.0.42 internally", OutputLeakCategory.IP_ADDRESS),
            ("AKIAABCDEFGHIJKLMNOP is the access key", OutputLeakCategory.SECRET_TOKEN),
            ("api_key: sUp3rSecretValue123", OutputLeakCategory.SECRET_TOKEN),
            ("sk-thisisaveryfakekeyabcdef1234567890", OutputLeakCategory.SECRET_TOKEN),
            ("-----BEGIN PRIVATE KEY-----\nMIIB...\n-----END PRIVATE KEY-----", OutputLeakCategory.SECRET_TOKEN),
        ],
    )
    def test_category_detected(self, text, category):
        matches = scan_text(text, _config())
        assert any(m.category is category for m in matches)

    @pytest.mark.parametrize(
        "text",
        [
            "the weather is nice today",
            "here's a summary of your document",
            "the answer to your question is 42",
        ],
    )
    def test_benign_text_has_no_matches(self, text):
        assert scan_text(text, _config()) == []

    def test_bare_ten_digit_run_is_not_flagged_as_phone(self):
        # No separators between digit groups -- deliberately not matched,
        # to avoid flagging order/tracking numbers (see module docstring).
        matches = scan_text("order number 4155550132 confirmed", _config())
        assert not any(m.category is OutputLeakCategory.PHONE for m in matches)


class TestCreditCardLuhnCheck:
    def test_valid_luhn_card_number_detected(self):
        # 4111 1111 1111 1111 is a well-known Luhn-valid test card number.
        matches = scan_text("your card ending is 4111 1111 1111 1111", _config())
        assert any(m.category is OutputLeakCategory.CREDIT_CARD for m in matches)

    def test_luhn_invalid_digit_run_is_not_flagged(self):
        matches = scan_text("tracking number 1234 5678 9012 3456", _config())
        assert not any(m.category is OutputLeakCategory.CREDIT_CARD for m in matches)


class TestCategoryDisabled:
    def test_disabled_category_never_appears(self):
        config = OutputLeakConfig(
            enabled=True, categories={OutputLeakCategory.EMAIL: OutputLeakAction.DISABLED}
        )
        matches = scan_text("contact jane.doe@example.com", config)
        assert matches == []


class TestAllowList:
    def test_allow_listed_phrase_suppresses_its_own_match(self):
        config = _config().model_copy(update={"allow_list": ["support@example.com"]})
        matches = scan_text("email support@example.com for help", config)
        assert matches == []

    def test_allow_list_does_not_suppress_unrelated_matches(self):
        config = _config().model_copy(update={"allow_list": ["unrelated@example.com"]})
        matches = scan_text("email jane.doe@example.com for help", config)
        assert len(matches) == 1


class TestCustomPatterns:
    def test_custom_pattern_matches_and_carries_its_own_action(self):
        config = OutputLeakConfig(
            enabled=True,
            custom_patterns=[CustomPattern(name="project_codename", pattern=r"\bProjectPhoenix\b", action=OutputLeakAction.BLOCK)],
        )
        matches = scan_text("this relates to ProjectPhoenix", config)
        assert len(matches) == 1
        assert matches[0].category is OutputLeakCategory.CUSTOM
        assert matches[0].pattern_name == "project_codename"
        assert matches[0].action is OutputLeakAction.BLOCK

    def test_custom_pattern_redacts_to_uppercased_name(self):
        config = OutputLeakConfig(
            enabled=True,
            custom_patterns=[CustomPattern(name="project_codename", pattern=r"\bProjectPhoenix\b", action=OutputLeakAction.REDACT)],
        )
        matches = scan_text("this relates to ProjectPhoenix launch", config)
        assert redact_text("this relates to ProjectPhoenix launch", matches) == "this relates to [PROJECT_CODENAME] launch"

    def test_invalid_regex_rejected_at_construction(self):
        with pytest.raises(ValidationError):
            CustomPattern(name="bad", pattern="[unterminated", action=OutputLeakAction.FLAG)

    def test_disabled_custom_pattern_is_skipped(self):
        config = OutputLeakConfig(
            enabled=True,
            custom_patterns=[CustomPattern(name="x", pattern=r"\bfoo\b", action=OutputLeakAction.DISABLED)],
        )
        assert scan_text("foo bar", config) == []


class TestActionPrecedence:
    def test_most_restrictive_action_wins(self):
        matches = scan_text(
            "email jane@example.com, ip 10.0.0.1, card 4111 1111 1111 1111",
            OutputLeakConfig(
                enabled=True,
                categories={
                    OutputLeakCategory.EMAIL: OutputLeakAction.FLAG,
                    OutputLeakCategory.IP_ADDRESS: OutputLeakAction.REDACT,
                    OutputLeakCategory.CREDIT_CARD: OutputLeakAction.BLOCK,
                },
            ),
        )
        assert resolve_overall_action(matches) is OutputLeakAction.BLOCK

    def test_no_matches_resolves_to_disabled(self):
        assert resolve_overall_action([]) is OutputLeakAction.DISABLED


class TestRedaction:
    def test_redacts_with_category_specific_token(self):
        matches = scan_text("reach jane.doe@example.com now", _config(email=OutputLeakAction.REDACT))
        assert redact_text("reach jane.doe@example.com now", matches) == "reach [EMAIL] now"

    def test_overlapping_matches_do_not_crash_redaction(self):
        text = "api_key: sk-thisisaveryfakekeyabcdef1234567890"
        matches = scan_text(text, _config(secret_token=OutputLeakAction.REDACT))
        redacted = redact_text(text, matches)
        assert "sk-thisisaveryfakekeyabcdef1234567890" not in redacted
        assert "[SECRET]" in redacted


class TestScanResponse:
    def test_scans_plain_string_content(self):
        response = _response("contact jane.doe@example.com for details")
        results = scan_response(response, _config(email=OutputLeakAction.REDACT))
        assert 0 in results
        assert results[0].action is OutputLeakAction.REDACT

    def test_scans_text_parts_only_in_list_content(self):
        response = _response(
            [
                TextContentPart(type="text", text="contact jane.doe@example.com"),
                ImageContentPart(type="image_url", image_url=ImageURL(url="https://example.com/x.png")),
            ]
        )
        results = scan_response(response, _config(email=OutputLeakAction.REDACT))
        assert 0 in results

    def test_no_match_returns_empty_dict(self):
        response = _response("nothing sensitive here")
        assert scan_response(response, _config()) == {}


class TestApplyAction:
    def test_redact_rewrites_plain_string_content(self):
        response = _response("contact jane.doe@example.com for details")
        results = scan_response(response, _config(email=OutputLeakAction.REDACT))
        new_response, action, matches = apply_action(response, results)
        assert action is OutputLeakAction.REDACT
        assert new_response.choices[0].message.content == "contact [EMAIL] for details"
        assert response.choices[0].message.content == "contact jane.doe@example.com for details"  # original untouched

    def test_redact_rewrites_text_parts_leaves_image_parts_alone(self):
        response = _response(
            [
                TextContentPart(type="text", text="contact jane.doe@example.com"),
                ImageContentPart(type="image_url", image_url=ImageURL(url="https://example.com/x.png")),
            ]
        )
        results = scan_response(response, _config(email=OutputLeakAction.REDACT))
        new_response, action, _ = apply_action(response, results)
        parts = new_response.choices[0].message.content
        assert parts[0].text == "contact [EMAIL]"
        assert parts[1].image_url.url == "https://example.com/x.png"

    def test_flag_leaves_response_unchanged(self):
        response = _response("contact jane.doe@example.com")
        results = scan_response(response, _config())  # every category defaults to flag
        new_response, action, _ = apply_action(response, results)
        assert action is OutputLeakAction.FLAG
        assert new_response is response

    def test_block_leaves_response_unchanged(self):
        response = _response("contact jane.doe@example.com")
        results = scan_response(response, _config(email=OutputLeakAction.BLOCK))
        new_response, action, _ = apply_action(response, results)
        assert action is OutputLeakAction.BLOCK
        assert new_response is response

    def test_no_results_returns_disabled(self):
        response = _response("nothing sensitive")
        new_response, action, matches = apply_action(response, {})
        assert action is OutputLeakAction.DISABLED
        assert matches == []
        assert new_response is response


class TestRequiresBuffering:
    def test_disabled_config_never_requires_buffering(self):
        assert requires_buffering(OutputLeakConfig(enabled=False)) is False

    def test_flag_only_categories_do_not_require_buffering(self):
        assert requires_buffering(_config()) is False  # every category defaults to flag

    def test_any_redact_category_requires_buffering(self):
        assert requires_buffering(_config(email=OutputLeakAction.REDACT)) is True

    def test_any_block_category_requires_buffering(self):
        assert requires_buffering(_config(ssn=OutputLeakAction.BLOCK)) is True

    def test_custom_pattern_with_block_requires_buffering(self):
        config = OutputLeakConfig(
            enabled=True,
            custom_patterns=[CustomPattern(name="x", pattern=r"foo", action=OutputLeakAction.BLOCK)],
        )
        assert requires_buffering(config) is True


class TestExtractStreamDeltaText:
    def test_extracts_content_from_a_valid_chunk(self):
        frame = 'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
        assert extract_stream_delta_text(frame) == "hello"

    def test_done_marker_returns_empty_string(self):
        assert extract_stream_delta_text("data: [DONE]\n\n") == ""

    def test_malformed_json_returns_empty_string(self):
        assert extract_stream_delta_text("data: {not valid json\n\n") == ""

    def test_empty_choices_returns_empty_string(self):
        frame = 'data: {"id":"c1","object":"chat.completion.chunk","choices":[]}\n\n'
        assert extract_stream_delta_text(frame) == ""

    def test_finish_reason_chunk_with_empty_delta_returns_empty_string(self):
        frame = 'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        assert extract_stream_delta_text(frame) == ""


class TestConfigParsing:
    def test_round_trip(self):
        config = OutputLeakConfig(
            enabled=True,
            allow_list=["safe@example.com"],
            categories={OutputLeakCategory.EMAIL: OutputLeakAction.BLOCK},
            custom_patterns=[CustomPattern(name="x", pattern=r"foo", action=OutputLeakAction.REDACT)],
        )
        import yaml

        raw = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
        parsed = parse_output_leak_config(raw)
        assert parsed == config

    def test_unknown_category_key_rejected(self):
        with pytest.raises(ValidationError):
            parse_output_leak_config("categories:\n  not_a_real_category: block\n")

    def test_custom_category_key_rejected_in_categories_map(self):
        # "custom" is a valid OutputLeakCategory value but not
        # independently configurable via the categories map.
        with pytest.raises(ValidationError):
            parse_output_leak_config("categories:\n  custom: block\n")

    def test_missing_categories_default_to_flag_excluding_custom(self):
        config = parse_output_leak_config("enabled: true\n")
        assert all(action is OutputLeakAction.FLAG for action in config.categories.values())
        assert len(config.categories) == len(OutputLeakCategory) - 1
        assert OutputLeakCategory.CUSTOM not in config.categories

    def test_missing_file_defaults_to_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_OUTPUT_LEAK_CONFIG", str(tmp_path / "nope.yaml"))
        config = load_output_leak_config()
        assert config.enabled is False


class TestScanMessageContent:
    def test_empty_string_content_returns_no_matches(self):
        assert scan_message_content("", _config()) == []

    def test_none_content_returns_no_matches(self):
        # A tool-calling response's content (ResponseChatMessage.content is
        # None when the message is entirely a tool call) -- must not crash.
        assert scan_message_content(None, _config(email=OutputLeakAction.BLOCK)) == []


def _tool_call(arguments: str, *, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, type="function", function=FunctionCall(name="f", arguments=arguments))


class TestScanToolCallArgumentText:
    def test_finds_a_match_and_tags_tool_call_index(self):
        config = _config(email=OutputLeakAction.FLAG)
        matches = scan_tool_call_argument_text(2, '{"to": "jane.doe@example.com"}', config)
        assert len(matches) == 1
        assert matches[0].tool_call_index == 2
        assert matches[0].category is OutputLeakCategory.EMAIL

    def test_no_match_returns_empty(self):
        config = _config(email=OutputLeakAction.FLAG)
        assert scan_tool_call_argument_text(0, '{"location": "Paris"}', config) == []

    def test_redact_configured_category_escalates_to_block(self):
        config = _config(email=OutputLeakAction.REDACT)
        matches = scan_tool_call_argument_text(0, '{"to": "jane.doe@example.com"}', config)
        assert len(matches) == 1
        assert matches[0].action is OutputLeakAction.BLOCK

    def test_flag_configured_category_stays_flag(self):
        config = _config(email=OutputLeakAction.FLAG)
        matches = scan_tool_call_argument_text(0, '{"to": "jane.doe@example.com"}', config)
        assert matches[0].action is OutputLeakAction.FLAG

    def test_block_configured_category_stays_block(self):
        config = _config(email=OutputLeakAction.BLOCK)
        matches = scan_tool_call_argument_text(0, '{"to": "jane.doe@example.com"}', config)
        assert matches[0].action is OutputLeakAction.BLOCK


class TestScanToolCalls:
    def test_none_tool_calls_returns_no_matches(self):
        assert scan_tool_calls(None, _config(email=OutputLeakAction.BLOCK)) == []

    def test_empty_list_returns_no_matches(self):
        assert scan_tool_calls([], _config(email=OutputLeakAction.BLOCK)) == []

    def test_scans_every_tool_call_by_position(self):
        config = _config(email=OutputLeakAction.FLAG)
        tool_calls = [
            _tool_call('{"location": "Paris"}'),
            _tool_call('{"to": "jane.doe@example.com"}'),
        ]
        matches = scan_tool_calls(tool_calls, config)
        assert len(matches) == 1
        assert matches[0].tool_call_index == 1


class TestScanResponseIncludesToolCalls:
    def _response_with_tool_calls(self, *, content=None, tool_calls=None) -> ChatCompletionResponse:
        return ChatCompletionResponse(
            id="chatcmpl-test",
            created=1700000000,
            model="local/gemma4-nvfp4",
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ResponseChatMessage(role="assistant", content=content, tool_calls=tool_calls),
                    finish_reason="tool_calls",
                )
            ],
            usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    def test_leak_inside_tool_call_arguments_is_found(self):
        response = self._response_with_tool_calls(tool_calls=[_tool_call('{"to": "jane.doe@example.com"}')])
        results = scan_response(response, _config(email=OutputLeakAction.BLOCK))
        assert 0 in results
        assert results[0].action is OutputLeakAction.BLOCK

    def test_clean_tool_call_with_no_content_finds_nothing(self):
        response = self._response_with_tool_calls(tool_calls=[_tool_call('{"location": "Paris"}')])
        results = scan_response(response, _config(email=OutputLeakAction.BLOCK))
        assert results == {}


class TestApplyActionWithToolCalls:
    def _response_with_tool_calls(self, *, content=None, tool_calls=None) -> ChatCompletionResponse:
        return ChatCompletionResponse(
            id="chatcmpl-test",
            created=1700000000,
            model="local/gemma4-nvfp4",
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ResponseChatMessage(role="assistant", content=content, tool_calls=tool_calls),
                    finish_reason="tool_calls",
                )
            ],
            usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    def test_content_redact_does_not_touch_clean_tool_calls(self):
        response = self._response_with_tool_calls(
            content="contact jane.doe@example.com",
            tool_calls=[_tool_call('{"location": "Paris"}')],
        )
        results = scan_response(response, _config(email=OutputLeakAction.REDACT))
        new_response, action, _matches = apply_action(response, results)
        assert action is OutputLeakAction.REDACT
        assert "[EMAIL]" in new_response.choices[0].message.content
        # Untouched -- the tool call had no match of its own.
        assert new_response.choices[0].message.tool_calls[0].function.arguments == '{"location": "Paris"}'

    def test_tool_call_block_overrides_content_flag(self):
        response = self._response_with_tool_calls(
            content="nothing sensitive here",
            tool_calls=[_tool_call('{"to": "jane.doe@example.com"}')],
        )
        config = _config(email=OutputLeakAction.BLOCK)
        results = scan_response(response, config)
        _new_response, action, matches = apply_action(response, results)
        assert action is OutputLeakAction.BLOCK
        assert any(m.tool_call_index == 0 for m in matches)


class TestAccumulateToolCallDeltas:
    def _chunk(self, tool_calls_delta: list[dict]) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"tool_calls": tool_calls_delta}, "finish_reason": None}],
                }
            )
            + "\n\n"
        )

    def test_accumulates_id_type_name_once_and_arguments_incrementally(self):
        accumulator: dict[int, dict] = {}
        accumulate_tool_call_deltas(
            accumulator,
            self._chunk([{"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"a'}}]),
        )
        accumulate_tool_call_deltas(accumulator, self._chunk([{"index": 0, "function": {"arguments": '":1}'}}]))

        assert accumulator[0]["id"] == "call_1"
        assert accumulator[0]["type"] == "function"
        assert accumulator[0]["name"] == "f"
        assert accumulator[0]["arguments"] == '{"a":1}'

    def test_multiple_indices_accumulate_independently(self):
        accumulator: dict[int, dict] = {}
        accumulate_tool_call_deltas(
            accumulator,
            self._chunk(
                [
                    {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                    {"index": 1, "id": "call_2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
                ]
            ),
        )
        assert set(accumulator.keys()) == {0, 1}
        assert accumulator[0]["id"] == "call_1"
        assert accumulator[1]["id"] == "call_2"

    def test_non_tool_call_frame_leaves_accumulator_unchanged(self):
        accumulator: dict[int, dict] = {}
        accumulate_tool_call_deltas(accumulator, "data: [DONE]\n\n")
        accumulate_tool_call_deltas(accumulator, "data: {not valid json\n\n")
        assert accumulator == {}


class TestAssembledToolCalls:
    def test_empty_accumulator_returns_none(self):
        assert assembled_tool_calls({}) is None

    def test_builds_ordered_tool_calls_list(self):
        accumulator = {
            1: {"id": "call_2", "type": "function", "name": "g", "arguments": "{}"},
            0: {"id": "call_1", "type": "function", "name": "f", "arguments": '{"a":1}'},
        }
        result = assembled_tool_calls(accumulator)
        assert result == [
            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}},
            {"id": "call_2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
        ]

    def test_missing_type_defaults_to_function(self):
        accumulator = {0: {"id": "call_1", "type": None, "name": "f", "arguments": "{}"}}
        result = assembled_tool_calls(accumulator)
        assert result[0]["type"] == "function"
