from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import cast

import pytest

from harness.vault.__main__ import IDLE_SECONDS, VaultApplication
from harness.vault.store import FIELDS, VaultError, VaultStore

PASSWORD = "a-test-only-master-password"
SECRET = "private-canary-never-in-harness-89d07"


def record(**changes: str) -> dict[str, str]:
    value = dict.fromkeys(FIELDS, "")
    value.update(
        title="Production SSH",
        project_id="p1",
        project_name="Example",
        kind="ssh",
        host="server.invalid",
        username="operator",
        password=SECRET,
        private_key="test-private-key",
        notes="private note",
    )
    value.update(changes)
    return value


@pytest.fixture
def vault(tmp_path: Path) -> VaultStore:
    backup = tmp_path / "backup"
    backup.mkdir()
    store = VaultStore(tmp_path / "vault")
    store.create(PASSWORD, str(backup))
    return store


def backups(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "backup" / "harness-vault-backups").glob("*.kdbx"))


def test_full_round_trip_and_independent_recovery(vault: VaultStore, tmp_path: Path) -> None:
    vault.save_record("", record(), vault.revision)
    listing = cast(list[dict[str, str]], vault.state()["records"])
    identity = listing[0]["id"]
    assert SECRET not in json.dumps(listing)
    assert "private_key" not in listing[0]
    assert vault.detail(identity)["password"] == SECRET
    assert len(backups(tmp_path)) == 2
    for path in [vault.path, *backups(tmp_path)]:
        assert SECRET.encode() not in path.read_bytes()
        assert b"Production SSH" not in path.read_bytes()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = backups(tmp_path)[-1].read_bytes()
    assert saved == vault.path.read_bytes()
    vault.lock()
    with pytest.raises(VaultError, match="locked"):
        vault.state()
    vault.unlock(PASSWORD)
    assert vault.detail(identity)["notes"] == "private note"
    restored = VaultStore(tmp_path / "new-machine")
    restored.recover(saved, PASSWORD, str(tmp_path / "backup"))
    assert restored.detail(identity) == vault.detail(identity)
    restored.delete_record(identity, restored.revision)
    assert restored.state()["records"] == []
    restored.restore(saved, PASSWORD, str(tmp_path / "backup"), restored.revision)
    assert restored.detail(identity)["private_key"] == "test-private-key"
    assert any("before-restore" in path.name for path in backups(tmp_path))


def test_failed_backup_never_replaces_live_file(vault: VaultStore, tmp_path: Path) -> None:
    original = vault.path.read_bytes()
    (tmp_path / "backup").rename(tmp_path / "disconnected")
    with pytest.raises(VaultError, match="backup_directory"):
        vault.save_record("", record(), vault.revision)
    assert vault.path.read_bytes() == original
    assert vault.database is None
    vault.unlock(PASSWORD)
    assert vault.state()["records"] == []


def test_corrupt_live_file_can_be_recovered_without_unlock(
    vault: VaultStore, tmp_path: Path
) -> None:
    saved = vault.path.read_bytes()
    vault.lock()
    vault.path.write_bytes(b"corrupted live file")
    with pytest.raises(VaultError):
        vault.unlock(PASSWORD)
    vault.recover(saved, PASSWORD, str(tmp_path / "backup"))
    assert vault.state()["records"] == []
    previous = [path for path in backups(tmp_path) if "before-recovery" in path.name]
    assert previous[0].read_bytes() == b"corrupted live file"


def test_bad_password_and_corruption_leave_existing_data(vault: VaultStore, tmp_path: Path) -> None:
    original = vault.path.read_bytes()
    for value, password in [
        (original, "wrong"),
        (original[:-32] + bytes(32), PASSWORD),
        (b"not-a-database", PASSWORD),
    ]:
        with pytest.raises(VaultError):
            vault.restore(value, password, str(tmp_path / "backup"), vault.revision)
        assert vault.path.read_bytes() == original
    assert len(backups(tmp_path)) == 1


def test_stale_revision_and_external_change_are_not_overwritten(vault: VaultStore) -> None:
    previous = vault.revision
    vault.save_record("", record(), previous)
    original = vault.path.read_bytes()
    with pytest.raises(VaultError, match="conflict"):
        vault.save_record("", record(title="stale"), previous)
    assert vault.path.read_bytes() == original
    vault.path.write_bytes(b"externally changed")
    with pytest.raises(VaultError, match="conflict"):
        vault.save_record("", record(title="another"), vault.revision)
    assert vault.path.read_bytes() == b"externally changed"
    assert vault.database is None


def test_password_rotation_preserves_old_backup_password(vault: VaultStore, tmp_path: Path) -> None:
    old = vault.path.read_bytes()
    vault.change_password("a-new-master-password", vault.revision)
    new = vault.path.read_bytes()
    vault.lock()
    with pytest.raises(VaultError):
        vault.unlock(PASSWORD)
    vault.unlock("a-new-master-password")
    assert vault.path.read_bytes() == new
    restored = VaultStore(tmp_path / "other")
    restored.recover(old, PASSWORD, str(tmp_path / "backup"))
    assert restored.state()["records"] == []


def test_disk_failure_keeps_previous_file_and_locks_working_copy(
    vault: VaultStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = vault.path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("harness.vault.store.os.replace", fail)
    with pytest.raises(OSError):
        vault.save_record("", record(), vault.revision)
    assert vault.path.read_bytes() == original
    assert vault.database is None


def test_paths_and_unknown_owned_directories_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "user-data"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(VaultError, match="unsafe_path"):
        VaultStore(link / "nested")
    assert not (target / "nested").exists()
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(VaultError, match="unsafe_path"):
        VaultStore(unsafe)
    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o755


def test_validation_rejects_record_and_preserves_unlocked_state(vault: VaultStore) -> None:
    for value in [
        record(port="0"),
        record(port="65536"),
        record(title=""),
        record(kind="unknown"),
        record(notes="x" * 65537),
    ]:
        with pytest.raises(VaultError, match="invalid_record"):
            vault.save_record("", value, vault.revision)
    assert vault.state()["records"] == []


def test_sessions_expire_rotate_and_reject_model_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()
    app = VaultApplication(tmp_path / "vault")
    now = [1000.0]
    monkeypatch.setattr("harness.vault.__main__.monotonic", lambda: now[0])
    response = app.call("create", {"password": PASSWORD, "folder": str(folder)}, "")
    token = cast(str, response["token"])
    with pytest.raises(VaultError, match="locked"):
        app.call("state", {}, "harness-mcp-bearer")
    assert app.call("state", {}, token)["records"] == []
    now[0] += IDLE_SECONDS + 1
    with pytest.raises(VaultError, match="locked"):
        app.call("state", {}, token)
    assert app.store.database is None
    new = app.call("unlock", {"password": PASSWORD}, "")["token"]
    assert new != token
    with pytest.raises(VaultError, match="locked"):
        app.call("state", {}, token)
    assert app.store.revision == hashlib.sha256(app.store.path.read_bytes()).hexdigest()


@pytest.mark.skipif(os.name == "nt", reason="POSIX private runtime")
def test_vault_never_imports_core_business_state() -> None:
    root = Path(__file__).parents[1] / "src" / "harness" / "vault"
    for path in root.glob("*.py"):
        source = path.read_text()
        for name in ("storage", "daemon", "registry", "retrieval", "mcp_bridge", "knowledge"):
            assert f"from harness.{name}" not in source
