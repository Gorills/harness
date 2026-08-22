import sys
from pathlib import Path

import pytest

import harness.entrypoints as entrypoints
from harness.entrypoints import harness_main
from harness.ipc import IpcTransportError, WorkspaceScanResult
from harness.runtime_paths import RuntimePaths


def test_harness_scan_resolves_location_and_prints_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    location = workspace_root / "src"
    location.mkdir(parents=True)
    socket_path = tmp_path / "ipc" / "harness.sock"
    seen: list[tuple[Path, Path]] = []

    def request_scan(ipc_socket: Path, path: Path) -> WorkspaceScanResult:
        seen.append((ipc_socket, path))
        return WorkspaceScanResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            project_created=True,
            workspace_created=True,
            file_count=7,
            added=7,
            updated=0,
            removed=0,
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "scan", str(location), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)

    assert harness_main() == 0
    assert seen == [(socket_path, location.resolve())]
    assert capsys.readouterr().out.splitlines() == [
        "Project: project-1 (created)",
        "Workspace: workspace-1 (created)",
        f"Workspace root: {workspace_root.resolve()}",
        "Visibility: normal",
        "Indexed files: 7",
        "Added: 7",
        "Updated: 0",
        "Removed: 0",
        "Schema: 3",
    ]


def test_harness_scan_uses_canonical_socket_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    defaults.socket.parent.mkdir(mode=0o700)
    defaults.socket.parent.chmod(0o700)
    seen_sockets: list[Path] = []

    def request_scan(ipc_socket: Path, _path: Path) -> WorkspaceScanResult:
        seen_sockets.append(ipc_socket)
        return WorkspaceScanResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            project_created=False,
            workspace_created=False,
            file_count=0,
            added=0,
            updated=0,
            removed=0,
        )

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(workspace_root)])
    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)

    assert harness_main() == 0
    assert seen_sockets == [defaults.socket]


def test_harness_scan_rejects_non_directory_before_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_file = tmp_path / "file.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")
    socket_path = tmp_path / "harness.sock"

    def unexpected_request(*_args: object, **_kwargs: object) -> WorkspaceScanResult:
        raise AssertionError("IPC should not be called for a non-directory location")

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "scan", str(workspace_file), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_scan", unexpected_request)

    assert harness_main() == 1
    assert capsys.readouterr().out.strip() == (
        f"Harness scan: FAIL (workspace path is not a directory: {workspace_file.resolve()})"
    )


def test_harness_scan_bounds_multiline_ipc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "harness.sock"

    def failed_request(*_args: object, **_kwargs: object) -> WorkspaceScanResult:
        raise IpcTransportError("first\nsecond\r" + "x" * 2000)

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "scan", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_scan", failed_request)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    line = output.rstrip("\n")
    assert "\n" not in line
    assert "\r" not in line
    assert line.startswith("Harness scan: FAIL (first\\nsecond\\r")
    assert line.endswith("...)")
    assert len(line) == len("Harness scan: FAIL ()") + 1024
