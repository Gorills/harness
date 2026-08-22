from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

import harness.daemon as daemon_module
import harness.entrypoints as entrypoints
from harness.daemon import serve_daemon
from harness.index import IndexingError, ScanDeadlineExceededError, list_indexed_files, scan_workspace
from harness.ipc import (
    IpcRemoteError,
    WorkspaceScanResult,
    request_workspace_scan,
    request_workspace_status,
)
from harness.registry import (
    WorkspaceRegistrationConflictError,
    create_project,
    list_workspaces,
    register_workspace,
    register_workspace_with_inferred_project,
)
from harness.runtime_paths import RuntimePaths
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX Workspace-scan IPC slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repository(base: Path, name: str = "repo") -> Path:
    root = base / name
    root.mkdir(parents=True)
    _git(root, "init")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
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
        "initial",
    )
    return root


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


def test_workspace_scan_registers_indexes_and_retries_idempotently(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        first = request_workspace_scan(socket_path, root.resolve())
        second = request_workspace_scan(socket_path, root.resolve())
        status = request_workspace_status(socket_path, [WorkspaceHint(root.resolve(), "test-root")])
    finally:
        _stop_server(stop_event, executor, future)

    assert first.schema_version == SCHEMA_VERSION
    assert first.workspace_root == root.resolve()
    assert first.file_count == 1
    assert (first.added, first.updated, first.removed) == (1, 0, 0)
    assert second.project_id == first.project_id
    assert second.workspace_id == first.workspace_id
    assert second.workspace_root == first.workspace_root
    assert (second.file_count, second.added, second.updated, second.removed) == (1, 0, 0, 0)
    assert status.project_id == first.project_id
    assert status.workspace_id == first.workspace_id
    assert status.indexed_file_count == 1

    connection = connect_database(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)
        assert len(list_indexed_files(connection, first.workspace_id)) == 1
    finally:
        connection.close()


def test_scan_registration_reuses_unique_project_for_linked_worktree(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked-scan", str(linked))
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        primary_project, primary = register_workspace_with_inferred_project(connection, path=root)
        linked_project, secondary = register_workspace_with_inferred_project(connection, path=linked)

        assert linked_project == primary_project
        assert secondary.project_id == primary.project_id
        assert secondary.workspace_id != primary.workspace_id
        assert secondary.git_common_dir == primary.git_common_dir
        assert set(list_workspaces(connection, project_id=primary.project_id)) == {
            primary,
            secondary,
        }
    finally:
        connection.close()


def test_scan_registration_fails_closed_when_shared_common_dir_has_multiple_projects(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    linked_one = tmp_path / "linked-one"
    linked_two = tmp_path / "linked-two"
    _git(root, "worktree", "add", "-b", "linked-one", str(linked_one))
    _git(root, "worktree", "add", "-b", "linked-two", str(linked_two))
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        first_project = create_project(connection)
        second_project = create_project(connection)
        register_workspace(connection, project_id=first_project.project_id, path=root)
        register_workspace(connection, project_id=second_project.project_id, path=linked_one)

        with pytest.raises(WorkspaceRegistrationConflictError, match="ambiguous"):
            register_workspace_with_inferred_project(connection, path=linked_two)

        assert len(list_workspaces(connection)) == 2
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (2,)
    finally:
        connection.close()


def test_workspace_scan_failure_retains_valid_registration_for_safe_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"

    def failed_scan(*_args: object, **_kwargs: object) -> object:
        raise IndexingError("fixture indexing failure")

    monkeypatch.setattr(daemon_module, "scan_workspace", failed_scan)
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with pytest.raises(IpcRemoteError) as exc_info:
            request_workspace_scan(socket_path, root.resolve())
        assert exc_info.value.code == "workspace_scan_error"
        assert "registration is retained and retry is safe" in exc_info.value.message
    finally:
        _stop_server(stop_event, executor, future)

    connection = connect_database(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
        registered = list_workspaces(connection)
        assert len(registered) == 1
        assert registered[0].workspace_root == root.resolve()
        assert list_indexed_files(connection, registered[0].workspace_id) == ()
    finally:
        connection.close()


def test_scan_workspace_deadline_rolls_back_before_index_mutation(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project, workspace = register_workspace_with_inferred_project(connection, path=root)
        assert project.project_id == workspace.project_id

        with pytest.raises(ScanDeadlineExceededError, match="deadline exceeded"):
            scan_workspace(connection, workspace.workspace_id, deadline=0.0)

        assert list_indexed_files(connection, workspace.workspace_id) == ()
    finally:
        connection.close()


def test_scan_git_enumeration_uses_remaining_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    _project, workspace = register_workspace_with_inferred_project(connection, path=root)
    original_run = subprocess.run
    seen_timeout: list[float | None] = []

    def timeout_git(*args: object, **kwargs: object) -> object:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and command[:2] == ["git", "ls-files"]:
            timeout = kwargs.get("timeout")
            seen_timeout.append(timeout if isinstance(timeout, float) else None)
            raise subprocess.TimeoutExpired(command, timeout if isinstance(timeout, float) else 0)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout_git)
    try:
        with pytest.raises(ScanDeadlineExceededError, match="deadline exceeded"):
            scan_workspace(
                connection,
                workspace.workspace_id,
                deadline=time.monotonic() + 5,
            )
        assert seen_timeout and seen_timeout[0] is not None and seen_timeout[0] > 0
        assert list_indexed_files(connection, workspace.workspace_id) == ()
    finally:
        connection.close()


def test_harness_scan_dispatches_canonical_location_and_prints_bounded_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repository(tmp_path)
    nested = root / "src" / "package"
    nested.mkdir(parents=True)
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o700)
    runtime_directory.chmod(0o700)
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=runtime_directory / "harness.sock",
    )
    seen: list[tuple[Path, Path]] = []

    def request_scan(socket_path: Path, workspace_path: Path) -> WorkspaceScanResult:
        seen.append((socket_path, workspace_path))
        return WorkspaceScanResult(
            schema_version=SCHEMA_VERSION,
            workspace_id="workspace-1",
            project_id="project-1",
            workspace_root=root.resolve(),
            file_count=7,
            added=2,
            updated=3,
            removed=1,
        )

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(nested)])
    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)

    assert entrypoints.harness_main() == 0
    assert seen == [(defaults.socket, nested.resolve())]
    assert capsys.readouterr().out.splitlines() == [
        "Project: project-1",
        "Workspace: workspace-1",
        f"Workspace root: {root.resolve()}",
        "Indexed files: 7",
        "Added: 2",
        "Updated: 3",
        "Removed: 1",
        f"Schema: {SCHEMA_VERSION}",
    ]
