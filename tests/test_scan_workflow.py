from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest

import harness.entrypoints as entrypoints
from harness.daemon import serve_daemon
from harness.ipc import (
    PROTOCOL_VERSION,
    StatusResult,
    WorkspaceScanResult,
    request_status,
    request_workspace_scan,
)
from harness.registry import (
    WorkspaceRegistrationConflictError,
    create_project,
    ensure_workspace_registration,
    list_workspaces,
    register_workspace,
)
from harness.scan import scan_path
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX IPC scan slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _initialize_repository(base: Path) -> Path:
    repository = base / "repository"
    repository.mkdir()
    _git(repository, "init")
    (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=harness@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "initial",
    )
    return repository


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


def _raw_request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        client.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            response.extend(client.recv(4096))
    value: object = json.loads(response.decode("utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_scan_path_registers_once_and_reconciles_existing_workspace(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        first = scan_path(connection, repository)
        assert first.project_created is True
        assert first.workspace_created is True
        assert first.file_count == 1
        assert first.added == 1
        assert first.updated == 0
        assert first.removed == 0

        (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (repository / "new.txt").write_text("new\n", encoding="utf-8")
        second = scan_path(connection, repository / ".")

        assert second.project_id == first.project_id
        assert second.workspace_id == first.workspace_id
        assert second.project_created is False
        assert second.workspace_created is False
        assert second.file_count == 2
        assert second.added == 1
        assert second.updated == 1
        assert second.removed == 0
        assert list_workspaces(connection) == (
            ensure_workspace_registration(connection, path=repository).workspace,
        )
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
    finally:
        connection.close()


def test_scan_reuses_project_for_linked_worktree(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    linked = tmp_path / "linked"
    _git(repository, "worktree", "add", "-b", "linked-scan", str(linked))
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        primary = scan_path(connection, repository)
        secondary = scan_path(connection, linked)

        assert secondary.project_id == primary.project_id
        assert secondary.workspace_id != primary.workspace_id
        assert secondary.project_created is False
        assert secondary.workspace_created is True
        assert len(list_workspaces(connection, project_id=primary.project_id)) == 2
    finally:
        connection.close()


def test_automatic_registration_fails_closed_for_ambiguous_common_dir(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    linked_one = tmp_path / "linked-one"
    linked_two = tmp_path / "linked-two"
    _git(repository, "worktree", "add", "-b", "linked-one", str(linked_one))
    _git(repository, "worktree", "add", "-b", "linked-two", str(linked_two))
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        first_project = create_project(connection)
        second_project = create_project(connection)
        register_workspace(connection, project_id=first_project.project_id, path=repository)
        register_workspace(connection, project_id=second_project.project_id, path=linked_one)

        with pytest.raises(
            WorkspaceRegistrationConflictError,
            match="automatic Project selection is ambiguous",
        ):
            ensure_workspace_registration(connection, path=linked_two)

        assert len(list_workspaces(connection)) == 2
    finally:
        connection.close()


def test_workspace_scan_ipc_round_trip_registers_and_indexes(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    nested = repository / "src" / "package"
    nested.mkdir(parents=True)
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        result = request_workspace_scan(socket_path, nested.resolve())

        assert result.schema_version == SCHEMA_VERSION
        assert result.visibility_mode == "normal"
        assert result.workspace_root == repository.resolve()
        assert result.project_created is True
        assert result.workspace_created is True
        assert result.file_count == 1
        assert result.added == 1
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 1, 1)

        raw_response = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "scan-exact",
                "method": "workspace_scan",
                "params": {"path": str(repository.resolve())},
            },
        )
        assert raw_response == {
            "version": PROTOCOL_VERSION,
            "request_id": "scan-exact",
            "ok": True,
            "result": {
                "schema_version": SCHEMA_VERSION,
                "project_id": result.project_id,
                "workspace_id": result.workspace_id,
                "visibility_mode": "normal",
                "workspace_root": str(repository.resolve()),
                "project_created": False,
                "workspace_created": False,
                "file_count": 1,
                "added": 0,
                "updated": 0,
                "removed": 0,
            },
        }

        again = request_workspace_scan(socket_path, repository.resolve())
        assert again.project_id == result.project_id
        assert again.workspace_id == result.workspace_id
        assert again.project_created is False
        assert again.workspace_created is False
        assert again.added == 0
        assert again.updated == 0
        assert again.removed == 0
    finally:
        _stop_server(stop_event, executor, future)


def test_workspace_scan_rejects_relative_wire_path_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        response = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "bad-scan",
                "method": "workspace_scan",
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


def test_harness_scan_dispatches_absolute_location_and_prints_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repo"
    nested = repository / "src"
    nested.mkdir(parents=True)
    socket_path = tmp_path / "harness.sock"
    seen: list[tuple[Path, Path]] = []

    def request_scan(ipc_socket: Path, path: Path) -> WorkspaceScanResult:
        seen.append((ipc_socket, path))
        return WorkspaceScanResult(
            schema_version=3,
            project_id="project-1",
            workspace_id="workspace-1",
            visibility_mode="normal",
            workspace_root=repository.resolve(),
            project_created=True,
            workspace_created=True,
            file_count=4,
            added=4,
            updated=0,
            removed=0,
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "scan", str(nested), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)

    assert entrypoints.harness_main() == 0
    assert seen == [(socket_path, nested.resolve())]
    assert capsys.readouterr().out.splitlines() == [
        "Project: project-1",
        "Workspace: workspace-1",
        f"Workspace root: {repository.resolve()}",
        "Visibility: normal",
        "Project created: yes",
        "Workspace created: yes",
        "Indexed files: 4",
        "Added: 4",
        "Updated: 0",
        "Removed: 0",
        "Schema: 3",
    ]
