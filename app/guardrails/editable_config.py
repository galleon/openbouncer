"""Structured (non-YAML) editing of the bundled guardrails presets' bullet
lists (policy rules / allowed topics), for the admin API.

Scope is deliberately narrow: a hardcoded manifest naming exactly the 4
config_ids and sections that exist today (see EDITABLE_CONFIG_MANIFEST), not
a generic "edit any config.yml" engine. Editing happens via targeted string
splicing on the raw file text rather than yaml.safe_load/safe_dump, because
the editable region lives inside a `content: |` literal block scalar
containing Jinja references ({{ user_input }} / {{ bot_response }}) and a
fixed Question/Answer scaffold -- PyYAML's dumper does not reliably
preserve literal-block style or exact formatting on round-trip, which risks
mangling that scaffold. Every function here fails loudly
(GuardrailsConfigParseError) rather than silently writing something wrong.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.atomic_write import atomic_write_text
from app.core.audit import record_entry as record_audit_entry
from app.guardrails.service import GuardrailsService

# Optional dependency (see pyproject.toml's "nemo" extra) -- only needed for
# the post-write validation step below (RailsConfig.from_path), which
# confirms an edited preset still loads before it's ever persisted for
# real. See app.guardrails.service's identical import-guard comment for why
# this can't be a hard top-level dependency.
try:
    from nemoguardrails import RailsConfig

    _NEMOGUARDRAILS_IMPORT_ERROR: Exception | None = None
except ImportError as exc:
    RailsConfig = None  # type: ignore[assignment,misc]
    _NEMOGUARDRAILS_IMPORT_ERROR = exc

_MAX_ITEMS = 100
_MAX_ITEM_LENGTH = 500

_BULLET_LINE = re.compile(r"^([ \t]*)-\s?(.*)$")


class GuardrailsConfigParseError(RuntimeError):
    """Raised when a config.yml's actual text doesn't match the shape the
    manifest expects, or a write's own round-trip/reload check fails --
    signals "don't trust this write", never partially applied."""


@dataclass(frozen=True)
class EditableSection:
    field: str  # key used in the admin API request/response
    label: str  # UI-facing label
    task_anchor: str  # exact substring identifying the right `task:` entry
    header_line: str  # exact line immediately preceding the bullet list


# self_check_input_output has two `task:` blocks in one file (input, then
# output) -- each section's search starts where the previous one's ended,
# so task_anchor + ordering together disambiguate which content block a
# section belongs to without needing to parse the surrounding YAML at all.
EDITABLE_CONFIG_MANIFEST: dict[str, list[EditableSection]] = {
    "self_check_input": [
        EditableSection(
            "policy",
            "Policy rules",
            "task: self_check_input",
            "Company policy for the user messages:",
        ),
    ],
    "self_check_output": [
        EditableSection(
            "policy",
            "Policy rules",
            "task: self_check_output",
            "Company policy for the bot:",
        ),
    ],
    "self_check_input_output": [
        EditableSection(
            "input_policy",
            "Policy rules (user messages)",
            "task: self_check_input",
            "Company policy for the user messages:",
        ),
        EditableSection(
            "output_policy",
            "Policy rules (bot messages)",
            "task: self_check_output",
            "Company policy for the bot:",
        ),
    ],
    "topic_safety": [
        EditableSection(
            "allowed_topics",
            "Allowed topics",
            # Verified against the real file: the task value is unquoted
            # YAML (`task: topic_safety_check_input $model=topic_guard`).
            "task: topic_safety_check_input $model=topic_guard",
            "Allowed topics:",
        ),
    ],
    "jailbreak_input": [
        EditableSection(
            "policy",
            "Policy rules",
            "task: self_check_input",
            "A message should be blocked if it does any of the following:",
        ),
    ],
    "topic_blocklist": [
        EditableSection(
            "blocked_topics",
            "Blocked topics",
            "task: topic_safety_check_input $model=topic_guard",
            "Blocked topics:",
        ),
    ],
    # pii_regex's two `patterns:` lists are plain YAML block-list syntax
    # (not inside a `content: |` prompt), but the same bullet-splicing
    # machinery works on them unchanged -- `- item` lines are `- item`
    # lines either way. task_anchor for each section only needs to land
    # *before* the right `patterns:` occurrence, not exactly on it: since
    # cursor always advances past the previously-found section, "input:"
    # resolves to rails.input (the first "input:" in the file) and then
    # the first "patterns:" after it is unambiguously
    # regex_detection.input.patterns; "output:" (searched from where the
    # input section's bullets ended) then resolves to
    # regex_detection.output, whose "patterns:" is next. Verified against
    # the real file in guardrails_configs/pii_regex/config.yml.
    "pii_regex": [
        EditableSection(
            "input_patterns",
            "Blocked patterns -- input (regex)",
            "input:",
            "patterns:",
        ),
        EditableSection(
            "output_patterns",
            "Blocked patterns -- output (regex)",
            "output:",
            "patterns:",
        ),
    ],
}


def _find_bullets(
    text: str, task_anchor: str, header_line: str, start: int = 0
) -> tuple[list[str], int, int, str]:
    """Locate the bullet list immediately following header_line, itself
    searched for after task_anchor (searched from `start`). Returns
    (bullets, block_start, block_end, indent) -- block_start/end delimit
    exactly the bullet lines, nothing else, so a caller can splice a
    replacement in without touching anything around it.
    """
    task_pos = text.find(task_anchor, start)
    if task_pos == -1:
        raise GuardrailsConfigParseError(f"Could not find {task_anchor!r} in config file")

    header_pos = text.find(header_line, task_pos)
    if header_pos == -1:
        raise GuardrailsConfigParseError(
            f"Could not find header {header_line!r} after {task_anchor!r}"
        )

    nl = text.find("\n", header_pos)
    after_header = nl + 1 if nl != -1 else len(text)

    bullets: list[str] = []
    indent: str | None = None
    pos = after_header
    while pos < len(text):
        nl = text.find("\n", pos)
        line_end = nl + 1 if nl != -1 else len(text)
        line = text[pos:line_end]
        stripped = line[:-1] if line.endswith("\n") else line
        m = _BULLET_LINE.match(stripped)
        if not m:
            break
        if indent is None:
            indent = m.group(1)
        elif m.group(1) != indent:
            # Indentation changed -- treat as end of this list rather than
            # guessing whether it's still part of the same bullet block.
            break
        bullets.append(m.group(2).rstrip())
        pos = line_end

    if not bullets:
        raise GuardrailsConfigParseError(f"No bullet list found under {header_line!r}")

    return bullets, after_header, pos, indent or ""


def _validate_items(items: list[str]) -> list[str]:
    cleaned = [item.strip() for item in items]
    if not cleaned:
        raise ValueError("At least one item is required.")
    if len(cleaned) > _MAX_ITEMS:
        raise ValueError(f"Too many items (max {_MAX_ITEMS}).")
    for item in cleaned:
        if not item:
            raise ValueError("Items must not be blank.")
        if "\n" in item or "\r" in item:
            raise ValueError("Items must be single-line.")
        if len(item) > _MAX_ITEM_LENGTH:
            raise ValueError(f"Item too long (max {_MAX_ITEM_LENGTH} characters).")
    return cleaned


def _splice_bullets(
    text: str, start: int, end: int, indent: str, items: list[str]
) -> tuple[str, int]:
    new_block = "".join(f"{indent}- {item}\n" for item in items)
    new_text = text[:start] + new_block + text[end:]
    return new_text, start + len(new_block)


def read_editable_sections(config_dir: Path, config_id: str) -> dict[str, list[str]]:
    """Raises KeyError if config_id isn't in the manifest (not editable),
    GuardrailsConfigParseError if the file's actual shape doesn't match
    what the manifest expects."""
    sections = EDITABLE_CONFIG_MANIFEST.get(config_id)
    if sections is None:
        raise KeyError(config_id)

    text = (config_dir / "config.yml").read_text()
    result: dict[str, list[str]] = {}
    cursor = 0
    for section in sections:
        bullets, _start, end, _indent = _find_bullets(
            text, section.task_anchor, section.header_line, start=cursor
        )
        result[section.field] = bullets
        cursor = end
    return result


def write_editable_sections(
    config_dir: Path,
    config_id: str,
    updates: dict[str, list[str]],
    guardrails_service: GuardrailsService,
    *,
    actor_key_id: str,
) -> dict[str, list[str]]:
    """Validates, splices, re-reads to confirm the round-trip, writes
    atomically, confirms the new file still loads via RailsConfig (rolling
    back on failure), records an audit log entry, then invalidates the
    cache entry for config_id so the change is live on the next request.
    Raises KeyError if config_id isn't editable, ValueError on bad input,
    GuardrailsConfigParseError if the file's shape doesn't match or the
    post-write reload fails.

    Unlike app.auth.keys/app.guardrails.prompt_injection, this function
    doesn't take an explicit asyncio.Lock -- it doesn't need one, since
    (like app.core.audit.record_entry) its entire body has no `await`
    point, so two concurrent calls in this process can never interleave
    mid-write regardless. That guarantee is process-local, though -- see
    the README's "Multi-replica deployments" section for why it doesn't
    extend to two gateway replicas writing the same file at once.
    """
    sections = EDITABLE_CONFIG_MANIFEST.get(config_id)
    if sections is None:
        raise KeyError(config_id)

    expected_fields = {s.field for s in sections}
    if set(updates) != expected_fields:
        raise ValueError(
            f"Expected exactly fields {sorted(expected_fields)}, got {sorted(updates)}"
        )

    validated = {field: _validate_items(items) for field, items in updates.items()}

    config_path = config_dir / "config.yml"
    original_text = config_path.read_text()
    text = original_text
    cursor = 0
    for section in sections:
        _bullets, start, end, indent = _find_bullets(
            text, section.task_anchor, section.header_line, start=cursor
        )
        text, cursor = _splice_bullets(text, start, end, indent, validated[section.field])

    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GuardrailsConfigParseError(f"Splice produced invalid YAML: {exc}") from exc

    reread: dict[str, list[str]] = {}
    reread_cursor = 0
    for section in sections:
        bullets, _start, end, _indent = _find_bullets(
            text, section.task_anchor, section.header_line, start=reread_cursor
        )
        reread[section.field] = bullets
        reread_cursor = end
    if reread != validated:
        raise GuardrailsConfigParseError(
            "Internal error: splice round-trip mismatch, aborting write."
        )

    if RailsConfig is None:
        # Known upfront, before anything is written -- no partial/unvalidated
        # write to roll back, unlike the reload failure below.
        raise GuardrailsConfigParseError(
            "Cannot validate this edit: the optional 'nemoguardrails' package "
            f"isn't installed in this deployment ({_NEMOGUARDRAILS_IMPORT_ERROR}). "
            "Install it with `uv sync --extra nemo` to enable editing nemo_library "
            "guardrails configs."
        )

    atomic_write_text(config_path, text)

    try:
        RailsConfig.from_path(str(config_dir))
    except Exception as exc:
        atomic_write_text(config_path, original_text)
        raise GuardrailsConfigParseError(
            f"New config failed to load, rolled back: {exc}"
        ) from exc

    # Recorded only after the write above is confirmed valid (reload
    # succeeded) -- see app.core.audit's module docstring for why.
    record_audit_entry(
        actor_key_id=actor_key_id,
        resource_type="guardrails_config",
        resource_id=config_id,
        action="update_guardrails_config",
        summary=f"Updated guardrails config '{config_id}': sections={sorted(updates.keys())}",
        path=config_path,
        before=original_text,
        after=text,
    )
    guardrails_service.invalidate(config_id)
    return reread
