import os
from pathlib import Path
from threading import Event

import pytest

import harness.entrypoints as entrypoints
from harness.daemon import InsecureSocketDirectoryError, serve_daemon
from harness.ipc import WorkspaceStatusResult
from harness.runtime_paths import (
    InsecureRuntimeDirectoryError,
    InsecureStateDirectoryError,
    RuntimePaths,
    ensure_private_state_directory,
    require_private_runtime_directory,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX runtime-path slice")


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def test_canonical_state_directory_rejects_symlink_to_private_directory(tmp_path: Path) -> None:
    target = tmp_path / "state-target"
    _private_directory(target)
    state_directory = tmp_path / "state-link"
    state_directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(InsecureStateDirectoryError, match="real directory"):
        ensure_private_state_directory(state_directory)


def test_canonical_runtime_directory_rejects_symlink_to_private_directory(tmp_path: Path) -> None:
    target = tmp_path / "runtime-target"
    _private_directory(target)
    runtime_directory = tmp_path / "runtime-link"
    runtime_directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(InsecureRuntimeDirectoryError, match="real directory"):
        require_private_runtime_directory(runtime_directory)


def test_status_validates_canonical_runtime_directory_before_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    target = tmp_path / "runtime-target"
    _private_directory(target)
    runtime_directory = tmp_path / "runtime-link"
    runtime_directory.symlink_to(target, target_is_directory=True)
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=runtime_directory / "harness.sock",
    )

    def unexpected_request(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise AssertionError("IPC must not trust a symlinked canonical runtime directory")

    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_status", unexpected_request)

    assert entrypoints._run_status(workspace_root, None) == 1
    assert capsys.readouterr().out.strip() == (
        "Harness status: FAIL (Harness runtime directory must be owned by the current user, "
        "be a real directory, and have no group/other access)"
    )


def test_daemon_rejects_symlink_socket_parent_before_bind(tmp_path: Path) -> None:
    target = tmp_path / "socket-target"
    _private_directory(target)
    socket_directory = tmp_path / "socket-link"
    socket_directory.symlink_to(target, target_is_directory=True)
    stop_event = Event()
    stop_event.set()

    with pytest.raises(InsecureSocketDirectoryError, match="real directory"):
        serve_daemon(
            tmp_path / "harness.db",
            socket_directory / "harness.sock",
            stop_event=stop_event,
        )

    assert not (target / "harness.sock").exists()
