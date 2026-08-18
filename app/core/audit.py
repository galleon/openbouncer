"""Append-only audit log for admin config writes (key lifecycle, guardrails
config edits, prompt-injection config edits), plus revert support -- see
the `/api/admin/audit-log*` endpoints in app/api/routes/admin.py.

Every admin persistence function that writes a config file (app.auth.keys,
app.guardrails.editable_config, app.guardrails.prompt_injection) calls
record_entry() right after that write succeeds, capturing the file's exact
*prior* text as `before`. That's the entire revert mechanism: reverting an
entry just atomic_write_texts its `before` back to `path` -- no separate
snapshot store to keep in sync.

Stored as JSONL, one object per line, appended to (never rewritten) -- a
revert is itself a new appended entry (action="revert"), so the log stays
a true history rather than something that can be edited after the fact.
Nothing is cached in-process: list/get re-read the file each call, which
is fine at the size an admin action log actually reaches (this is not a
high-frequency, request-path log -- see app.core.logging_middleware for
that).

Single-writer assumption: like the config files it describes, this log
assumes one process (or several processes sharing a filesystem, and even
then only safely for appends -- see the README's "Multi-replica
deployments" section) owns OPENBOUNCER_AUDIT_LOG_PATH. It is not
distributed storage, and record_entry()'s append+trim isn't safe against
another *process* appending or trimming at the same instant (only against
other coroutines within the same process -- see record_entry()'s
docstring).
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.atomic_write import atomic_write_text

DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parents[2] / "config" / "audit_log.jsonl"
AUDIT_LOG_PATH_ENV_VAR = "OPENBOUNCER_AUDIT_LOG_PATH"

# A hard cap on what a single list_entries() call will return, regardless
# of the caller-requested limit -- protects against a very large log ever
# being fully serialized into one response.
_MAX_LISTED_ENTRIES = 200

# Retention: the log is append-only with no external rotation, so without
# a cap it grows forever over the life of a long-running deployment.
# record_entry() trims the oldest entries once the log exceeds this count
# -- see _trim_if_needed(). Entries that age out this way can no longer be
# fetched (get_entry) or reverted (revert_entry); this is a deliberate
# trade-off (bounded disk use) documented in the README's "Audit log &
# revert" section, not a bug.
DEFAULT_MAX_ENTRIES = 5000
MAX_ENTRIES_ENV_VAR = "OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES"


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
class AuditEntry:
    id: str
    timestamp: str
    actor_key_id: str
    # "api_keys" | "guardrails_config" | "prompt_injection"
    resource_type: str
    # Only meaningful for "guardrails_config" (the config_id) -- None for
    # "api_keys"/"prompt_injection", which are each a single file.
    resource_id: str | None
    action: str
    summary: str
    path: str
    before: str
    after: str


def _audit_log_path() -> Path:
    explicit = os.environ.get(AUDIT_LOG_PATH_ENV_VAR)
    return Path(explicit) if explicit else DEFAULT_AUDIT_LOG_PATH


def record_entry(
    *,
    actor_key_id: str,
    resource_type: str,
    resource_id: str | None,
    action: str,
    summary: str,
    path: Path,
    before: str,
    after: str,
) -> AuditEntry:
    """Appends one entry to the audit log. Callers record only after their
    write has actually succeeded (and, for guardrails configs, passed
    reload validation) -- see the module docstring -- so the log never
    describes a change that didn't really happen.

    A plain synchronous file append with no `await` in between construction
    and the write, so it's safe without an extra lock: nothing else can
    interleave mid-call within a single asyncio event loop (matches the
    reasoning already used for RequestLoggingMiddleware's Prometheus
    counters). _trim_if_needed() below, called at the end of this
    function, is synchronous for the same reason -- the whole append+trim
    sequence is atomic with respect to the rest of the event loop even
    though it isn't behind an explicit lock.
    """
    entry = AuditEntry(
        id=uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc).isoformat(),
        actor_key_id=actor_key_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        summary=summary,
        path=str(path),
        before=before,
        after=after,
    )
    log_path = _audit_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")
    _trim_if_needed(log_path)
    return entry


def _trim_if_needed(log_path: Path) -> None:
    """Keeps the audit log bounded to _max_entries() entries. Cheap to
    check on every write -- reading/counting lines in a file this size is
    fast -- and only actually rewrites the file once the cap is exceeded,
    not on every call. Rewriting (not truncating in place) is what
    atomic_write_text already gives every other admin write in this
    codebase: a crash or concurrent read mid-trim never observes a
    partially-trimmed file.
    """
    max_entries = _max_entries()
    lines = log_path.read_text().splitlines()
    if len(lines) <= max_entries:
        return
    trimmed = "\n".join(lines[-max_entries:]) + "\n"
    atomic_write_text(log_path, trimmed)


def _read_all_entries() -> list[AuditEntry]:
    log_path = _audit_log_path()
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        entries.append(AuditEntry(**json.loads(line)))
    return entries


def list_entries(*, limit: int = 50) -> list[AuditEntry]:
    """Most-recent-first, capped at min(limit, _MAX_LISTED_ENTRIES)."""
    entries = _read_all_entries()
    entries.reverse()
    return entries[: min(limit, _MAX_LISTED_ENTRIES)]


def get_entry(entry_id: str) -> AuditEntry | None:
    for entry in _read_all_entries():
        if entry.id == entry_id:
            return entry
    return None


def revert_entry(entry_id: str, *, actor_key_id: str) -> tuple[AuditEntry, AuditEntry]:
    """Writes `entry_id`'s `before` text back to its file and records a new
    "revert" audit entry describing that write (the log is append-only --
    a revert is a new event, not an edit to history). Returns
    (original_entry, revert_entry). Raises KeyError if entry_id doesn't
    exist.

    Does NOT invalidate any in-process cache (get_key_store,
    get_prompt_injection_config, a GuardrailsService's per-config_id
    cache) -- callers (see app/api/routes/admin.py) do that based on the
    returned original entry's resource_type/resource_id, the same way
    every other admin write endpoint already does its own invalidation.
    """
    original = get_entry(entry_id)
    if original is None:
        raise KeyError(entry_id)

    path = Path(original.path)
    current_text = path.read_text() if path.exists() else ""
    atomic_write_text(path, original.before)

    revert = record_entry(
        actor_key_id=actor_key_id,
        resource_type=original.resource_type,
        resource_id=original.resource_id,
        action="revert",
        summary=f"Reverted {original.id} ({original.action}: {original.summary})",
        path=path,
        before=current_text,
        after=original.before,
    )
    return original, revert
