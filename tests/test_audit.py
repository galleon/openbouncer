import json
import os
from pathlib import Path

import pytest

from app.core.audit import get_entry, list_entries, record_entry, revert_entry, verify_chain
from app.core.hash_chain import GENESIS_HASH


@pytest.fixture(autouse=True)
def _audit_log_path(tmp_path, monkeypatch):
    # Overrides the module-scoped observer_client fixture's env var with a
    # fresh, test-local path per test in this file too (belt and suspenders
    # -- conftest.py's autouse fixture already does this for every test,
    # this just makes the intent explicit here).
    monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_PATH", str(tmp_path / "audit_log.jsonl"))


@pytest.mark.asyncio
async def test_record_entry_appends_and_is_listed(tmp_path):
    target = tmp_path / "some_config.yaml"
    target.write_text("before: true\n")

    entry = await record_entry(
        actor_key_id="admin-key",
        resource_type="api_keys",
        resource_id=None,
        action="create_key",
        summary="Created key 'foo'",
        path=target,
        before="before: true\n",
        after="before: true\nfoo: bar\n",
    )

    listed = list_entries(limit=10)
    assert len(listed) == 1
    assert listed[0].id == entry.id
    assert listed[0].actor_key_id == "admin-key"
    assert listed[0].action == "create_key"
    assert listed[0].before == "before: true\n"
    assert listed[0].after == "before: true\nfoo: bar\n"


@pytest.mark.asyncio
async def test_list_entries_is_most_recent_first(tmp_path):
    target = tmp_path / "f.yaml"
    for i in range(3):
        await record_entry(
            actor_key_id="admin-key",
            resource_type="api_keys",
            resource_id=None,
            action=f"action-{i}",
            summary=f"summary-{i}",
            path=target,
            before="",
            after="",
        )

    listed = list_entries(limit=10)
    assert [e.action for e in listed] == ["action-2", "action-1", "action-0"]


@pytest.mark.asyncio
async def test_list_entries_respects_limit(tmp_path):
    target = tmp_path / "f.yaml"
    for i in range(5):
        await record_entry(
            actor_key_id="admin-key",
            resource_type="api_keys",
            resource_id=None,
            action=f"action-{i}",
            summary="",
            path=target,
            before="",
            after="",
        )

    assert len(list_entries(limit=2)) == 2


def test_list_entries_empty_when_no_log_file():
    assert list_entries(limit=10) == []


def test_get_entry_returns_none_for_unknown_id():
    assert get_entry("does-not-exist") is None


@pytest.mark.asyncio
async def test_get_entry_finds_by_id(tmp_path):
    target = tmp_path / "f.yaml"
    entry = await record_entry(
        actor_key_id="admin-key",
        resource_type="prompt_injection",
        resource_id=None,
        action="update_prompt_injection_config",
        summary="",
        path=target,
        before="",
        after="",
    )
    found = get_entry(entry.id)
    assert found == entry


@pytest.mark.asyncio
async def test_revert_entry_restores_prior_file_content(tmp_path):
    target = tmp_path / "guardrails.yaml"
    target.write_text("policy:\n  - old rule\n")

    entry = await record_entry(
        actor_key_id="admin-key",
        resource_type="guardrails_config",
        resource_id="topic_safety",
        action="update_guardrails_config",
        summary="Updated sections",
        path=target,
        before="policy:\n  - old rule\n",
        after="policy:\n  - new rule\n",
    )
    # Simulate the write this entry describes actually having happened.
    target.write_text("policy:\n  - new rule\n")

    original, revert = await revert_entry(entry.id, actor_key_id="admin-key")

    assert original == entry
    assert target.read_text() == "policy:\n  - old rule\n"
    assert revert.action == "revert"
    assert revert.resource_type == "guardrails_config"
    assert revert.resource_id == "topic_safety"
    assert revert.before == "policy:\n  - new rule\n"
    assert revert.after == "policy:\n  - old rule\n"
    assert entry.id in revert.summary


@pytest.mark.asyncio
async def test_revert_entry_itself_appears_in_the_log(tmp_path):
    target = tmp_path / "f.yaml"
    target.write_text("v: 1\n")
    entry = await record_entry(
        actor_key_id="admin-key",
        resource_type="api_keys",
        resource_id=None,
        action="update_key",
        summary="",
        path=target,
        before="v: 1\n",
        after="v: 2\n",
    )
    target.write_text("v: 2\n")

    await revert_entry(entry.id, actor_key_id="admin-key")

    listed = list_entries(limit=10)
    assert [e.action for e in listed] == ["revert", "update_key"]


@pytest.mark.asyncio
async def test_revert_unknown_entry_raises_key_error():
    with pytest.raises(KeyError):
        await revert_entry("does-not-exist", actor_key_id="admin-key")


@pytest.mark.asyncio
async def test_revert_entry_handles_missing_current_file(tmp_path):
    # The file described by the entry no longer exists (e.g. deleted by
    # some other means) -- revert should still succeed, recreating it from
    # `before`, with the revert entry's own `before` recorded as "".
    target = tmp_path / "gone.yaml"
    entry = await record_entry(
        actor_key_id="admin-key",
        resource_type="api_keys",
        resource_id=None,
        action="update_key",
        summary="",
        path=target,
        before="v: 1\n",
        after="v: 2\n",
    )

    original, revert = await revert_entry(entry.id, actor_key_id="admin-key")

    assert target.read_text() == "v: 1\n"
    assert revert.before == ""


class TestRetention:
    async def _record(self, tmp_path, action: str):
        return await record_entry(
            actor_key_id="admin-key",
            resource_type="api_keys",
            resource_id=None,
            action=action,
            summary="",
            path=tmp_path / "f.yaml",
            before="",
            after="",
        )

    @pytest.mark.asyncio
    async def test_log_stays_under_cap_without_trimming(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES", "5")
        for i in range(5):
            await self._record(tmp_path, f"action-{i}")

        log_path = Path(os.environ["OPENBOUNCER_AUDIT_LOG_PATH"])
        assert len(log_path.read_text().splitlines()) == 5

    @pytest.mark.asyncio
    async def test_exceeding_cap_trims_oldest_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES", "3")
        for i in range(5):
            await self._record(tmp_path, f"action-{i}")

        entries = list_entries(limit=10)
        assert len(entries) == 3
        # Most-recent-first -- the two oldest (action-0, action-1) were
        # trimmed away.
        assert [e.action for e in entries] == ["action-4", "action-3", "action-2"]

    @pytest.mark.asyncio
    async def test_trimmed_entry_is_unfetchable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES", "1")
        first = await self._record(tmp_path, "action-0")
        await self._record(tmp_path, "action-1")

        assert get_entry(first.id) is None

    @pytest.mark.asyncio
    async def test_trimmed_entry_cannot_be_reverted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES", "1")
        first = await self._record(tmp_path, "action-0")
        await self._record(tmp_path, "action-1")

        with pytest.raises(KeyError):
            await revert_entry(first.id, actor_key_id="admin-key")

    @pytest.mark.asyncio
    async def test_invalid_max_entries_env_var_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES", "not-a-number")
        for i in range(10):
            await self._record(tmp_path, f"action-{i}")

        log_path = Path(os.environ["OPENBOUNCER_AUDIT_LOG_PATH"])
        # Default (5000) is well above 10 -- nothing gets trimmed.
        assert len(log_path.read_text().splitlines()) == 10


class TestHashChain:
    async def _record(self, tmp_path, action: str):
        return await record_entry(
            actor_key_id="admin-key",
            resource_type="api_keys",
            resource_id=None,
            action=action,
            summary="",
            path=tmp_path / "f.yaml",
            before="",
            after="",
        )

    @pytest.mark.asyncio
    async def test_first_entry_chains_from_genesis(self, tmp_path):
        entry = await self._record(tmp_path, "action-0")
        assert entry.prev_hash == GENESIS_HASH
        assert entry.hash

    @pytest.mark.asyncio
    async def test_entries_chain_to_each_other(self, tmp_path):
        first = await self._record(tmp_path, "action-0")
        second = await self._record(tmp_path, "action-1")
        assert second.prev_hash == first.hash

    @pytest.mark.asyncio
    async def test_verify_chain_valid_after_normal_writes(self, tmp_path):
        for i in range(5):
            await self._record(tmp_path, f"action-{i}")
        result = verify_chain()
        assert result.valid is True
        assert result.verified_count == 5

    def test_verify_chain_valid_on_empty_log(self):
        result = verify_chain()
        assert result.valid is True
        assert result.verified_count == 0

    @pytest.mark.asyncio
    async def test_verify_chain_detects_direct_file_tampering(self, tmp_path):
        await self._record(tmp_path, "action-0")
        await self._record(tmp_path, "action-1")

        log_path = Path(os.environ["OPENBOUNCER_AUDIT_LOG_PATH"])
        lines = log_path.read_text().splitlines()
        tampered = json.loads(lines[0])
        tampered["summary"] = "not what was actually recorded"
        lines[0] = json.dumps(tampered)
        log_path.write_text("\n".join(lines) + "\n")

        result = verify_chain()
        assert result.valid is False
        assert result.broken_reason is not None

    @pytest.mark.asyncio
    async def test_verify_chain_stays_valid_across_a_trim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENBOUNCER_AUDIT_LOG_MAX_ENTRIES", "3")
        for i in range(6):
            await self._record(tmp_path, f"action-{i}")

        # A checkpoint should exist now (3 entries were trimmed away), and
        # verification of the remaining 3 should still succeed by
        # continuing from it.
        log_path = Path(os.environ["OPENBOUNCER_AUDIT_LOG_PATH"])
        checkpoint_path = log_path.with_name("audit_log.chain_checkpoint.json")
        assert checkpoint_path.exists()

        result = verify_chain()
        assert result.valid is True
        assert result.verified_count == 3

    @pytest.mark.asyncio
    async def test_verify_chain_tolerates_pre_upgrade_legacy_lines(self, tmp_path):
        # Simulates a deployment upgrading to this feature with an existing,
        # unchained log already on disk.
        log_path = tmp_path / "audit_log.jsonl"
        os.environ["OPENBOUNCER_AUDIT_LOG_PATH"] = str(log_path)
        legacy_entry = {
            "id": "legacy-1",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "actor_key_id": "admin-key",
            "resource_type": "api_keys",
            "resource_id": None,
            "action": "create_key",
            "summary": "written before this feature existed",
            "path": str(tmp_path / "f.yaml"),
            "before": "",
            "after": "",
        }
        log_path.write_text(json.dumps(legacy_entry) + "\n")

        await self._record(tmp_path, "action-after-upgrade")

        result = verify_chain()
        assert result.valid is True
        assert result.legacy_unchained_count == 1
        assert result.verified_count == 1
