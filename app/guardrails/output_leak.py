"""Fast, local, regex-based scanning of *model output* for sensitive
information -- a standalone post-filter, independent of GUARDRAILS_MODE
(see app.guardrails.service), that runs on every chat completion response
before it's returned to the caller. Modeled on OpenRouter's Sensitive Info
Guardrail (https://openrouter.ai/docs/guides/features/guardrails/sensitive-info)
and OWASP's LLM02 (Sensitive Information Disclosure) category, and built as
the response-side sibling of app.guardrails.prompt_injection (which covers
the request side, LLM01) -- same Flag/Redact/Block action model, same
file-backed/hot-reloadable/audited config shape, deliberately zero imports
from app.guardrails.service/catalog/editable_config or from
app.guardrails.prompt_injection itself, no coupling to NeMo.

Extends OpenRouter's fixed built-in set in two ways:
- A `secret_token` category (API keys, AWS access keys, JWTs, PEM private
  key headers, generic `api_key: ...`-shaped assignments) -- the leak this
  gateway is most exposed to isn't just end-user PII flowing back out, it's
  a model regurgitating a credential that appeared earlier in its own
  context (a system prompt, a tool result, RAG-retrieved text).
- Admin-defined custom regex patterns (`custom_patterns`), each with its
  own name and action -- OpenRouter's equivalent (`content_filters`) is
  regex-only too; unlike their built-ins, every category and custom
  pattern here is regex/Luhn-only (no NLP name/address detection à la
  Presidio) to keep this a fast, dependency-free pre-filter, matching
  OpenRouter's own "use regex-only presets if latency is critical"
  guidance -- see the README section this module is documented under for
  where NLP detection would slot in as a later, heavier addition.

Redaction uses a category-specific placeholder (`[EMAIL]`, `[SSN]`, ...)
rather than one generic token, again matching OpenRouter's documented
behavior -- see redaction_token().
"""

import dataclasses
import enum
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.atomic_write import atomic_write_text
from app.core.audit import record_entry as record_audit_entry
from app.core.distributed_lock import admin_write_lock
from app.schemas.chat import ChatCompletionResponse, TextContentPart

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "output_leak.yaml"
CONFIG_PATH_ENV_VAR = "OPENBOUNCER_OUTPUT_LEAK_CONFIG"


class OutputLeakAction(str, enum.Enum):
    DISABLED = "disabled"
    FLAG = "flag"
    REDACT = "redact"
    BLOCK = "block"


# Most-restrictive-wins precedence, same idea (and same ordering) as
# app.guardrails.prompt_injection._ACTION_SEVERITY.
_ACTION_SEVERITY: dict[OutputLeakAction, int] = {
    OutputLeakAction.DISABLED: 0,
    OutputLeakAction.FLAG: 1,
    OutputLeakAction.REDACT: 2,
    OutputLeakAction.BLOCK: 3,
}


class OutputLeakCategory(str, enum.Enum):
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    SECRET_TOKEN = "secret_token"
    # Not a real, independently-configurable category -- every
    # admin-defined custom_patterns match is grouped under this one value
    # so it stays a small, bounded, code-defined Prometheus label (see
    # OUTPUT_LEAK_MATCHES_TOTAL in app.core.metrics); the pattern's own
    # admin-supplied `name` is unbounded text, so it's logged, never used
    # as a metric label.
    CUSTOM = "custom"


@dataclass(frozen=True)
class CategoryMatch:
    category: OutputLeakCategory
    pattern_name: str
    matched_text: str
    span: tuple[int, int]
    # Resolved at match-creation time (unlike prompt_injection.CategoryMatch,
    # which resolves its action later from config) because a custom
    # pattern's action lives on the pattern itself, not in a fixed
    # category->action map -- see _custom_pattern_matches().
    action: OutputLeakAction
    # Which text segment of the message this span is relative to: None for
    # a plain string `content`, else the index into a list-of-parts
    # `content`'s TextContentPart entries -- same reasoning as
    # prompt_injection.CategoryMatch.segment.
    segment: int | None = None


@dataclass(frozen=True)
class ScanResult:
    action: OutputLeakAction
    matches: list[CategoryMatch]


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

_RAW_PATTERNS: dict[OutputLeakCategory, list[tuple[str, str]]] = {
    OutputLeakCategory.EMAIL: [
        ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\b"),
    ],
    OutputLeakCategory.PHONE: [
        # Requires at least one separator between the 3/3/4 digit groups
        # (optionally a leading +1/1 country code) -- an unformatted run of
        # 10 digits is deliberately not matched, to avoid flagging things
        # like order/tracking numbers that just happen to be 10 digits.
        ("phone", r"(?<!\d)(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"),
    ],
    OutputLeakCategory.SSN: [
        ("ssn", r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    ],
    OutputLeakCategory.IP_ADDRESS: [
        (
            "ipv4",
            r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
        ),
    ],
    OutputLeakCategory.SECRET_TOKEN: [
        ("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b"),
        ("openai_style_api_key", r"\bsk-[A-Za-z0-9]{16,}\b"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        ("private_key_header", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        (
            "generic_secret_assignment",
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{8,}['\"]?",
        ),
    ],
}

_PATTERN_REGISTRY: dict[OutputLeakCategory, list[tuple[str, "re.Pattern[str]"]]] = {
    category: [(name, re.compile(pattern)) for name, pattern in patterns]
    for category, patterns in _RAW_PATTERNS.items()
}

# Credit cards are handled separately from the table above: a bare digit-run
# regex has too many false positives (order numbers, phone numbers with
# separators stripped, ...) to gate a redact/block action on by itself, so
# every regex candidate is additionally verified with a Luhn checksum --
# candidates that fail it are dropped entirely, not just deprioritized.
_CREDIT_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


REDACTION_TOKEN = "[REDACTED]"

_REDACTION_TOKENS: dict[OutputLeakCategory, str] = {
    OutputLeakCategory.EMAIL: "[EMAIL]",
    OutputLeakCategory.PHONE: "[PHONE]",
    OutputLeakCategory.SSN: "[SSN]",
    OutputLeakCategory.CREDIT_CARD: "[CREDIT_CARD]",
    OutputLeakCategory.IP_ADDRESS: "[IP_ADDRESS]",
    OutputLeakCategory.SECRET_TOKEN: "[SECRET]",
}


def redaction_token(match: CategoryMatch) -> str:
    if match.category is OutputLeakCategory.CUSTOM:
        # Admin-chosen, human-readable label for their own pattern, e.g. a
        # custom_patterns entry named "project_codename" redacts to
        # "[PROJECT_CODENAME]".
        return f"[{match.pattern_name.upper()}]"
    return _REDACTION_TOKENS.get(match.category, REDACTION_TOKEN)


def _is_allow_listed(match: CategoryMatch, allow_list: list[str]) -> bool:
    matched_lower = match.matched_text.lower()
    return any(
        matched_lower in phrase.lower() or phrase.lower() in matched_lower
        for phrase in allow_list
        if phrase
    )


def _builtin_matches(text: str, config: "OutputLeakConfig") -> list[CategoryMatch]:
    matches = []
    for category, patterns in _PATTERN_REGISTRY.items():
        action = config.categories.get(category, OutputLeakAction.FLAG)
        if action is OutputLeakAction.DISABLED:
            continue
        for name, pattern in patterns:
            for m in pattern.finditer(text):
                matches.append(CategoryMatch(category, name, m.group(0), m.span(), action))
    return matches


def _credit_card_matches(text: str, config: "OutputLeakConfig") -> list[CategoryMatch]:
    action = config.categories.get(OutputLeakCategory.CREDIT_CARD, OutputLeakAction.FLAG)
    if action is OutputLeakAction.DISABLED:
        return []
    matches = []
    for m in _CREDIT_CARD_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            matches.append(
                CategoryMatch(OutputLeakCategory.CREDIT_CARD, "credit_card_luhn", m.group(0), m.span(), action)
            )
    return matches


def _custom_pattern_matches(text: str, config: "OutputLeakConfig") -> list[CategoryMatch]:
    matches = []
    for custom in config.custom_patterns:
        if custom.action is OutputLeakAction.DISABLED:
            continue
        # Validity is already enforced at config-save time
        # (CustomPattern._pattern_must_compile) -- re.compile here just
        # turns the stored source back into a matcher, not a validation
        # step, so a failure would indicate a hand-edited config file
        # rather than something to recover from silently.
        pattern = re.compile(custom.pattern)
        for m in pattern.finditer(text):
            matches.append(CategoryMatch(OutputLeakCategory.CUSTOM, custom.name, m.group(0), m.span(), custom.action))
    return matches


def scan_text(text: str, config: "OutputLeakConfig") -> list[CategoryMatch]:
    """Scans a single plain-text string (one message's content, or one
    text content-part) for sensitive-information matches, honoring
    config.allow_list. Used directly for streaming (deltas concatenate to
    plain text) and as the building block for scan_message_content()
    below (non-streaming, which also has to handle list-of-parts
    content)."""
    if not text:
        return []

    matches = _builtin_matches(text, config) + _credit_card_matches(text, config) + _custom_pattern_matches(text, config)
    matches = [m for m in matches if not _is_allow_listed(m, config.allow_list)]

    seen: set[tuple[OutputLeakCategory, str, tuple[int, int]]] = set()
    deduped = []
    for m in matches:
        key = (m.category, m.pattern_name, m.span)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    return deduped


def resolve_overall_action(matches: list[CategoryMatch]) -> OutputLeakAction:
    if not matches:
        return OutputLeakAction.DISABLED
    return max((m.action for m in matches), key=lambda a: _ACTION_SEVERITY[a])


def _merge_spans_with_tokens(matches: list[CategoryMatch]) -> list[tuple[tuple[int, int], str]]:
    ordered = sorted(((m.span, redaction_token(m)) for m in matches), key=lambda st: st[0])
    merged: list[tuple[tuple[int, int], str]] = []
    for span, token in ordered:
        if merged and span[0] <= merged[-1][0][1]:
            prev_span, prev_token = merged[-1]
            merged[-1] = ((prev_span[0], max(prev_span[1], span[1])), prev_token)
        else:
            merged.append((span, token))
    return merged


def redact_text(text: str, matches: list[CategoryMatch]) -> str:
    if not matches:
        return text
    pieces = []
    cursor = 0
    for (start, end), token in _merge_spans_with_tokens(matches):
        pieces.append(text[cursor:start])
        pieces.append(token)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


redact_preview = redact_text  # same shape as prompt_injection.redact_preview; kept as an alias for readability at call sites.


def scan_message_content(content, config: "OutputLeakConfig") -> list[CategoryMatch]:
    """Scans a ResponseChatMessage.content value (str | list[ContentPart])
    for sensitive-information matches."""
    if isinstance(content, str):
        return scan_text(content, config)
    matches = []
    for i, part in enumerate(content):
        if isinstance(part, TextContentPart):
            for m in scan_text(part.text, config):
                matches.append(dataclasses.replace(m, segment=i))
    return matches


def redact_message_content(content, matches: list[CategoryMatch]):
    by_segment: dict[int | None, list[CategoryMatch]] = {}
    for m in matches:
        by_segment.setdefault(m.segment, []).append(m)

    if isinstance(content, str):
        return redact_text(content, by_segment.get(None, []))

    new_parts = list(content)
    for i, part in enumerate(new_parts):
        if isinstance(part, TextContentPart) and i in by_segment:
            new_parts[i] = part.model_copy(update={"text": redact_text(part.text, by_segment[i])})
    return new_parts


def scan_response(response: ChatCompletionResponse, config: "OutputLeakConfig") -> dict[int, ScanResult]:
    """Returns choice-index -> ScanResult for every choice with >=1 match.
    An empty dict means nothing matched -- return the response unchanged."""
    results: dict[int, ScanResult] = {}
    for i, choice in enumerate(response.choices):
        matches = scan_message_content(choice.message.content, config)
        if not matches:
            continue
        results[i] = ScanResult(action=resolve_overall_action(matches), matches=matches)
    return results


def apply_action(
    response: ChatCompletionResponse, results: dict[int, ScanResult]
) -> tuple[ChatCompletionResponse, OutputLeakAction, list[CategoryMatch]]:
    """Combines per-choice results into one overall decision (most
    restrictive wins), same shape as
    app.guardrails.prompt_injection.apply_action but over response choices
    instead of request messages. Never mutates the input response."""
    if not results:
        return response, OutputLeakAction.DISABLED, []

    overall_action = max((r.action for r in results.values()), key=lambda a: _ACTION_SEVERITY[a])
    all_matches = [m for r in results.values() for m in r.matches]

    if overall_action is not OutputLeakAction.REDACT:
        return response, overall_action, all_matches

    new_choices = list(response.choices)
    for i, result in results.items():
        if result.action is OutputLeakAction.REDACT:
            choice = new_choices[i]
            new_content = redact_message_content(choice.message.content, result.matches)
            new_choices[i] = choice.model_copy(update={"message": choice.message.model_copy(update={"content": new_content})})
    return response.model_copy(update={"choices": new_choices}), overall_action, all_matches


def requires_buffering(config: "OutputLeakConfig") -> bool:
    """Whether a streaming response must be fully buffered before anything
    is released to the client -- true iff at least one enabled category or
    custom pattern is configured redact/block. When false, tokens can be
    forwarded to the caller live as they're generated, with scanning done
    only for flag-level logging/metrics once the stream ends -- see
    app.api.routes.chat._with_output_leak_scan, and the same true-vs-
    buffered trade-off documented on
    app.guardrails.service.NemoLibraryGuardrailsService for output rails.
    """
    if not config.enabled:
        return False
    if any(action in (OutputLeakAction.REDACT, OutputLeakAction.BLOCK) for action in config.categories.values()):
        return True
    return any(p.action in (OutputLeakAction.REDACT, OutputLeakAction.BLOCK) for p in config.custom_patterns)


def extract_stream_delta_text(frame: str) -> str:
    """Pulls the incremental content text out of one SSE `data: ...\\n\\n`
    chat-completion-chunk frame (this gateway's wire format, see
    app.guardrails.service._sse_chunk / UpstreamClient.stream_chat_completion)
    -- used by app.api.routes.chat's output-leak stream wrapper to
    accumulate the full response text as chunks arrive from either a
    direct upstream or a guardrails backend, without needing to import
    either. Returns "" for `[DONE]`, malformed frames, and frames with no
    delta content (e.g. a trailing usage-only chunk) -- anything that
    isn't actual bot text.
    """
    body = frame.strip()
    if not body.startswith("data:"):
        return ""
    payload = body[len("data:") :].strip()
    if payload == "[DONE]":
        return ""
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta")
    content = delta.get("content") if isinstance(delta, dict) else None
    return content if isinstance(content, str) else ""


# ---------------------------------------------------------------------------
# Config: file-backed, lru_cache'd, hot-reloadable -- same shape as
# app.guardrails.prompt_injection.PromptInjectionConfig.
# ---------------------------------------------------------------------------

_CUSTOM_PATTERN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class CustomPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=_CUSTOM_PATTERN_NAME_RE.pattern)
    pattern: str = Field(min_length=1, max_length=500)
    action: OutputLeakAction = OutputLeakAction.FLAG

    @field_validator("pattern")
    @classmethod
    def _pattern_must_compile(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc
        return value


class OutputLeakConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allow_list: list[str] = Field(default_factory=list)
    categories: dict[OutputLeakCategory, OutputLeakAction] = Field(default_factory=dict)
    custom_patterns: list[CustomPattern] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_default_categories(self) -> "OutputLeakConfig":
        # Same "omitted = flag, not disabled" posture as
        # PromptInjectionConfig._fill_default_categories -- an admin has to
        # opt out explicitly. CUSTOM is rejected outright (not just
        # excluded from the defaults) if present: it isn't a real
        # independently-configurable category, its action lives per-entry
        # on custom_patterns instead (see OutputLeakCategory.CUSTOM).
        if OutputLeakCategory.CUSTOM in self.categories:
            raise ValueError(
                "'custom' is not an independently configurable category -- use custom_patterns instead."
            )
        for category in OutputLeakCategory:
            if category is OutputLeakCategory.CUSTOM:
                continue
            self.categories.setdefault(category, OutputLeakAction.FLAG)
        return self


def parse_output_leak_config(raw_yaml: str) -> OutputLeakConfig:
    data = yaml.safe_load(raw_yaml) or {}
    return OutputLeakConfig(**data)


def _resolve_config_path() -> Path:
    explicit = os.environ.get(CONFIG_PATH_ENV_VAR)
    return Path(explicit) if explicit else DEFAULT_CONFIG_PATH


def load_output_leak_config() -> OutputLeakConfig:
    path = _resolve_config_path()
    if not path.exists():
        return OutputLeakConfig()
    return parse_output_leak_config(path.read_text())


@lru_cache
def get_output_leak_config() -> OutputLeakConfig:
    return load_output_leak_config()


# Same reasoning (and cross-replica coordination via REDIS_URL, see
# app.core.distributed_lock) as app.auth.keys._WRITE_LOCK_NAME /
# app.guardrails.prompt_injection._WRITE_LOCK_NAME.
_WRITE_LOCK_NAME = "output_leak"


async def _persist_config(config: OutputLeakConfig, *, actor_key_id: str, summary: str) -> None:
    path = _resolve_config_path()
    before = (
        path.read_text()
        if path.exists()
        else yaml.safe_dump(OutputLeakConfig().model_dump(mode="json"), sort_keys=False)
    )
    new_yaml = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    atomic_write_text(path, new_yaml)
    await record_audit_entry(
        actor_key_id=actor_key_id,
        resource_type="output_leak",
        resource_id=None,
        action="update_output_leak_config",
        summary=summary,
        path=path,
        before=before,
        after=new_yaml,
    )
    get_output_leak_config.cache_clear()


async def update_output_leak_config(
    *,
    actor_key_id: str,
    enabled: bool | None = None,
    allow_list: list[str] | None = None,
    categories: dict[OutputLeakCategory, OutputLeakAction] | None = None,
    custom_patterns: list[CustomPattern] | None = None,
) -> OutputLeakConfig:
    """Persists a partial update and invalidates get_output_leak_config()'s
    cache so it's live on the very next request -- no restart needed.

    `categories`, if given, is merged key-by-key into the current on-disk
    categories (not a full replacement), computed inside the lock against a
    freshly-loaded snapshot -- same reasoning as
    prompt_injection.update_prompt_injection_config. `custom_patterns`, if
    given, is a full replacement (there's no natural per-entry merge key
    to partially update by, unlike the fixed category enum).
    """
    async with admin_write_lock(_WRITE_LOCK_NAME):
        current = load_output_leak_config()
        updates: dict[str, object] = {}
        if enabled is not None:
            updates["enabled"] = enabled
        if allow_list is not None:
            updates["allow_list"] = allow_list
        if categories is not None:
            merged_categories = dict(current.categories)
            merged_categories.update(categories)
            updates["categories"] = merged_categories
        if custom_patterns is not None:
            updates["custom_patterns"] = custom_patterns

        updated = current.model_copy(update=updates)
        await _persist_config(
            updated,
            actor_key_id=actor_key_id,
            summary=f"Updated output-leak config: fields={sorted(updates.keys())}",
        )
        return updated
