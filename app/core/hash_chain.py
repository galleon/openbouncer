"""Hash-chaining mechanics shared by app.core.audit and
app.core.guardrail_events, so tamper-evidence isn't implemented twice for
two JSONL logs whose append/trim/read plumbing is already near-identical.

What this proves, and what it doesn't: each entry's `hash` covers its own
content plus the previous entry's `hash` (`prev_hash`), so editing,
deleting, or reordering any past entry breaks every hash from that point
forward -- verify_chain() below detects that. This proves *internal
consistency* of the file: nothing changed after the fact without leaving a
detectable gap. It does NOT prove the log wasn't tampered with by someone
who has filesystem write access to it *and* understands this mechanism --
that person can regenerate the whole chain from scratch, consistently. Real
protection against that needs external anchoring (publishing periodic root
hashes somewhere this project doesn't control), which is out of scope here.
See README's "Tamper-evident audit log" section for the full story.

Both logs also trim their oldest entries once they exceed a configured cap
(see each module's _trim_if_needed) -- once that happens, the file's new
first entry has a `prev_hash` pointing at an entry that's no longer on
disk, so verification can't recompute it. write_checkpoint()/read_checkpoint()
persist a small sidecar JSON file recording the hash of the last entry
*evicted* by each trim, so verify_chain() has something to check the
post-trim file's first entry against -- a trim is then a documented,
verifiable continuation ("N entries trimmed at T, chain continues from
checkpoint X"), not a silently-accepted gap.

Pre-existing deployments' log files predate this feature and have no
`hash`/`prev_hash` fields at all. Those lines are never retroactively
hashed -- doing so would fabricate a chain across history this code can't
actually vouch for, which is worse than no chain at all. Instead the first
entry written after upgrading starts a fresh chain from GENESIS_HASH, and
verify_chain() reports pre-existing lines as "legacy" (counted, not
chain-checked) rather than pass or fail.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.atomic_write import atomic_write_text

# Fixed sentinel prev_hash for the first entry of a chain -- either the
# very first entry ever written to a fresh log, or the first entry written
# after upgrading a pre-existing unchained log (see module docstring).
# Not a real hash of anything; just a well-known starting point.
GENESIS_HASH = "0" * 64

CHECKPOINT_SUFFIX = ".chain_checkpoint.json"


def checkpoint_path_for(log_path: Path) -> Path:
    return log_path.with_name(log_path.stem + CHECKPOINT_SUFFIX)


def compute_hash(prev_hash: str, payload: dict) -> str:
    """payload is an entry's own fields, excluding `hash`/`prev_hash` --
    canonical (sort_keys) JSON so a field-ordering change alone never
    changes the hash of otherwise-identical content."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def read_checkpoint(log_path: Path) -> dict | None:
    path = checkpoint_path_for(log_path)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_checkpoint(log_path: Path, *, trimmed_through_hash: str | None, newly_trimmed_count: int) -> None:
    """Called by each log's _trim_if_needed right after it rewrites the
    trimmed file. Cumulative: trimmed_entry_count is the total ever
    trimmed over the life of the deployment, not just this trim, so the
    verify endpoints can report the full picture."""
    existing = read_checkpoint(log_path)
    cumulative = (existing["trimmed_entry_count"] if existing else 0) + newly_trimmed_count
    payload = {
        "trimmed_through_hash": trimmed_through_hash,
        "trimmed_entry_count": cumulative,
        "trimmed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_text(checkpoint_path_for(log_path), json.dumps(payload))


def previous_hash(log_path: Path) -> str:
    """What a new entry's prev_hash should be: the last written entry's
    hash if it has one, else the last trim checkpoint's, else GENESIS_HASH
    (fresh log, or the log is still all pre-upgrade legacy entries)."""
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        if lines:
            last = json.loads(lines[-1])
            if last.get("hash"):
                return last["hash"]
    checkpoint = read_checkpoint(log_path)
    if checkpoint and checkpoint.get("trimmed_through_hash"):
        return checkpoint["trimmed_through_hash"]
    return GENESIS_HASH


@dataclass(frozen=True)
class ChainVerificationResult:
    valid: bool
    verified_count: int
    legacy_unchained_count: int
    broken_at_id: str | None
    broken_reason: str | None


def verify_chain(raw_entries: list[dict], *, checkpoint_hash: str | None) -> ChainVerificationResult:
    """raw_entries: parsed JSON objects in file order (oldest first).
    Entries missing `hash`/`prev_hash` are legacy (pre-upgrade) and are
    counted, not chain-checked -- but only while the chain hasn't started
    yet; a legacy entry appearing *after* a chained one (or after a
    checkpoint, i.e. resuming a trimmed file) is itself a break, since a
    real chain has no gaps once it starts.
    """
    expected_prev = checkpoint_hash if checkpoint_hash is not None else GENESIS_HASH
    started = checkpoint_hash is not None
    verified = 0
    legacy_count = 0

    for entry in raw_entries:
        if not entry.get("hash") or not entry.get("prev_hash"):
            if started:
                return ChainVerificationResult(
                    valid=False,
                    verified_count=verified,
                    legacy_unchained_count=legacy_count,
                    broken_at_id=entry.get("id"),
                    broken_reason="unchained (legacy) entry found after the chain had started",
                )
            legacy_count += 1
            continue

        started = True
        if entry["prev_hash"] != expected_prev:
            return ChainVerificationResult(
                valid=False,
                verified_count=verified,
                legacy_unchained_count=legacy_count,
                broken_at_id=entry.get("id"),
                broken_reason="prev_hash does not match the preceding entry's hash",
            )
        payload = {k: v for k, v in entry.items() if k not in ("hash", "prev_hash")}
        if compute_hash(expected_prev, payload) != entry["hash"]:
            return ChainVerificationResult(
                valid=False,
                verified_count=verified,
                legacy_unchained_count=legacy_count,
                broken_at_id=entry.get("id"),
                broken_reason="hash does not match this entry's content (modified after being written)",
            )
        expected_prev = entry["hash"]
        verified += 1

    return ChainVerificationResult(
        valid=True,
        verified_count=verified,
        legacy_unchained_count=legacy_count,
        broken_at_id=None,
        broken_reason=None,
    )


def verify_log_file(log_path: Path) -> ChainVerificationResult:
    """Reads log_path and its sidecar checkpoint (if any) and verifies the
    chain -- the one function both the admin endpoints and the standalone
    `python -m app.core.hash_chain <path>` CLI below call."""
    if not log_path.exists():
        return ChainVerificationResult(
            valid=True, verified_count=0, legacy_unchained_count=0, broken_at_id=None, broken_reason=None
        )
    raw_entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    checkpoint = read_checkpoint(log_path)
    checkpoint_hash = checkpoint["trimmed_through_hash"] if checkpoint else None
    return verify_chain(raw_entries, checkpoint_hash=checkpoint_hash)


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Verify a hash-chained OpenBouncer JSONL log (audit_log.jsonl or "
            "guardrail_events.jsonl) offline, without running the server."
        )
    )
    parser.add_argument("log_path", type=Path)
    args = parser.parse_args()

    if not args.log_path.exists():
        print(f"{args.log_path}: no such file")
        return 1

    result = verify_log_file(args.log_path)
    if result.valid:
        print(
            f"OK -- {result.verified_count} chained entries verified, "
            f"{result.legacy_unchained_count} legacy (pre-upgrade) entries skipped."
        )
        return 0
    print(
        f"BROKEN CHAIN -- {result.broken_reason} at entry {result.broken_at_id} "
        f"({result.verified_count} entries verified before the break, "
        f"{result.legacy_unchained_count} legacy entries skipped)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
