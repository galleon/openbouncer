"""Append-only, per-request log of individual guardrail decisions -- the
observability counterpart to app.core.audit (which logs *admin config
writes*, not requests). Every prompt-injection or output-leak match that
isn't `disabled` gets one entry here, regardless of which action was
actually applied (flag/redact/block) -- so an operator can answer "which
key triggered this, on what, when" after the fact, something the existing
Prometheus counters (openbouncer_prompt_injection_*_total /
openbouncer_output_leak_*_total) can't do: they show *how many*, this
shows *which ones*. See `GET /api/admin/guardrail-events` in
app/api/routes/admin.py, surfaced in the Activity dashboard.

Same JSONL-append-plus-trim shape as app.core.audit (see that module's
docstring for the reasoning: no separate snapshot store, cheap to check on
every write, single-writer/single-process assumption, same "Multi-replica
deployments" caveat as the README documents for the audit log). Kept as a
separate file and a separate module from app.core.audit on purpose: these
are two different logs with different audiences (admin config changes vs.
per-request guardrail activity), different volume (this one is written on
the hot request path, potentially once per flagged request, not just on
rare admin writes), and no revert semantics -- reusing audit.py's
AuditEntry/record_entry would conflate "what an admin changed" with "what
a guardrail did to someone else's traffic."

Snippet privacy: `snippet` never contains a *raw* output-leak match (an
actual email/SSN/API key) -- callers pass the category's own redaction
placeholder (e.g. "[EMAIL]") instead, so this log can't become a second
copy of the very data the output-leak guardrail exists to keep from
leaking. Prompt-injection matches are the adversarial *input* phrase
itself (e.g. "ignore all previous instructions"), not PII, so those are
logged as-is by default -- that's the content a reviewer actually needs to
see to tell a real attack from a false positive.

That default is a real trade-off, not a free lunch: an attack phrase can
still co-occur with real user content in the same message (a prompt
containing both a genuine question and an injection attempt), and a
deployment handling classified/regulated input may not be able to accept
*any* raw request content landing in a log at all, even a log this
narrowly scoped. Setting `OPENBOUNCER_LOG_PROMPT_CONTENT=false` (default:
enabled, matching every existing "log the content" behavior in this
module) makes `record_event()` replace `snippet` with a bracketed
category placeholder for *every* event, prompt-injection included --
category/pattern_name/action/via/model/key_id/request_id are all
classification metadata, never content, so investigation still works, just
without ever writing the matched text itself to disk.
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.atomic_write import atomic_write_text

DEFAULT_EVENTS_PATH = Path(__file__).resolve().parents[2] / "config" / "guardrail_events.jsonl"
EVENTS_PATH_ENV_VAR = "OPENBOUNCER_GUARDRAIL_EVENTS_PATH"

# Bounded, same reasoning as app.core.audit._MAX_LISTED_ENTRIES.
_MAX_LISTED_ENTRIES = 200

# Higher default than app.core.audit.DEFAULT_MAX_ENTRIES (5000): this log
# can get one entry per *flagged* request (the default action for most
# categories), not just rare admin writes, so it needs more headroom
# before trimming starts discarding recent history.
DEFAULT_MAX_ENTRIES = 20_000
MAX_ENTRIES_ENV_VAR = "OPENBOUNCER_GUARDRAIL_EVENTS_MAX_ENTRIES"

# Truncation cap on `snippet` -- a perf/size guard against a pathologically
# long matched span (e.g. a huge base64 blob), not a privacy measure (see
# the module docstring for the actual privacy mechanism).
_MAX_SNIPPET_LENGTH = 300

# See the module docstring's second paragraph. Enabled (log content) by
# default -- flipping it is an opt-in, deployment-level privacy posture,
# same "existing behavior is the default" discipline as every other env
# var in this codebase.
LOG_PROMPT_CONTENT_ENV_VAR = "OPENBOUNCER_LOG_PROMPT_CONTENT"


def _log_prompt_content() -> bool:
    raw = os.environ.get(LOG_PROMPT_CONTENT_ENV_VAR)
    return raw is None or raw.strip().lower() not in ("false", "0", "no")


def _max_entries() -> int:
    raw = os.environ.get(MAX_ENTRIES_ENV_VAR)
    if not raw:
        return DEFAULT_MAX_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_ENTRIES
    return value if value > 0 else DEFAULT_MAX_ENTRIES


@dataclass(frozen=True)
class GuardrailEvent:
    id: str
    timestamp: str
    request_id: str | None
    key_id: str
    # "prompt_injection" | "output_leak".
    guardrail: str
    model: str
    category: str
    pattern_name: str
    # "flag" | "redact" | "block" -- never "disabled" (callers don't
    # record disabled-category matches at all).
    action: str
    # Detection path for prompt-injection matches ("direct" / "typoglycemia"
    # / "base64" / "hex" / "char_spaced"); None for output-leak matches,
    # which have no equivalent concept.
    via: str | None
    # See the module docstring's "Snippet privacy" paragraph.
    snippet: str


def _events_path() -> Path:
    explicit = os.environ.get(EVENTS_PATH_ENV_VAR)
    return Path(explicit) if explicit else DEFAULT_EVENTS_PATH


def record_event(
    *,
    request_id: str | None,
    key_id: str,
    guardrail: str,
    model: str,
    category: str,
    pattern_name: str,
    action: str,
    via: str | None,
    snippet: str,
) -> GuardrailEvent:
    """Appends one entry. Plain synchronous file append with no `await` in
    between construction and the write -- same "safe without an explicit
    lock" reasoning as app.core.audit.record_entry (nothing else can
    interleave mid-call within a single asyncio event loop).

    Snippet redaction is enforced here, centrally, rather than left to each
    caller in app/api/routes/chat.py to remember -- this is the one place
    every event passes through regardless of which guardrail produced it,
    so it's the one place a zero-retention policy can't be accidentally
    bypassed by a new call site forgetting to check it.
    """
    effective_snippet = snippet if _log_prompt_content() else f"[{category}]"
    event = GuardrailEvent(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc).isoformat(),
        request_id=request_id,
        key_id=key_id,
        guardrail=guardrail,
        model=model,
        category=category,
        pattern_name=pattern_name,
        action=action,
        via=via,
        snippet=effective_snippet[:_MAX_SNIPPET_LENGTH],
    )
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(asdict(event)) + "\n")
    _trim_if_needed(path)
    return event


def _trim_if_needed(path: Path) -> None:
    """Same shape as app.core.audit._trim_if_needed -- see that function's
    docstring."""
    max_entries = _max_entries()
    lines = path.read_text().splitlines()
    if len(lines) <= max_entries:
        return
    trimmed = "\n".join(lines[-max_entries:]) + "\n"
    atomic_write_text(path, trimmed)


def _read_all_events() -> list[GuardrailEvent]:
    path = _events_path()
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        events.append(GuardrailEvent(**json.loads(line)))
    return events


def list_events(
    *,
    limit: int = 50,
    key_id: str | None = None,
    guardrail: str | None = None,
    action: str | None = None,
) -> list[GuardrailEvent]:
    """Most-recent-first, capped at min(limit, _MAX_LISTED_ENTRIES), with
    optional exact-match filters -- the "filterable table" the Activity
    dashboard renders this as. Filtering happens in Python over the whole
    file rather than as a smarter on-disk index: simple, and fine at the
    size this log actually reaches (DEFAULT_MAX_ENTRIES caps it), same
    trade-off app.core.audit.list_entries already accepts.
    """
    events = _read_all_events()
    events.reverse()
    if key_id is not None:
        events = [e for e in events if e.key_id == key_id]
    if guardrail is not None:
        events = [e for e in events if e.guardrail == guardrail]
    if action is not None:
        events = [e for e in events if e.action == action]
    return events[: min(limit, _MAX_LISTED_ENTRIES)]
