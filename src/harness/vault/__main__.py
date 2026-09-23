from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import resource
import secrets
import select
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic
from typing import ClassVar

from harness.vault.assets import VAULT_CSS, VAULT_HTML, VAULT_JS
from harness.vault.keychain import DeviceKey
from harness.vault.store import MAX_FILE_BYTES, VaultError, VaultStore, private_directory

IDLE_SECONDS = 15 * 60
MAX_REQUEST_BYTES = (MAX_FILE_BYTES * 4 // 3) + 65536


class VaultApplication:
    def __init__(self, root: Path) -> None:
        self.store = VaultStore(root)
        self.device = DeviceKey(root)
        self.token = ""
        self.last_use = 0.0
        self.unlock_after = 0.0
        self.no_password = False
        if self.store.path.exists():
            try:
                self.store.unlock("")
                self.no_password = True
            except (VaultError, OSError):
                # Protected or damaged files retain manual entry/recovery.
                pass

    def lock(self, *, pause_device: bool = True) -> None:
        self.token = ""
        self.store.lock()
        if pause_device:
            self.device.pause()

    def state(self) -> dict[str, object]:
        value = self.store.state()
        assert self.store.database is not None
        self.no_password = self.store.database.password == ""
        return {
            **value,
            "device": self.device.status(),
            "access_mode": "no_password" if self.no_password else "password",
        }

    def expire(self) -> None:
        if self.token and not self.no_password and monotonic() - self.last_use >= IDLE_SECONDS:
            self.lock()

    def call(self, action: str, data: dict[str, object], token: str) -> dict[str, object]:
        self.expire()
        if action == "status":
            return {
                "exists": self.store.path.exists(),
                "device": self.device.status(),
                "access_mode": "no_password" if self.no_password else "password",
            }
        if action == "open_without_password":
            # Always verify the file itself, never trust a preference flag to bypass a password.
            if not self.no_password:
                raise VaultError("password_required")
            try:
                self.store.unlock("")
            except VaultError:
                raise VaultError("password_required") from None
            self.no_password = True
            if not self.token:
                self.token = secrets.token_urlsafe(32)
            self.last_use = monotonic()
            return {"token": self.token, **self.state()}
        if action in {"unlock", "device_unlock", "create", "initial_restore"}:
            if monotonic() < self.unlock_after:
                raise VaultError("try_later")
            self.unlock_after = monotonic() + 2
            password = (
                self.device.password(automatic=data.get("automatic") is True)
                if action == "device_unlock"
                else ""
                if action == "create" and data.get("access_mode") == "no_password"
                else _text(data, "password")
            )
            if action in {"unlock", "device_unlock"}:
                self.store.unlock(password)
            elif action == "create":
                self.store.create(
                    password,
                    _text(data, "folder"),
                    no_password=data.get("access_mode") == "no_password",
                )
            else:
                self.device.forget()
                self.store.recover(_backup_bytes(data), password, _text(data, "folder"))
            self.token = secrets.token_urlsafe(32)
            self.last_use = monotonic()
            self.no_password = password == ""
            if not self.no_password:
                self.device.resume()
            device_error = ""
            if data.get("remember") is True and not self.no_password:
                try:
                    self.device.remember(password)
                except VaultError as exc:
                    device_error = str(exc)
            return {"token": self.token, "device_error": device_error, **self.state()}
        if not token or not self.token or not secrets.compare_digest(token, self.token):
            raise VaultError("locked")
        self.last_use = monotonic()
        if action == "lock":
            self.lock()
            return {}
        if action == "touch":
            return {}
        if action == "state":
            return self.state()
        if action == "device_forget":
            self.device.forget()
            return self.state()
        if action == "device_remember":
            if self.no_password:
                raise VaultError("invalid_request")
            assert self.store.database is not None
            self.device.remember(str(self.store.database.password))
            return self.state()
        if action == "detail":
            return {"record": self.store.detail(_text(data, "id"))}
        was_no_password = self.no_password
        expected = _text(data, "revision")
        if action == "save":
            self.store.save_record(_text(data, "id"), data.get("record"), expected)
        elif action == "delete":
            self.store.delete_record(_text(data, "id"), expected)
        elif action == "backup":
            self.store.backup(_text(data, "folder"), expected)
        elif action == "restore":
            self.device.forget()
            self.store.restore(
                _backup_bytes(data), _text(data, "password"), _text(data, "folder"), expected
            )
        elif action == "password":
            self.device.forget()
            self.store.change_password(_text(data, "password"), expected)
        elif action == "disable_password":
            self.store.disable_password(expected)
            self.no_password = True
            # Desktop availability must not block this mode. A previously enrolled key
            # can still be explicitly removed via device_forget; it is no longer used.
            try:
                self.device.pause()
            except (OSError, VaultError):
                pass
        else:
            raise VaultError("invalid_request")
        state = self.state()
        if was_no_password and not self.no_password:
            # Tokens obtainable without a password cannot survive enabling protection.
            self.token = secrets.token_urlsafe(32)
            return {"token": self.token, **state}
        return state


def _text(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise VaultError("invalid_request")
    return value


def _backup_bytes(data: dict[str, object]) -> bytes:
    try:
        value = base64.b64decode(_text(data, "file"), validate=True)
    except ValueError:
        raise VaultError("invalid_backup") from None
    if len(value) > MAX_FILE_BYTES:
        raise VaultError("too_large")
    return value


class Handler(BaseHTTPRequestHandler):
    app: ClassVar[VaultApplication]
    origin: ClassVar[str]
    parent_origin: ClassVar[str]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def _valid_host(self) -> bool:
        return self.headers.get_all("Host") == [self.origin.removeprefix("http://")]

    def do_GET(self) -> None:
        if not self._valid_host():
            self._send(404, b"", "text/plain")
            return
        assets = {
            "/": (VAULT_HTML, "text/html; charset=utf-8"),
            "/vault.css": (VAULT_CSS, "text/css; charset=utf-8"),
            "/vault.js": (VAULT_JS, "text/javascript; charset=utf-8"),
        }
        asset = assets.get(self.path)
        if asset is None:
            self._send(404, b"", "text/plain")
        else:
            self._send(200, asset[0].encode(), asset[1])

    def do_POST(self) -> None:
        if (
            not self._valid_host()
            or self.headers.get_all("Origin") != [self.origin]
            or self.headers.get_all("Content-Type") != ["application/json"]
            or self.headers.get("Transfer-Encoding") is not None
            or self.path != "/api"
        ):
            self._send(403, b"{}", "application/json")
            return
        try:
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise VaultError("invalid_request")
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise VaultError("too_large")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise VaultError("invalid_request")
            result = self.app.call(
                _text(data, "action"),
                data,
                self.headers.get("Authorization", "").removeprefix("Bearer "),
            )
            self._json(200, result)
        except VaultError as exc:
            self._json(401 if str(exc) == "locked" else 400, {"error": str(exc)})
        except (ValueError, RecursionError, UnicodeError):
            self._json(400, {"error": "invalid_request"})
        except Exception:
            self.app.lock()
            self._json(503, {"error": "storage_failed"})

    def _json(self, status: int, value: dict[str, object]) -> None:
        self._send(status, json.dumps(value).encode(), "application/json")

    def _send(self, status: int, value: bytes, content_type: str) -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(value)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; form-action 'none'; "
            f"base-uri 'none'; frame-ancestors {self.parent_origin}",
        )
        self.end_headers()
        self.wfile.write(value)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._send(code, b"", "text/plain")

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--parent-origin", required=True)
    args = parser.parse_args()
    # Import parsing cannot take down the daemon; bound this separate process.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    private_directory(args.root)
    lock_fd = os.open(args.root / "vault.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Handler.app = VaultApplication(args.root)
        Handler.parent_origin = args.parent_origin
        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            Handler.origin = f"http://127.0.0.1:{server.server_port}"
            server.timeout = 0.2
            print(Handler.origin, flush=True)
            while True:
                # Parent exit closes stdin, including abrupt daemon termination.
                readable, _, _ = select.select([sys.stdin], [], [], 0)
                if readable and not os.read(sys.stdin.fileno(), 1):
                    break
                Handler.app.expire()
                server.handle_request()
        Handler.app.lock(pause_device=False)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
