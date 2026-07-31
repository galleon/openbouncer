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
from nemoguardrails import RailsConfig

from app.core.atomic_write import atomic_write_text
from app.guardrails.service import GuardrailsService

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
) -> dict[str, list[str]]:
    """Validates, splices, re-reads to confirm the round-trip, writes
    atomically, confirms the new file still loads via RailsConfig (rolling
    back on failure), then invalidates the cache entry for config_id so the
    change is live on the next request. Raises KeyError if config_id isn't
    editable, ValueError on bad input, GuardrailsConfigParseError if the
    file's shape doesn't match or the post-write reload fails.
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

    atomic_write_text(config_path, text)

    try:
        RailsConfig.from_path(str(config_dir))
    except Exception as exc:
        atomic_write_text(config_path, original_text)
        raise GuardrailsConfigParseError(
            f"New config failed to load, rolled back: {exc}"
        ) from exc

    guardrails_service.invalidate(config_id)
    return reread
