import sys
from pathlib import Path

import pytest

import harness.entrypoints as entrypoints
from harness.entrypoints import harness_main
from harness.index import IndexedFileKind
from harness.ipc import (
    IpcTransportError,
    WorkspaceSearchHit,
    WorkspaceSearchResult,
)
from harness.runtime_paths import RuntimePaths
from harness.search import SearchMatchKind
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode


def _result(workspace_root: Path) -> WorkspaceSearchResult:
    return WorkspaceSearchResult(
        schema_version=3,
        workspace_id="workspace-1",
        project_id="project-1",
        workspace_root=workspace_root.resolve(),
        results=(
            WorkspaceSearchHit(
                relative_path="src/weird\nname.py",
                kind=IndexedFileKind.FILE,
                size_bytes=12,
                match_kind=SearchMatchKind.EXACT_FILENAME,
            ),
        ),
    )


def test_harness_search_resolves_location_passes_limit_and_escapes_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    location = workspace_root / "src"
    location.mkdir(parents=True)
    socket_path = tmp_path / "ipc" / "harness.sock"
    seen: list[tuple[Path, tuple[WorkspaceHint, ...], str, int]] = []

    def request_search(
        ipc_socket: Path,
        hints: list[WorkspaceHint],
        query: str,
        *,
        limit: int,
    ) -> WorkspaceSearchResult:
        seen.append((ipc_socket, tuple(hints), query, limit))
        return _result(workspace_root)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harness",
            "search",
            "weird name",
            str(location),
            "--limit",
            "7",
            "--socket",
            str(socket_path),
        ],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_search", request_search)

    assert harness_main() == 0
    assert seen == [
        (
            socket_path,
            (
                WorkspaceHint(
                    path=location.resolve(),
                    source="cli-location",
                    match_mode=WorkspaceHintMatchMode.LOCATION,
                ),
            ),
            "weird name",
            7,
        )
    ]
    assert capsys.readouterr().out.splitlines() == [
        "Project: project-1",
        "Workspace: workspace-1",
        f"Workspace root: {workspace_root.resolve()}",
        "Matches: 1",
        '"src/weird\\nname.py"\tfile\t12\texact_filename',
        "Schema: 3",
    ]


def test_harness_search_uses_canonical_socket_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    autostarted: list[RuntimePaths] = []
    seen_sockets: list[Path] = []

    def request_search(
        ipc_socket: Path,
        _hints: list[WorkspaceHint],
        _query: str,
        *,
        limit: int,
    ) -> WorkspaceSearchResult:
        assert limit == 10
        seen_sockets.append(ipc_socket)
        return WorkspaceSearchResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            workspace_root=workspace_root.resolve(),
            results=(),
        )

    monkeypatch.setattr(sys, "argv", ["harness", "search", "token", str(workspace_root)])
    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "ensure_canonical_daemon", autostarted.append)
    monkeypatch.setattr(entrypoints, "request_workspace_search", request_search)

    assert harness_main() == 0
    assert autostarted == [defaults]
    assert seen_sockets == [defaults.socket]


def test_harness_search_explicit_socket_does_not_autostart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "manual.sock"

    def unexpected_autostart(_paths: RuntimePaths) -> None:
        raise AssertionError("explicit socket must not autostart the canonical daemon")

    def request_search(
        _ipc_socket: Path,
        _hints: list[WorkspaceHint],
        _query: str,
        *,
        limit: int,
    ) -> WorkspaceSearchResult:
        assert limit == 10
        return WorkspaceSearchResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            workspace_root=workspace_root.resolve(),
            results=(),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "search", "token", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "ensure_canonical_daemon", unexpected_autostart)
    monkeypatch.setattr(entrypoints, "request_workspace_search", request_search)

    assert harness_main() == 0


def test_harness_search_rejects_non_directory_before_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_file = tmp_path / "file.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")
    socket_path = tmp_path / "harness.sock"

    def unexpected_request(*_args: object, **_kwargs: object) -> WorkspaceSearchResult:
        raise AssertionError("IPC should not be called for a non-directory location")

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "search", "token", str(workspace_file), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_search", unexpected_request)

    assert harness_main() == 1
    assert capsys.readouterr().out.strip() == (
        f"Harness search: FAIL (workspace path is not a directory: {workspace_file.resolve()})"
    )


def test_harness_search_bounds_multiline_ipc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "harness.sock"

    def failed_request(*_args: object, **_kwargs: object) -> WorkspaceSearchResult:
        raise IpcTransportError("first\nsecond\r" + "x" * 2000)

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "search", "token", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_search", failed_request)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    line = output.rstrip("\n")
    assert "\n" not in line
    assert "\r" not in line
    assert line.startswith("Harness search: FAIL (first\\nsecond\\r")
    assert line.endswith("...)")
    assert len(line) == len("Harness search: FAIL ()") + 1024
