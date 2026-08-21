"""Fast, local, regex-based prompt-injection detection.

A standalone pre-filter, independent of GUARDRAILS_MODE (see
app.guardrails.service) -- it runs before any LLM call, on every request,
regardless of whether nemo_library/nemo_microservice guardrails are also
enabled. Modeled on OpenRouter's prompt-injection guardrail
(https://openrouter.ai/docs/guides/features/guardrails/prompt-injection)
and OWASP's LLM01 (Prompt Injection) category.

Every existing rail in app.guardrails.service (including pii_regex and
jailbreak_input) is pass/block only, driven by nemoguardrails' Colang flow
model. This module has its own Flag/Redact/Block action model and a
scope selector (all messages vs. user messages only) that Colang doesn't
support -- deliberately zero imports from app.guardrails.service/catalog/
editable_config, no coupling to NeMo.
"""

import base64
import binascii
import dataclasses
import enum
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.atomic_write import atomic_write_text
from app.core.audit import record_entry as record_audit_entry
from app.core.distributed_lock import admin_write_lock
from app.schemas.chat import ChatCompletionRequest, ChatMessage, TextContentPart

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "prompt_injection.yaml"
CONFIG_PATH_ENV_VAR = "OPENBOUNCER_PROMPT_INJECTION_CONFIG"

REDACTION_TOKEN = "[PROMPT_INJECTION]"

# Messages longer than this skip the evasion passes (typoglycemia/base64/
# hex/char-spaced) -- direct-pattern matching still runs. A perf guard
# against pathological input, not a correctness requirement.
_MAX_EVASION_SCAN_LENGTH = 20_000


class InjectionAction(str, enum.Enum):
    DISABLED = "disabled"
    FLAG = "flag"
    REDACT = "redact"
    BLOCK = "block"


# Used to pick the most-restrictive action when several categories/messages
# match with different configured actions -- block > redact > flag > disabled.
_ACTION_SEVERITY: dict[InjectionAction, int] = {
    InjectionAction.DISABLED: 0,
    InjectionAction.FLAG: 1,
    InjectionAction.REDACT: 2,
    InjectionAction.BLOCK: 3,
}


class InjectionScope(str, enum.Enum):
    ALL_MESSAGES = "all_messages"
    USER_MESSAGES_ONLY = "user_messages_only"


class InjectionCategory(str, enum.Enum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    MODE_ACTIVATION = "mode_activation"
    SYSTEM_OVERRIDE = "system_override"
    PROMPT_EXTRACTION = "prompt_extraction"
    ROLE_MANIPULATION = "role_manipulation"
    JAILBREAK_DAN = "jailbreak_dan"
    SAFETY_BYPASS = "safety_bypass"
    TAG_INJECTION = "tag_injection"
    CONTROL_TOKEN_INJECTION = "control_token_injection"


@dataclass(frozen=True)
class CategoryMatch:
    category: InjectionCategory
    pattern_name: str
    matched_text: str
    span: tuple[int, int]
    # "direct" | "typoglycemia" | "base64" | "hex" | "char_spaced"
    via: str
    # Which text segment of the message this span is relative to: None for
    # a plain string `content`, else the index into a list-of-parts
    # `content`'s TextContentPart entries. Needed so apply_action() can
    # redact each segment's own text using its own (segment-local) offsets
    # -- segments are never concatenated for scanning, precisely so spans
    # stay valid for redaction without an offset-mapping step.
    segment: int | None = None


@dataclass(frozen=True)
class ScanResult:
    action: InjectionAction
    matches: list[CategoryMatch]


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

_RAW_PATTERNS: dict[InjectionCategory, list[tuple[str, str]]] = {
    InjectionCategory.INSTRUCTION_OVERRIDE: [
        ("ignore_instructions", r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions?\b"),
        ("disregard_instructions", r"\bdisregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions?\b"),
        ("forget_instructions", r"\bforget\s+(?:all\s+|any\s+)?(?:your\s+)?(?:previous\s+)?instructions?\b"),
        ("ignore_rules", r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+rules\b"),
        ("override_previous_instructions", r"\boverride\s+(?:your\s+)?(?:previous\s+)?instructions?\b"),
    ],
    InjectionCategory.MODE_ACTIVATION: [
        ("developer_mode", r"\bdeveloper\s+mode\b"),
        ("special_mode", r"\b(?:enter|activate|enable)\s+(?:a\s+)?special\s+mode\b"),
        ("admin_mode", r"\badmin(?:istrator)?\s+mode\b"),
        ("god_mode", r"\bgod\s+mode\b"),
        ("unrestricted_mode", r"\bunrestricted\s+mode\b"),
    ],
    InjectionCategory.SYSTEM_OVERRIDE: [
        ("system_override_kw", r"\bsystem\s+override\b"),
        ("new_system_prompt", r"\byour\s+new\s+system\s+prompt\s+is\b"),
        ("overwrite_system_prompt", r"\boverwrite\s+(?:your\s+)?system\s+prompt\b"),
        ("system_prompt_is_now", r"\bsystem\s+prompt\s+is\s+now\b"),
    ],
    InjectionCategory.PROMPT_EXTRACTION: [
        ("reveal_prompt", r"\breveal\s+(?:your\s+)?(?:system\s+)?prompt\b"),
        ("show_prompt", r"\bshow\s+(?:me\s+)?(?:your\s+)?(?:system\s+)?prompt\b"),
        ("what_are_your_instructions", r"\bwhat\s+(?:are|is)\s+your\s+instructions?\b"),
        ("repeat_instructions", r"\brepeat\s+(?:your\s+)?(?:initial\s+|system\s+)?instructions?\b"),
        ("print_system_prompt", r"\bprint\s+(?:your\s+)?system\s+prompt\b"),
    ],
    InjectionCategory.ROLE_MANIPULATION: [
        ("remove_restrictions", r"\bremove\s+(?:all\s+)?(?:your\s+)?restrictions?\b"),
        ("act_unbound", r"\bact\s+(?:as\s+if\s+)?unbound\b"),
        ("pretend_different", r"\bpretend\s+(?:to\s+be|you\s+are)\s+(?:a\s+)?different\b"),
        ("no_longer_bound", r"\byou\s+are\s+no\s+longer\s+bound\b"),
        ("act_without_restrictions", r"\bact\s+without\s+(?:any\s+)?restrictions?\b"),
    ],
    InjectionCategory.JAILBREAK_DAN: [
        ("do_anything_now", r"\bdo\s+anything\s+now\b"),
        ("act_as_dan", r"\bact\s+as\s+DAN\b"),
        ("dan_mode", r"\bDAN\s+mode\b"),
        ("jailbreak_kw", r"\bjailbreak(?:s|ing|ed)?\s+(?:this|the)?\s*(?:ai|assistant|model)\b"),
        ("unlocked_persona", r"\bunlock(?:ed)?\s+(?:mode|persona)\b"),
    ],
    InjectionCategory.SAFETY_BYPASS: [
        ("bypass_safety", r"\bbypass\s+(?:the\s+)?safety\b"),
        ("disable_safety", r"\bdisable\s+(?:the\s+)?safety\b"),
        ("ignore_safety", r"\bignore\s+(?:the\s+)?safety\b"),
        ("bypass_filters", r"\bbypass\s+(?:the\s+)?(?:content\s+)?filters?\b"),
        ("without_ethical_restrictions", r"\bwithout\s+any\s+(?:ethical\s+)?restrictions?\b"),
    ],
    InjectionCategory.TAG_INJECTION: [
        ("fake_system_tag", r"<\s*system\s*>"),
        ("fake_assistant_tag", r"<\s*assistant\s*>"),
        ("fake_user_tag", r"<\s*user\s*>"),
        ("bracket_role_tag", r"\[\s*(?:system|assistant)\s*\]\s*:"),
    ],
    InjectionCategory.CONTROL_TOKEN_INJECTION: [
        # Generic <|...|> catches ChatML (<|im_start|>/<|im_end|>), Llama 3
        # (<|start_header_id|>/<|eot_id|>), and similar model-specific
        # control tokens without needing one named pattern per model family.
        ("pipe_control_token", r"<\|[a-zA-Z_]+\|>"),
        ("llama2_inst_token", r"\[/?INST\]"),
        ("gpt_endoftext_token", r"<\|endoftext\|>"),
    ],
}

_PATTERN_REGISTRY: dict[InjectionCategory, list[tuple[str, "re.Pattern[str]"]]] = {
    category: [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in patterns]
    for category, patterns in _RAW_PATTERNS.items()
}

# Shared by all three evasion countermeasures: a fixed keyword list (matches
# OpenRouter's own documented set) mapped to the category it implies.
_EVASION_KEYWORD_CATEGORY: dict[str, InjectionCategory] = {
    "ignore": InjectionCategory.INSTRUCTION_OVERRIDE,
    "disregard": InjectionCategory.INSTRUCTION_OVERRIDE,
    "instructions": InjectionCategory.INSTRUCTION_OVERRIDE,
    "bypass": InjectionCategory.SAFETY_BYPASS,
    "override": InjectionCategory.SAFETY_BYPASS,
    "reveal": InjectionCategory.PROMPT_EXTRACTION,
    "system": InjectionCategory.SYSTEM_OVERRIDE,
    "prompt": InjectionCategory.PROMPT_EXTRACTION,
    "delete": InjectionCategory.SAFETY_BYPASS,
}

_WORD_RE = re.compile(r"[A-Za-z]+")
# A run of 3+ single-letter "words" separated by whitespace, e.g. "i g n o r e".
_CHAR_SPACED_RUN_RE = re.compile(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b")
_BASE64_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
# Also matches space-separated hex byte pairs ("69 67 6e 6f 72 65").
_HEX_CANDIDATE_RE = re.compile(r"(?:[0-9a-fA-F]{2}\s?){8,}")


def _direct_matches(text: str) -> list[CategoryMatch]:
    matches = []
    for category, patterns in _PATTERN_REGISTRY.items():
        for name, pattern in patterns:
            for m in pattern.finditer(text):
                matches.append(CategoryMatch(category, name, m.group(0), m.span(), "direct"))
    return matches


def _typoglycemia_matches(text: str) -> list[CategoryMatch]:
    """A word is flagged if it has the same first/last letter and the same
    multiset of middle letters as a keyword, but isn't the keyword itself
    -- the literal definition of typoglycemia scrambling (e.g. "ignroe"
    matches "ignore"; the exact word "ignore" does not, since that's not
    evasion -- it's caught by the direct-pattern phrases instead, if it's
    part of one)."""
    matches = []
    for m in _WORD_RE.finditer(text):
        word = m.group(0)
        word_lower = word.lower()
        for keyword, category in _EVASION_KEYWORD_CATEGORY.items():
            if len(word_lower) != len(keyword) or word_lower == keyword:
                continue
            if word_lower[0] != keyword[0] or word_lower[-1] != keyword[-1]:
                continue
            if sorted(word_lower[1:-1]) == sorted(keyword[1:-1]):
                matches.append(
                    CategoryMatch(category, f"typoglycemia_{keyword}", word, m.span(), "typoglycemia")
                )
                break
    return matches


def _keyword_hits_in_decoded(decoded_text: str, span: tuple[int, int], original_substring: str, via: str) -> list[CategoryMatch]:
    lowered = decoded_text.lower()
    hits = []
    for keyword, category in _EVASION_KEYWORD_CATEGORY.items():
        if re.search(rf"\b{re.escape(keyword)}\b", lowered):
            hits.append(CategoryMatch(category, f"{via}_decoded_{keyword}", original_substring, span, via))
    return hits


def _decoded_keyword_matches(text: str) -> list[CategoryMatch]:
    """Decodes base64/hex-looking substrings and keyword-scans the decoded
    text -- matching OpenRouter's own documented "decode then keyword-scan"
    approach, not a full pattern re-scan. Spans always refer to the
    *original* (still-encoded) substring, so redaction can splice it out
    without needing to map decoded-text offsets back."""
    matches = []
    for m in _BASE64_CANDIDATE_RE.finditer(text):
        candidate = m.group(0)
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError):
            continue
        decoded_text = decoded.decode("utf-8", errors="ignore")
        if decoded_text.strip():
            matches.extend(_keyword_hits_in_decoded(decoded_text, m.span(), candidate, "base64"))
    for m in _HEX_CANDIDATE_RE.finditer(text):
        compact = m.group(0).replace(" ", "")
        if len(compact) % 2 != 0:
            continue
        try:
            decoded = bytes.fromhex(compact)
        except ValueError:
            continue
        decoded_text = decoded.decode("utf-8", errors="ignore")
        if decoded_text.strip():
            matches.extend(_keyword_hits_in_decoded(decoded_text, m.span(), m.group(0), "hex"))
    return matches


def _char_spaced_matches(text: str) -> list[CategoryMatch]:
    """Collapses a single run of space-separated single letters (e.g.
    "i g n o r e" -> "ignore") and checks it against the same fixed
    keyword list the other evasion passes use. Deliberately scoped to a
    single collapsed run rather than reconstructing a full normalized
    text and re-running the whole phrase-pattern registry against it --
    that would require mapping matches back to original-text offsets
    across a length-changing transform for redaction, which isn't worth
    the complexity for what's fundamentally the same keyword-evasion
    technique as the base64/hex passes above."""
    matches = []
    for m in _CHAR_SPACED_RUN_RE.finditer(text):
        collapsed = re.sub(r"\s+", "", m.group(0)).lower()
        category = _EVASION_KEYWORD_CATEGORY.get(collapsed)
        if category is not None:
            matches.append(
                CategoryMatch(category, f"char_spaced_{collapsed}", m.group(0), m.span(), "char_spaced")
            )
    return matches


def _is_allow_listed(match: CategoryMatch, allow_list: list[str]) -> bool:
    matched_lower = match.matched_text.lower()
    return any(
        matched_lower in phrase.lower() or phrase.lower() in matched_lower
        for phrase in allow_list
        if phrase
    )


def scan_message(text: str, config: "PromptInjectionConfig") -> list[CategoryMatch]:
    """Scans a single string (one message, or one content part of a
    message) for prompt-injection matches, honoring config.detect_evasions
    and config.allow_list. Does not know about scope or per-request
    actions -- that's scan_request()'s job."""
    if not text:
        return []

    matches = _direct_matches(text)
    if config.detect_evasions and len(text) <= _MAX_EVASION_SCAN_LENGTH:
        matches.extend(_typoglycemia_matches(text))
        matches.extend(_decoded_keyword_matches(text))
        matches.extend(_char_spaced_matches(text))

    matches = [
        m
        for m in matches
        if config.categories.get(m.category, InjectionAction.FLAG) != InjectionAction.DISABLED
    ]
    matches = [m for m in matches if not _is_allow_listed(m, config.allow_list)]

    seen: set[tuple[InjectionCategory, str, tuple[int, int]]] = set()
    deduped = []
    for m in matches:
        key = (m.category, m.pattern_name, m.span)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)
    return deduped


def _text_segments(message: ChatMessage) -> list[tuple[int | None, str]]:
    """(segment, text) pairs -- segment is None for a plain string
    `content`, else the part's index in a list-of-parts `content`."""
    if isinstance(message.content, str):
        return [(None, message.content)]
    return [(i, part.text) for i, part in enumerate(message.content) if isinstance(part, TextContentPart)]


def scan_request(request: ChatCompletionRequest, config: "PromptInjectionConfig") -> dict[int, ScanResult]:
    """Returns message-index -> ScanResult for every message with >=1
    match, filtered by config.scope. An empty dict means nothing matched
    -- pass the request through unchanged."""
    results: dict[int, ScanResult] = {}
    for msg_index, message in enumerate(request.messages):
        if config.scope is InjectionScope.USER_MESSAGES_ONLY and message.role != "user":
            continue

        message_matches: list[CategoryMatch] = []
        for segment, text in _text_segments(message):
            for m in scan_message(text, config):
                message_matches.append(dataclasses.replace(m, segment=segment))

        if not message_matches:
            continue

        results[msg_index] = ScanResult(
            action=resolve_overall_action(message_matches, config), matches=message_matches
        )
    return results


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _redact_text(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    pieces = []
    cursor = 0
    for start, end in _merge_spans(spans):
        pieces.append(text[cursor:start])
        pieces.append(REDACTION_TOKEN)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _redact_message(message: ChatMessage, matches: list[CategoryMatch]) -> ChatMessage:
    by_segment: dict[int | None, list[tuple[int, int]]] = {}
    for m in matches:
        by_segment.setdefault(m.segment, []).append(m.span)

    if isinstance(message.content, str):
        return message.model_copy(update={"content": _redact_text(message.content, by_segment.get(None, []))})

    new_parts = list(message.content)
    for i, part in enumerate(new_parts):
        if isinstance(part, TextContentPart) and i in by_segment:
            new_parts[i] = part.model_copy(update={"text": _redact_text(part.text, by_segment[i])})
    return message.model_copy(update={"content": new_parts})


def apply_action(
    request: ChatCompletionRequest, results: dict[int, ScanResult]
) -> tuple[ChatCompletionRequest, InjectionAction, list[CategoryMatch]]:
    """Combines per-message results into one overall decision (most
    restrictive wins). For REDACT, returns a *new* request with matched
    spans in REDACT-actioned messages replaced by REDACTION_TOKEN --
    messages whose own action is only FLAG are left untouched even if the
    overall decision is REDACT. Never mutates the input request (Pydantic
    models are used immutably elsewhere in this codebase, e.g.
    response.model_copy(update=...) in app/api/routes/chat.py)."""
    if not results:
        return request, InjectionAction.DISABLED, []

    overall_action = max((r.action for r in results.values()), key=lambda a: _ACTION_SEVERITY[a])
    all_matches = [m for r in results.values() for m in r.matches]

    if overall_action is not InjectionAction.REDACT:
        return request, overall_action, all_matches

    new_messages = list(request.messages)
    for msg_index, result in results.items():
        if result.action is InjectionAction.REDACT:
            new_messages[msg_index] = _redact_message(new_messages[msg_index], result.matches)
    return request.model_copy(update={"messages": new_messages}), overall_action, all_matches


# ---------------------------------------------------------------------------
# Config: file-backed, lru_cache'd, hot-reloadable -- same shape as
# app.auth.keys.KeyStore/get_key_store()/_persist_config().
# ---------------------------------------------------------------------------


class PromptInjectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    scope: InjectionScope = InjectionScope.USER_MESSAGES_ONLY
    detect_evasions: bool = True
    allow_list: list[str] = Field(default_factory=list)
    categories: dict[InjectionCategory, InjectionAction] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fill_default_categories(self) -> "PromptInjectionConfig":
        # A category omitted from the YAML/PATCH payload defaults to FLAG
        # (log-only), not DISABLED -- an admin has to opt out explicitly,
        # matching OpenRouter's own "test in flag mode first" guidance.
        # A model_validator (not a field_validator on "categories") is
        # required here: field_validators don't run when a field is
        # entirely absent from the input and falls back to its
        # default_factory (categories: {} when the whole "categories" key
        # is missing from the YAML/PATCH payload, e.g. `{"enabled": true}`
        # with no categories block at all) -- only an "after" model
        # validator is guaranteed to run on every construction regardless
        # of which fields were actually supplied.
        for category in InjectionCategory:
            self.categories.setdefault(category, InjectionAction.FLAG)
        return self


def parse_prompt_injection_config(raw_yaml: str) -> PromptInjectionConfig:
    data = yaml.safe_load(raw_yaml) or {}
    return PromptInjectionConfig(**data)


def _resolve_config_path() -> Path:
    explicit = os.environ.get(CONFIG_PATH_ENV_VAR)
    return Path(explicit) if explicit else DEFAULT_CONFIG_PATH


def load_prompt_injection_config() -> PromptInjectionConfig:
    path = _resolve_config_path()
    if not path.exists():
        # No config file: default to disabled, same "absent = safe default"
        # posture as app.auth.keys.load_key_store()'s empty key store.
        return PromptInjectionConfig()
    return parse_prompt_injection_config(path.read_text())


@lru_cache
def get_prompt_injection_config() -> PromptInjectionConfig:
    return load_prompt_injection_config()


# Serializes read-modify-write across concurrent admin requests. Coordinates
# across gateway replicas too, for real, when REDIS_URL is set -- see
# app.core.distributed_lock's module docstring (and app.auth.keys' matching
# _WRITE_LOCK_NAME).
_WRITE_LOCK_NAME = "prompt_injection"


async def _persist_config(config: PromptInjectionConfig, *, actor_key_id: str, summary: str) -> None:
    path = _resolve_config_path()
    before = (
        path.read_text()
        if path.exists()
        else yaml.safe_dump(PromptInjectionConfig().model_dump(mode="json"), sort_keys=False)
    )
    new_yaml = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    atomic_write_text(path, new_yaml)
    # Recorded only after the write above succeeds -- see app.core.audit's
    # module docstring for why.
    await record_audit_entry(
        actor_key_id=actor_key_id,
        resource_type="prompt_injection",
        resource_id=None,
        action="update_prompt_injection_config",
        summary=summary,
        path=path,
        before=before,
        after=new_yaml,
    )
    get_prompt_injection_config.cache_clear()


def resolve_overall_action(matches: list[CategoryMatch], config: PromptInjectionConfig) -> InjectionAction:
    """The single decision a caller (chat.py's per-request scan, or the
    admin "test your patterns" endpoint) should act on for a set of
    matches: the most-restrictive configured action across every match's
    category, or DISABLED (nothing to do) if there are no matches."""
    if not matches:
        return InjectionAction.DISABLED
    return max(
        (config.categories.get(m.category, InjectionAction.FLAG) for m in matches),
        key=lambda a: _ACTION_SEVERITY[a],
    )


def redact_preview(text: str, matches: list[CategoryMatch]) -> str:
    """Like _redact_message, but for a single plain string with no
    message/content-part structure -- used by the admin "test your
    patterns" preview endpoint, which takes raw sample text rather than a
    full ChatCompletionRequest."""
    return _redact_text(text, [m.span for m in matches])


async def update_prompt_injection_config(
    *,
    actor_key_id: str,
    enabled: bool | None = None,
    scope: InjectionScope | None = None,
    detect_evasions: bool | None = None,
    allow_list: list[str] | None = None,
    categories: dict[InjectionCategory, InjectionAction] | None = None,
) -> PromptInjectionConfig:
    """Persists a partial update and invalidates get_prompt_injection_config()'s
    cache so it's live on the very next request -- no restart needed.

    `categories`, if given, is merged key-by-key into the current on-disk
    categories (not a full replacement) -- computed here, inside the lock,
    against a freshly-loaded snapshot, so two concurrent PATCHes touching
    different categories can't race and silently clobber each other.
    Every other field is a full replacement, same as
    app.auth.keys.update_key_fields.
    """
    async with admin_write_lock(_WRITE_LOCK_NAME):
        current = load_prompt_injection_config()
        updates: dict[str, object] = {}
        if enabled is not None:
            updates["enabled"] = enabled
        if scope is not None:
            updates["scope"] = scope
        if detect_evasions is not None:
            updates["detect_evasions"] = detect_evasions
        if allow_list is not None:
            updates["allow_list"] = allow_list
        if categories is not None:
            merged_categories = dict(current.categories)
            merged_categories.update(categories)
            updates["categories"] = merged_categories

        updated = current.model_copy(update=updates)
        await _persist_config(
            updated,
            actor_key_id=actor_key_id,
            summary=f"Updated prompt-injection config: fields={sorted(updates.keys())}",
        )
        return updated
