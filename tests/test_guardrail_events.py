import os
from pathlib import Path

import pytest

from app.core.guardrail_events import list_events, record_event


def _record(**overrides):
    fields = dict(
        request_id="req-1",
        key_id="test-key",
        guardrail="prompt_injection",
        model="local/gemma4-nvfp4",
        category="instruction_override",
        pattern_name="ignore_instructions",
        action="block",
        via="direct",
        snippet="ignore all previous instructions",
    )
    fields.update(overrides)
    return record_event(**fields)


def test_record_event_appends_and_is_listed():
    event = _record()

    listed = list_events(limit=10)
    assert len(listed) == 1
    assert listed[0].id == event.id
    assert listed[0].key_id == "test-key"
    assert listed[0].guardrail == "prompt_injection"
    assert listed[0].category == "instruction_override"
    assert listed[0].action == "block"
    assert listed[0].via == "direct"
    assert listed[0].snippet == "ignore all previous instructions"


def test_output_leak_event_has_no_via():
    event = _record(guardrail="output_leak", category="email", pattern_name="email", via=None, snippet="[EMAIL]")
    listed = list_events(limit=10)
    assert listed[0].via is None
    assert listed[0].snippet == "[EMAIL]"


def test_list_events_is_most_recent_first():
    for i in range(3):
        _record(pattern_name=f"pattern-{i}")

    listed = list_events(limit=10)
    assert [e.pattern_name for e in listed] == ["pattern-2", "pattern-1", "pattern-0"]


def test_list_events_respects_limit():
    for i in range(5):
        _record(pattern_name=f"pattern-{i}")

    assert len(list_events(limit=2)) == 2


def test_list_events_empty_when_no_log_file():
    assert list_events(limit=10) == []


def test_snippet_is_truncated_to_max_length():
    huge = "x" * 10_000
    _record(snippet=huge)
    listed = list_events(limit=1)
    assert len(listed[0].snippet) == 300


class TestFilters:
    def _seed(self):
        _record(key_id="key-a", guardrail="prompt_injection", action="block")
        _record(key_id="key-b", guardrail="output_leak", category="email", via=None, action="redact")
        _record(key_id="key-a", guardrail="output_leak", category="ssn", via=None, action="flag")

    def test_filter_by_key_id(self):
        self._seed()
        events = list_events(limit=10, key_id="key-a")
        assert len(events) == 2
        assert all(e.key_id == "key-a" for e in events)

    def test_filter_by_guardrail(self):
        self._seed()
        events = list_events(limit=10, guardrail="output_leak")
        assert len(events) == 2
        assert all(e.guardrail == "output_leak" for e in events)

    def test_filter_by_action(self):
        self._seed()
        events = list_events(limit=10, action="flag")
        assert len(events) == 1
        assert events[0].category == "ssn"

    def test_combined_filters(self):
        self._seed()
        events = list_events(limit=10, key_id="key-a", guardrail="output_leak")
        assert len(events) == 1
        assert events[0].category == "ssn"

    def test_no_filters_returns_everything(self):
        self._seed()
        assert len(list_events(limit=10)) == 3


class TestRetention:
    def test_log_stays_under_cap_without_trimming(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_GUARDRAIL_EVENTS_MAX_ENTRIES", "5")
        for i in range(5):
            _record(pattern_name=f"pattern-{i}")

        log_path = Path(os.environ["OPENBOUNCER_GUARDRAIL_EVENTS_PATH"])
        assert len(log_path.read_text().splitlines()) == 5

    def test_exceeding_cap_trims_oldest_entries(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_GUARDRAIL_EVENTS_MAX_ENTRIES", "3")
        for i in range(5):
            _record(pattern_name=f"pattern-{i}")

        events = list_events(limit=10)
        assert len(events) == 3
        assert [e.pattern_name for e in events] == ["pattern-4", "pattern-3", "pattern-2"]

    def test_invalid_max_entries_env_var_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_GUARDRAIL_EVENTS_MAX_ENTRIES", "not-a-number")
        for i in range(10):
            _record(pattern_name=f"pattern-{i}")

        log_path = Path(os.environ["OPENBOUNCER_GUARDRAIL_EVENTS_PATH"])
        # Default (20000) is well above 10 -- nothing gets trimmed.
        assert len(log_path.read_text().splitlines()) == 10
