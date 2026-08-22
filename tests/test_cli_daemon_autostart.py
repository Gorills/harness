from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

import harness.entrypoints as entrypoints
from harness.ipc import (
    IpcRemoteError,
    IpcTransportError,
    WorkspaceScanResult,
    WorkspaceStatusResult,
)
from harness.runtime_paths import RuntimePaths
from harness.storage import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon autostart slice")


def _transport_error(error_number: int) -> IpcTransportError:
    try:
        raise IpcTransportError("local IPC transport failed") from OSError(
            error_number, "transport"
        )
    except IpcTransportError as exc:
        return exc


def _status(workspace_root: Path) -> WorkspaceStatusResult:
    return WorkspaceStatusResult(
        schema_version=SCHEMA_VERSION,
        workspace_id="workspace-1",
        project_id="project-1",
        visibility_mode="normal",
        workspace_root=workspace_root.resolve(),
        head=None,
        branch="main",
        dirty_path_count=0,
        indexed_file_count=0,
    )


def _unexpected_autostart(_path: Path) -> None:
    raise AssertionError("daemon autostart was not expected")


def test_status_autostarts_canonical_daemon_once_and_retries_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    requests = 0
    starts: list[Path] = []

    def request_status(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise _transport_error(errno.ENOENT)
        return _status(workspace_root)

    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_status", request_status)
    monkeypatch.setattr(entrypoints, "start_canonical_daemon", starts.append)

    assert entrypoints._run_status(workspace_root, None) == 0
    assert requests == 2
    assert starts == [defaults.socket]
    assert defaults.socket.parent.is_dir()
    assert stat.S_IMODE(defaults.socket.parent.stat().st_mode) & 0o077 == 0


def test_status_does_not_autostart_explicit_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "manual.sock"

    def unavailable_status(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise _transport_error(errno.ENOENT)

    monkeypatch.setattr(entrypoints, "request_workspace_status", unavailable_status)
    monkeypatch.setattr(entrypoints, "start_canonical_daemon", _unexpected_autostart)

    assert entrypoints._run_status(workspace_root, socket_path) == 1
    assert "Harness status: FAIL (local IPC transport failed)" in capsys.readouterr().out


def test_status_does_not_autostart_ambiguous_canonical_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )

    def denied_status(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise _transport_error(errno.EACCES)

    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_status", denied_status)
    monkeypatch.setattr(entrypoints, "start_canonical_daemon", _unexpected_autostart)

    assert entrypoints._run_status(workspace_root, None) == 1
    assert "Harness status: FAIL (local IPC transport failed)" in capsys.readouterr().out


def test_status_does_not_autostart_daemon_reported_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )

    def remote_failure(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise IpcRemoteError("database_error", "daemon could not read status")

    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_status", remote_failure)
    monkeypatch.setattr(entrypoints, "start_canonical_daemon", _unexpected_autostart)

    assert entrypoints._run_status(workspace_root, None) == 1
    assert "database_error: daemon could not read status" in capsys.readouterr().out


def test_scan_autostarts_canonical_daemon_once_and_retries_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    requests = 0
    starts: list[Path] = []

    def request_scan(_socket: Path, path: Path) -> WorkspaceScanResult:
        nonlocal requests
        assert path == workspace_root.resolve()
        requests += 1
        if requests == 1:
            raise _transport_error(errno.ECONNREFUSED)
        return WorkspaceScanResult(
            schema_version=SCHEMA_VERSION,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            project_created=True,
            workspace_created=True,
            file_count=0,
            added=0,
            updated=0,
            removed=0,
        )

    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)
    monkeypatch.setattr(entrypoints, "start_canonical_daemon", starts.append)

    assert entrypoints._run_scan(workspace_root, None) == 0
    assert requests == 2
    assert starts == [defaults.socket]
