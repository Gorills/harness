from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import cast

import pytest
from test_vault import PASSWORD

from harness.vault.__main__ import IDLE_SECONDS, VaultApplication
from harness.vault.keychain import DeviceKey
from harness.vault.store import VaultError


class MemoryKeychain(DeviceKey):
    """Replace only the OS boundary; real metadata/KDBX/restart behavior remains."""

    def __init__(self, root: Path, secrets: dict[str, str]) -> None:
        super().__init__(root)
        self.secrets = secrets
        self.failed = False

    @property
    def available(self) -> bool:
        return True

    def _call(self, operation: str, password: str = "") -> str:
        if self.failed:
            raise VaultError("device_unavailable")
        if operation == "set":
            self.secrets[self.account] = password
        elif operation == "delete":
            self.secrets.pop(self.account, None)
        elif operation == "get":
            if self.account not in self.secrets:
                raise VaultError("device_unavailable")
            return self.secrets[self.account]
        return ""


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VaultApplication:
    secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "harness.vault.__main__.DeviceKey", lambda root: MemoryKeychain(root, secrets)
    )
    (tmp_path / "copies").mkdir()
    application = VaultApplication(tmp_path / "vault")
    application.call(
        "create", {"password": PASSWORD, "folder": str(tmp_path / "copies"), "remember": True}, ""
    )
    return application


def test_remember_restart_resume_and_explicit_lock(app: VaultApplication) -> None:
    root = app.store.root
    token = app.token
    assert cast(MemoryKeychain, app.device).secrets[app.device.account] == PASSWORD
    assert PASSWORD.encode() not in app.device.path.read_bytes()
    assert stat.S_IMODE(app.device.path.stat().st_mode) == 0o600
    app.lock(pause_device=False)  # Normal process exit preserves the operator's preference.
    restarted = VaultApplication(root)
    opened = restarted.call("device_unlock", {"automatic": True}, "")
    assert opened["records"] == []
    assert opened["token"] != token
    with pytest.raises(VaultError, match="locked"):
        restarted.call("state", {}, token)
    restarted.call("lock", {}, restarted.token)
    restarted = VaultApplication(root)
    with pytest.raises(VaultError, match="locked"):
        restarted.call("device_unlock", {"automatic": True}, "")
    restarted.unlock_after = 0
    assert restarted.call("device_unlock", {"automatic": False}, "")["token"]
    assert restarted.device.status()["automatic"] is True


def test_expiry_does_not_silently_reopen(
    app: VaultApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("harness.vault.__main__.monotonic", lambda: app.last_use + IDLE_SECONDS + 1)
    with pytest.raises(VaultError, match="locked"):
        app.call("state", {}, app.token)
    assert app.store.database is None
    assert app.device.status()["automatic"] is False


def test_keychain_failure_keeps_manual_login_and_never_falls_back_to_file(
    app: VaultApplication,
) -> None:
    device = cast(MemoryKeychain, app.device)
    app.lock()
    device.failed = True
    app.unlock_after = 0
    with pytest.raises(VaultError, match="device_unavailable"):
        app.call("device_unlock", {"automatic": False}, "")
    app.unlock_after = 0
    result = app.call("unlock", {"password": PASSWORD, "remember": True}, "")
    assert result["token"]
    assert result["device_error"] == "device_unavailable"
    for path in app.store.root.iterdir():
        assert PASSWORD.encode() not in path.read_bytes()


def test_disable_revokes_credential_and_rotation_cannot_leave_stale_key(
    app: VaultApplication,
) -> None:
    app.call("device_forget", {}, app.token)
    assert cast(MemoryKeychain, app.device).secrets == {}
    assert not app.device.path.exists()
    with pytest.raises(VaultError, match="locked"):
        app.call("device_remember", {}, "harness-model-token")
    app.call("device_remember", {}, app.token)
    app.call(
        "password", {"password": "new-master-password", "revision": app.store.revision}, app.token
    )
    assert cast(MemoryKeychain, app.device).secrets == {}
    assert app.device.status()["enabled"] is False
    app.call("device_remember", {}, app.token)
    assert cast(MemoryKeychain, app.device).secrets[app.device.account] == "new-master-password"


def test_disable_failure_pauses_auto_unlock(app: VaultApplication) -> None:
    cast(MemoryKeychain, app.device).failed = True
    with pytest.raises(VaultError, match="device_unavailable"):
        app.call("device_forget", {}, app.token)
    assert app.device.status()["automatic"] is False
    assert app.store.state()["records"] == []


def test_helper_uses_stdin_bounded_timeout_and_private_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "test-only")
    monkeypatch.setattr("harness.vault.keychain.sys.platform", "linux")
    key = DeviceKey(tmp_path)

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert PASSWORD not in " ".join(args)
        request = json.loads(cast(bytes, kwargs["input"]))
        assert request == {"operation": "set", "account": key.account, "password": PASSWORD}
        assert kwargs["timeout"] == 45
        assert kwargs["stderr"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(args, 45)

    monkeypatch.setattr("harness.vault.keychain.subprocess.run", run)
    with pytest.raises(VaultError, match="device_unavailable"):
        key.remember(PASSWORD)
    assert not key.path.exists()
    assert key.account != DeviceKey(tmp_path / "other").account


def test_damaged_preference_fails_closed_but_manual_entry_survives(app: VaultApplication) -> None:
    app.device.path.write_bytes(b"broken metadata")
    assert app.device.status()["enabled"] is False
    app.lock()
    app.unlock_after = 0
    assert app.call("unlock", {"password": PASSWORD}, "")["records"] == []


def test_secret_service_adapter_scopes_writes_reads_and_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    import secretstorage

    from harness.vault.keychain import main

    saved: list[bytes] = []
    attributes = {"application": "harness-private-vault", "vault": "test-vault-account"}
    closed: list[bool] = []

    class Connection:
        def close(self) -> None:
            closed.append(True)

    class Item:
        def get_secret(self) -> bytes:
            return saved[0]

        def delete(self) -> None:
            saved.clear()

    class Collection:
        def is_locked(self) -> bool:
            return False

        def ensure_not_locked(self) -> None:
            pass

        def create_item(
            self,
            label: str,
            attrs: dict[str, str],
            secret: bytes,
            *,
            replace: bool,
        ) -> Item:
            assert attrs == attributes
            assert replace is True
            assert PASSWORD not in label
            saved[:] = [secret]
            return Item()

        def search_items(self, attrs: dict[str, str]) -> list[Item]:
            assert attrs == attributes
            return [Item()] if saved else []

    monkeypatch.setattr(secretstorage, "dbus_init", Connection)
    monkeypatch.setattr(secretstorage, "get_default_collection", lambda _: Collection())
    for operation, expected in [("set", ""), ("get", PASSWORD), ("delete", "")]:
        request = json.dumps(
            {"operation": operation, "account": "test-vault-account", "password": PASSWORD}
        ).encode()
        output = io.StringIO()
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(request)))
        monkeypatch.setattr("sys.stdout", output)
        main()
        assert json.loads(output.getvalue()) == expected
    assert saved == []
    assert len(closed) == 3
