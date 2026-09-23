"""Launch-only bridge. Never imports vault storage, keys, credentials or records."""

from __future__ import annotations

import select
import subprocess
import sys
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit


class VaultProcess:
    def __init__(self, database_path: Path) -> None:
        base = database_path.parent
        if base.name == "harness":
            base = base.parent
        self.root = base / "harness-vault"
        self._process: subprocess.Popen[bytes] | None = None
        self._origin = ""
        self._lock = RLock()
        self._closed = False

    def origin(self, parent_origin: str) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("vault_unavailable")
            if self._process is not None and self._process.poll() is None:
                return self._origin
            self._stop()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "harness.vault",
                    "--root",
                    str(self.root),
                    "--parent-origin",
                    parent_origin,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            self._process = process
            assert process.stdout is not None
            readable, _, _ = select.select([process.stdout], [], [], 5)
            origin = process.stdout.readline(128).decode("ascii").strip() if readable else ""
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
                self._stop()
                raise RuntimeError("vault_unavailable")
            self._origin = origin
            return origin

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop()

    def _stop(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._origin = ""
            if process is None:
                return
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
