from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from typing import cast

import pytest

from harness.daemon import serve_daemon
from harness.index import ScanDeadlineExceededError, list_indexed_files, scan_workspace
from harness.ipc import (
    PROTOCOL_VERSION,
    IpcRemoteError,
    StatusResult,
    WorkspaceScanResult,
    request_status,
    request_workspace_scan,
)
from harness.registry import (
    create_project,
    register_workspace,
    register_workspace_for_scan,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon scan slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(
        path,
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
    return path


def _start_server(
    database: Path,
    socket_path: Path,
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop_event)
    deadline = monotonic() + 3
    while not socket_path.exists():
        if future.done():
            future.result()
        if monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon socket did not appear")
        time.sleep(0.01)
    return stop_event, executor, future


def _stop_server(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def _raw_request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(socket_path))
        client.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            response.extend(client.recv(4096))
    value: object = json.loads(response.decode("utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_scan_registration_creates_then_reuses_project_and_workspace(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        first = register_workspace_for_scan(connection, path=root)
        second = register_workspace_for_scan(connection, path=root / ".")

        assert first.project_created is True
        assert first.workspace_created is True
        assert second.project_created is False
        assert second.workspace_created is False
        assert second.project == first.project
        assert second.workspace == first.workspace
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)
    finally:
        connection.close()


def test_scan_registration_reuses_project_for_linked_worktree(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked-scan", str(linked))
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        primary = register_workspace_for_scan(connection, path=root)
        secondary = register_workspace_for_scan(connection, path=linked)

        assert primary.project_created is True
        assert secondary.project_created is False
        assert secondary.workspace_created is True
        assert secondary.project.project_id == primary.project.project_id
        assert secondary.workspace.workspace_id != primary.workspace.workspace_id
        assert secondary.workspace.git_common_dir == primary.workspace.git_common_dir
    finally:
        connection.close()


def test_scan_workspace_expired_deadline_is_non_mutating(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)

        with pytest.raises(ScanDeadlineExceededError, match="deadline exceeded"):
            scan_workspace(connection, workspace.workspace_id, deadline=monotonic() - 1)

        assert list_indexed_files(connection, workspace.workspace_id) == ()
    finally:
        connection.close()


def test_daemon_scan_registers_indexes_and_returns_exact_wire_contract(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        result = request_workspace_scan(socket_path, root)
        assert isinstance(result, WorkspaceScanResult)
        assert result.schema_version == SCHEMA_VERSION
        assert result.workspace_root == root.resolve()
        assert result.visibility_mode == "normal"
        assert result.project_created is True
        assert result.workspace_created is True
        assert result.file_count == 1
        assert result.added == 1
        assert result.updated == 0
        assert result.removed == 0

        response = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "scan-exact",
                "method": "scan_workspace",
                "params": {"path": str(root.resolve())},
            },
        )
        assert response == {
            "version": PROTOCOL_VERSION,
            "request_id": "scan-exact",
            "ok": True,
            "result": {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": result.workspace_id,
                "project_id": result.project_id,
                "visibility_mode": "normal",
                "workspace_root": str(root.resolve()),
                "project_created": False,
                "workspace_created": False,
                "file_count": 1,
                "added": 0,
                "updated": 0,
                "removed": 0,
            },
        }
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 1, 1)
    finally:
        _stop_server(stop_event, executor, future)


def test_daemon_scan_rejects_relative_path_and_recovers(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        response = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "bad-scan",
                "method": "scan_workspace",
                "params": {"path": "relative"},
            },
        )
        assert response == {
            "version": PROTOCOL_VERSION,
            "request_id": None,
            "ok": False,
            "error": {"code": "invalid_request", "message": "IPC request is invalid"},
        }
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_daemon_scan_reports_git_failure_without_stopping_daemon(tmp_path: Path) -> None:
    not_a_repository = tmp_path / "plain"
    not_a_repository.mkdir()
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with pytest.raises(IpcRemoteError) as exc_info:
            request_workspace_scan(socket_path, not_a_repository)
        assert exc_info.value.code == "workspace_git_error"
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)
