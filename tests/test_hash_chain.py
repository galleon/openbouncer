import json

from app.core.hash_chain import (
    GENESIS_HASH,
    checkpoint_path_for,
    compute_hash,
    previous_hash,
    read_checkpoint,
    verify_chain,
    verify_log_file,
    write_checkpoint,
)


def _chain(*payloads: dict) -> list[dict]:
    """Builds a valid hash-chained sequence of raw entry dicts, the same
    way record_entry/record_event do."""
    entries = []
    prev = GENESIS_HASH
    for payload in payloads:
        h = compute_hash(prev, payload)
        entries.append({**payload, "prev_hash": prev, "hash": h})
        prev = h
    return entries


def test_compute_hash_is_deterministic():
    payload = {"id": "1", "action": "create_key"}
    assert compute_hash(GENESIS_HASH, payload) == compute_hash(GENESIS_HASH, payload)


def test_compute_hash_ignores_key_order():
    assert compute_hash(GENESIS_HASH, {"a": 1, "b": 2}) == compute_hash(GENESIS_HASH, {"b": 2, "a": 1})


def test_compute_hash_changes_with_content():
    assert compute_hash(GENESIS_HASH, {"a": 1}) != compute_hash(GENESIS_HASH, {"a": 2})


def test_compute_hash_changes_with_prev_hash():
    assert compute_hash("aaa", {"a": 1}) != compute_hash("bbb", {"a": 1})


def test_verify_chain_valid_sequence():
    entries = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1}, {"id": "3", "n": 2})
    result = verify_chain(entries, checkpoint_hash=None)
    assert result.valid is True
    assert result.verified_count == 3
    assert result.legacy_unchained_count == 0
    assert result.broken_at_id is None


def test_verify_chain_empty_is_valid():
    result = verify_chain([], checkpoint_hash=None)
    assert result.valid is True
    assert result.verified_count == 0


def test_verify_chain_detects_modified_content():
    entries = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1})
    entries[0]["n"] = 999  # tamper after the fact, without recomputing hashes
    result = verify_chain(entries, checkpoint_hash=None)
    assert result.valid is False
    assert result.broken_at_id == "1"
    assert "content" in result.broken_reason


def test_verify_chain_detects_deleted_entry():
    entries = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1}, {"id": "3", "n": 2})
    del entries[1]  # remove the middle entry -- breaks the chain link
    result = verify_chain(entries, checkpoint_hash=None)
    assert result.valid is False
    assert result.broken_at_id == "3"
    assert "prev_hash" in result.broken_reason


def test_verify_chain_detects_reordered_entries():
    entries = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1}, {"id": "3", "n": 2})
    entries[1], entries[2] = entries[2], entries[1]
    result = verify_chain(entries, checkpoint_hash=None)
    assert result.valid is False


def test_verify_chain_tolerates_leading_legacy_entries():
    legacy = [{"id": "old-1", "n": -1}, {"id": "old-2", "n": -2}]
    chained = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1})
    result = verify_chain(legacy + chained, checkpoint_hash=None)
    assert result.valid is True
    assert result.legacy_unchained_count == 2
    assert result.verified_count == 2


def test_verify_chain_rejects_legacy_entry_after_chain_started():
    chained = _chain({"id": "1", "n": 0})
    legacy = [{"id": "old", "n": -1}]
    result = verify_chain(chained + legacy, checkpoint_hash=None)
    assert result.valid is False
    assert result.broken_at_id == "old"


def test_verify_chain_continues_from_checkpoint():
    # Simulates the state right after a trim: the entries that would have
    # preceded `entries[0]` are gone, but the checkpoint records their
    # chain's last hash, so verification can still confirm continuity.
    full = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1}, {"id": "3", "n": 2})
    checkpoint_hash = full[0]["hash"]
    remaining = full[1:]
    result = verify_chain(remaining, checkpoint_hash=checkpoint_hash)
    assert result.valid is True
    assert result.verified_count == 2


def test_verify_chain_detects_break_across_checkpoint_boundary():
    full = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1})
    remaining = full[1:]
    # Wrong checkpoint hash -- as if the sidecar checkpoint didn't actually
    # match what got trimmed (data corruption, or genuine tampering).
    result = verify_chain(remaining, checkpoint_hash="0" * 64)
    assert result.valid is False
    assert result.broken_at_id == "2"


def test_previous_hash_is_genesis_for_fresh_log(tmp_path):
    log_path = tmp_path / "log.jsonl"
    assert previous_hash(log_path) == GENESIS_HASH


def test_previous_hash_reads_last_line(tmp_path):
    log_path = tmp_path / "log.jsonl"
    entries = _chain({"id": "1"}, {"id": "2"})
    log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    assert previous_hash(log_path) == entries[-1]["hash"]


def test_previous_hash_falls_back_to_genesis_for_legacy_last_line(tmp_path):
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(json.dumps({"id": "old", "n": 0}) + "\n")
    assert previous_hash(log_path) == GENESIS_HASH


def test_previous_hash_uses_checkpoint_when_file_only_has_the_kept_tail(tmp_path):
    log_path = tmp_path / "log.jsonl"
    write_checkpoint(log_path, trimmed_through_hash="deadbeef", newly_trimmed_count=10)
    log_path.write_text("")  # kept tail happens to be empty in this scenario
    assert previous_hash(log_path) == "deadbeef"


def test_write_checkpoint_is_cumulative(tmp_path):
    log_path = tmp_path / "log.jsonl"
    write_checkpoint(log_path, trimmed_through_hash="aaa", newly_trimmed_count=5)
    write_checkpoint(log_path, trimmed_through_hash="bbb", newly_trimmed_count=3)
    checkpoint = read_checkpoint(log_path)
    assert checkpoint["trimmed_through_hash"] == "bbb"
    assert checkpoint["trimmed_entry_count"] == 8


def test_read_checkpoint_none_when_absent(tmp_path):
    assert read_checkpoint(tmp_path / "log.jsonl") is None


def test_checkpoint_path_naming(tmp_path):
    log_path = tmp_path / "audit_log.jsonl"
    assert checkpoint_path_for(log_path) == tmp_path / "audit_log.chain_checkpoint.json"


def test_verify_log_file_missing_is_valid(tmp_path):
    result = verify_log_file(tmp_path / "does_not_exist.jsonl")
    assert result.valid is True
    assert result.verified_count == 0


def test_verify_log_file_end_to_end(tmp_path):
    log_path = tmp_path / "log.jsonl"
    entries = _chain({"id": "1"}, {"id": "2"})
    log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    result = verify_log_file(log_path)
    assert result.valid is True
    assert result.verified_count == 2


def test_verify_log_file_detects_tampering_on_disk(tmp_path):
    log_path = tmp_path / "log.jsonl"
    entries = _chain({"id": "1", "n": 0}, {"id": "2", "n": 1})
    lines = [json.dumps(e) for e in entries]
    log_path.write_text("\n".join(lines) + "\n")

    # Directly edit the file, as an attacker with filesystem access would --
    # bypassing record_entry()/record_event() entirely.
    tampered = json.loads(lines[0])
    tampered["n"] = 999
    lines[0] = json.dumps(tampered)
    log_path.write_text("\n".join(lines) + "\n")

    result = verify_log_file(log_path)
    assert result.valid is False
    assert result.broken_at_id == "1"
