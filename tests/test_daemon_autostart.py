from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import harness.daemon_autostart as autostart
from harness.daemon_autostart import DaemonAutostartError, ensure_canonical_daemon
from harness.ipc import IpcTransportError, StatusResult
from harness.runtime_paths import InsecureRuntimeDirectoryError, RuntimePaths
from harness.storage import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon autostart slice")


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )


def _ready_status() -> StatusResult:
    return StatusResult(SCHEMA_VERSION, 0, 0)


def test_canonical_autostart_reuses_reachable_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)
    probes: list[Path] = []

    def request_status(socket_path: Path, *, timeout: float) -> StatusResult:
        probes.append(socket_path)
        assert timeout == autostart._DAEMON_PROBE_TIMEOUT_SECONDS
        return _ready_status()

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reachable canonical daemon must not be respawned")

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr(autostart.subprocess, "Popen", unexpected_spawn)

    ensure_canonical_daemon(paths)

    assert probes == [paths.socket]


def test_canonical_autostart_starts_package_module_when_runtime_directory_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    commands: list[list[str]] = []

    def spawn(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        assert kwargs == {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "start_new_session": True,
        }
        paths.socket.parent.mkdir(mode=0o700)
        paths.socket.parent.chmod(0o700)
        return object()

    monkeypatch.setattr(autostart.subprocess, "Popen", spawn)
    monkeypatch.setattr(autostart, "request_status", lambda *_args, **_kwargs: _ready_status())

    ensure_canonical_daemon(paths)

    assert commands == [[sys.executable, "-m", "harness.daemon_process"]]


def test_canonical_autostart_recovers_transport_unavailable_with_one_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)
    probe_count = 0
    spawn_count = 0

    def request_status(*_args: object, **_kwargs: object) -> StatusResult:
        nonlocal probe_count
        probe_count += 1
        if probe_count == 1:
            raise IpcTransportError("local IPC transport failed: connection refused")
        return _ready_status()

    def spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawn_count
        spawn_count += 1
        return object()

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr(autostart.subprocess, "Popen", spawn)

    ensure_canonical_daemon(paths)

    assert probe_count == 2
    assert spawn_count == 1


def test_canonical_autostart_rejects_existing_insecure_runtime_directory_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o755)
    paths.socket.parent.chmod(0o755)

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("insecure runtime directory must fail closed")

    monkeypatch.setattr(autostart.subprocess, "Popen", unexpected_spawn)

    with pytest.raises(InsecureRuntimeDirectoryError):
        ensure_canonical_daemon(paths)


def test_canonical_autostart_reports_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)

    def failed_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("process creation denied")

    monkeypatch.setattr(autostart.subprocess, "Popen", failed_spawn)

    with pytest.raises(DaemonAutostartError, match="could not be started"):
        ensure_canonical_daemon(paths)
