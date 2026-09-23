from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from test_vault import PASSWORD, SECRET, backups, record

from harness.vault.__main__ import IDLE_SECONDS, VaultApplication
from harness.vault.keychain import DeviceKey
from harness.vault.store import VaultError, VaultStore


def test_empty_password_file_and_backup_are_portable(tmp_path: Path) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()
    store = VaultStore(tmp_path / "vault")
    store.create("", str(folder), no_password=True)
    store.save_record("", record(), store.revision)
    identity = cast(list[dict[str, str]], store.state()["records"])[0]["id"]
    fresh = VaultStore(tmp_path / "new-computer")
    fresh.recover(backups(tmp_path)[-1].read_bytes(), "", str(folder))
    assert fresh.detail(identity)["password"] == SECRET
    assert fresh.detail(identity)["private_key"] == "test-private-key"
    fresh.lock()
    fresh.unlock("")
    assert fresh.detail(identity)["notes"] == "private note"


def test_existing_password_is_not_bypassed_and_conversion_preserves_previous_copy(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()
    app = VaultApplication(tmp_path / "vault")
    app.call("create", {"password": PASSWORD, "folder": str(folder)}, "")
    old_bytes = app.store.path.read_bytes()
    with pytest.raises(VaultError, match="password_required"):
        app.call("open_without_password", {}, "")
    with pytest.raises(VaultError, match="locked"):
        app.call("disable_password", {"revision": app.store.revision}, "model-bearer")
    with pytest.raises(VaultError, match="conflict"):
        app.call("disable_password", {"revision": "stale"}, app.token)
    converted = app.call("disable_password", {"revision": app.store.revision}, app.token)
    assert converted["access_mode"] == "no_password"
    assert [p.read_bytes() for p in backups(tmp_path) if "before-password-removal" in p.name] == [
        old_bytes
    ]
    independent = VaultStore(tmp_path / "other")
    independent.recover(old_bytes, PASSWORD, str(folder))
    restarted = VaultApplication(tmp_path / "vault")
    assert restarted.call("status", {}, "")["access_mode"] == "no_password"
    assert restarted.call("open_without_password", {}, "")["records"] == []


def test_missing_backup_disk_rejects_password_removal_without_data_loss(tmp_path: Path) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()
    store = VaultStore(tmp_path / "vault")
    store.create(PASSWORD, str(folder))
    previous = store.path.read_bytes()
    folder.rename(tmp_path / "unmounted")
    with pytest.raises(VaultError, match="backup_directory"):
        store.disable_password(store.revision)
    assert store.path.read_bytes() == previous
    store.unlock(PASSWORD)


def test_no_password_needs_no_keychain_and_does_not_expire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No-password entry must never call the OS keychain")

    monkeypatch.setattr(DeviceKey, "_call", forbidden)
    app = VaultApplication(tmp_path / "vault")
    result = app.call(
        "create", {"access_mode": "no_password", "folder": str(folder), "remember": True}, ""
    )
    assert result["access_mode"] == "no_password"
    monkeypatch.setattr("harness.vault.__main__.monotonic", lambda: app.last_use + IDLE_SECONDS + 1)
    assert app.call("state", {}, app.token)["records"] == []
    with pytest.raises(VaultError, match="invalid_request"):
        app.call("device_remember", {}, app.token)
    app.lock(pause_device=False)
    assert app.call("open_without_password", {}, "")["access_mode"] == "no_password"


def test_return_to_password_mode_and_old_open_backups_remain_portable(tmp_path: Path) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()
    app = VaultApplication(tmp_path / "vault")
    app.call("create", {"access_mode": "no_password", "folder": str(folder)}, "")
    open_backup = app.store.path.read_bytes()
    open_token = app.token
    state = app.call("password", {"password": PASSWORD, "revision": app.store.revision}, app.token)
    assert state["access_mode"] == "password"
    assert state["token"] != open_token
    with pytest.raises(VaultError, match="locked"):
        app.call("state", {}, open_token)
    app.lock()
    with pytest.raises(VaultError, match="password_required"):
        app.call("open_without_password", {}, "")
    restored = VaultStore(tmp_path / "other")
    restored.recover(open_backup, "", str(folder))
    assert restored.state()["records"] == []
    restarted = VaultApplication(tmp_path / "vault")
    assert restarted.call("status", {}, "")["access_mode"] == "password"


def test_corrupted_open_file_never_silently_replaced(tmp_path: Path) -> None:
    folder = tmp_path / "backup"
    folder.mkdir()
    app = VaultApplication(tmp_path / "vault")
    app.call("create", {"access_mode": "no_password", "folder": str(folder)}, "")
    app.store.path.write_bytes(b"broken")
    with pytest.raises(VaultError, match="password_required"):
        app.call("open_without_password", {}, "")
    assert app.store.path.read_bytes() == b"broken"
