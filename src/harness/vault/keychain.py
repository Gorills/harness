"""Linux Secret Service only. No plaintext or general-purpose keyring fallback."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from harness.vault.store import VaultError, _write, read_private


class DeviceKey:
    def __init__(self, root: Path) -> None:
        self.path = root / "device-unlock.json"
        self.account = hashlib.sha256(str(root.resolve()).encode()).hexdigest()

    @property
    def available(self) -> bool:
        return sys.platform == "linux" and bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))

    def status(self) -> dict[str, bool]:
        enabled = False
        automatic = False
        if self.path.exists():
            try:
                value = json.loads(read_private(self.path))
                enabled = value == {"version": 1, "automatic": True} or value == {
                    "version": 1,
                    "automatic": False,
                }
                automatic = enabled and value["automatic"] is True
            except (ValueError, OSError, VaultError):
                # A damaged preference must never prevent manual KDBX recovery.
                enabled = automatic = False
        return {"available": self.available, "enabled": enabled, "automatic": automatic}

    def _set(self, automatic: bool) -> None:
        _write(self.path, json.dumps({"version": 1, "automatic": automatic}).encode(), replace=True)

    def _call(self, operation: str, password: str = "") -> str:
        if not self.available:
            raise VaultError("device_unavailable")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "harness.vault.keychain"],
                input=json.dumps(
                    {"operation": operation, "account": self.account, "password": password}
                ).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=45,
                check=True,
            )
            value = json.loads(result.stdout)
            if not isinstance(value, str) or len(value) > 1024:
                raise ValueError
            return value
        except (OSError, ValueError, subprocess.SubprocessError):
            raise VaultError("device_unavailable") from None

    def remember(self, password: str) -> None:
        self._call("set", password)
        self._set(True)

    def password(self, *, automatic: bool) -> str:
        state = self.status()
        if not state["enabled"] or (automatic and not state["automatic"]):
            raise VaultError("locked")
        return self._call("get")

    def pause(self) -> None:
        if self.status()["enabled"]:
            self._set(False)

    def resume(self) -> None:
        if self.status()["enabled"]:
            self._set(True)

    def forget(self) -> None:
        if self.status()["enabled"]:
            # Revoke automatic entry even if the desktop service is unavailable.
            self.pause()
            self._call("delete")
            self.path.unlink()


def main() -> None:
    # A bounded helper owns potentially blocking desktop prompts. No secrets in argv/logs.
    import secretstorage

    request = json.loads(sys.stdin.buffer.read(8192))
    attributes = {"application": "harness-private-vault", "vault": request["account"]}
    with closing(secretstorage.dbus_init()) as connection:
        collection = secretstorage.get_default_collection(connection)
        if collection.is_locked():
            collection.unlock()
        collection.ensure_not_locked()
        operation = request["operation"]
        if operation == "set":
            secret = request["password"].encode()
            item = collection.create_item(
                "Harness — личное хранилище", attributes, secret, replace=True
            )
            if item.get_secret() != secret:
                raise ValueError
            value = ""
        elif operation == "get":
            items = list(collection.search_items(attributes))
            if len(items) != 1:
                raise ValueError
            value = items[0].get_secret().decode()
        elif operation == "delete":
            for item in collection.search_items(attributes):
                item.delete()
            value = ""
        else:
            raise ValueError
        sys.stdout.write(json.dumps(value))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
