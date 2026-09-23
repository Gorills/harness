from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.daemon import serve_daemon
from harness.index import IndexedFileKind, scan_workspace
from harness.ipc import (
    IpcRemoteError,
    WorkspaceIndexEntryResult,
    request_workspace_index_entry,
)
from harness.registry import create_project, register_workspace
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX IPC slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _registered_workspace_database(
    tmp_path: Path,
    *,
    files: dict[str, str] | None = None,
) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    source_files = files or {
        "src/rotateRefreshToken.py": "TOKEN = 1\n",
        "tests/rotate_refresh_token_test.py": "def test_token(): pass\n",
    }
    for relative_path, content in source_files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        return root, database, project.project_id, workspace.workspace_id
    finally:
        connection.close()


def _start_server(
    database: Path,
    socket_path: Path,
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop_event)
    deadline = time.monotonic() + 3
    while not socket_path.exists():
        if future.done():
            future.result()
        if time.monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon socket did not appear")
        time.sleep(0.01)
    return stop_event, executor, future


def _stop_server(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def test_workspace_index_entry_round_trip_supports_long_exact_refs(tmp_path: Path) -> None:
    first = "a" * 140
    second = "b" * 140
    relative_path = f"{first}/{second}/token_service.py"
    root, database, project_id, workspace_id = _registered_workspace_database(
        tmp_path,
        files={relative_path: "TOKEN = 1\n"},
    )
    assert len(relative_path.encode("utf-8")) > 256
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        result = request_workspace_index_entry(
            socket_path,
            [WorkspaceHint(root.resolve(), "explicit-root")],
            relative_path,
        )
        assert result == WorkspaceIndexEntryResult(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace_id,
            project_id=project_id,
            workspace_root=root.resolve(),
            relative_path=relative_path,
            kind=IndexedFileKind.FILE,
            size_bytes=(root / relative_path).stat().st_size,
        )
        assert "content_sha256" not in repr(result)
    finally:
        _stop_server(stop_event, executor, future)


def test_workspace_index_entry_missing_and_corrupt_rows_fail_distinctly(tmp_path: Path) -> None:
    root, database, _project_id, _workspace_id = _registered_workspace_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with pytest.raises(IpcRemoteError) as exc_info:
            request_workspace_index_entry(
                socket_path,
                [WorkspaceHint(root.resolve(), "cwd", WorkspaceHintMatchMode.LOCATION)],
                "missing.py",
            )
        assert exc_info.value.code == "index_entry_not_found"
    finally:
        _stop_server(stop_event, executor, future)

    connection = connect_database(database)
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE indexed_files SET kind = 'corrupt' WHERE relative_path = ?",
            ("src/rotateRefreshToken.py",),
        )
    finally:
        connection.close()

    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with pytest.raises(IpcRemoteError) as exc_info:
            request_workspace_index_entry(
                socket_path,
                [WorkspaceHint(root.resolve(), "cwd", WorkspaceHintMatchMode.LOCATION)],
                "src/rotateRefreshToken.py",
            )
        assert exc_info.value.code == "index_error"
        assert "unsupported kind" in exc_info.value.message
    finally:
        _stop_server(stop_event, executor, future)
