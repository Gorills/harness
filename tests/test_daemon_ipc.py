from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest

import harness.daemon as daemon_module
from harness.daemon import (
    DaemonError,
    InsecureSocketDirectoryError,
    SocketPathInUseError,
    serve_daemon,
)
from harness.index import scan_workspace
from harness.ipc import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    IpcProtocolError,
    IpcRemoteError,
    RuntimeDiagnosticsResult,
    StatusResult,
    WorkspaceScanResult,
    WorkspaceStatusResult,
    _receive_frame,
    _status_from_response,
    request_runtime_diagnostics,
    request_status,
    request_workspace_scan,
    request_workspace_status,
)
from harness.registry import create_project, register_workspace
from harness.runtime_identity import current_runtime_identity
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX IPC slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _git_output(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return os.fsdecode(result.stdout).strip()


def _registered_workspace_database(
    tmp_path: Path,
) -> tuple[Path, Path, str, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
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
        "init",
    )
    head = _git_output(root, "rev-parse", "HEAD")
    branch = _git_output(root, "branch", "--show-current")

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        return root, database, project.project_id, workspace.workspace_id, f"{head}:{branch}"
    finally:
        connection.close()


def _start_server(
    database: Path, socket_path: Path
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


def _raw_request(socket_path: Path, payload: bytes) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        client.sendall(payload)
        response = bytearray()
        while not response.endswith(b"\n"):
            response.extend(client.recv(4096))
    value: object = json.loads(response.decode("utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_status_round_trip_returns_only_bounded_registry_counts(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project-1'), ('project-2')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace-1', 'project-1', '/repo', '/repo/.git')
            """
        )
    finally:
        connection.close()

    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        assert request_status(socket_path) == StatusResult(
            schema_version=SCHEMA_VERSION,
            project_count=2,
            workspace_count=1,
        )
        response = _raw_request(
            socket_path,
            b'{"version":1,"request_id":"exact","method":"status"}\n',
        )
        assert response == {
            "version": PROTOCOL_VERSION,
            "request_id": "exact",
            "ok": True,
            "result": {
                "schema_version": SCHEMA_VERSION,
                "project_count": 2,
                "workspace_count": 1,
            },
        }
    finally:
        _stop_server(stop_event, executor, future)

    assert not socket_path.exists()


def test_runtime_diagnostics_report_exact_daemon_identity_with_dashboard_running(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        diagnostics = request_runtime_diagnostics(socket_path)
        runtime_identity = current_runtime_identity()
        assert diagnostics == RuntimeDiagnosticsResult(
            schema_version=SCHEMA_VERSION,
            package_version=runtime_identity.package_version,
            python_executable=runtime_identity.python_executable,
            code_sha256=runtime_identity.code_sha256,
            project_count=0,
            workspace_count=0,
            dashboard_running=True,
        )
    finally:
        _stop_server(stop_event, executor, future)


def test_workspace_status_round_trip_resolves_registered_root_and_live_git_state(
    tmp_path: Path,
) -> None:
    root, database, project_id, workspace_id, git_identity = _registered_workspace_database(
        tmp_path
    )
    head, branch = git_identity.split(":", maxsplit=1)
    (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        status = request_workspace_status(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
        )
        assert status == WorkspaceStatusResult(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace_id,
            project_id=project_id,
            visibility_mode="normal",
            workspace_root=root.resolve(),
            head=head,
            branch=branch,
            dirty_path_count=1,
            indexed_file_count=1,
        )

        raw_payload = {
            "version": PROTOCOL_VERSION,
            "request_id": "workspace-exact",
            "method": "workspace_status",
            "params": {
                "hints": [
                    {
                        "path": str(root.resolve()),
                        "source": "explicit-root",
                        "match_mode": "root",
                    }
                ]
            },
        }
        response = _raw_request(
            socket_path,
            (json.dumps(raw_payload, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        assert response == {
            "version": PROTOCOL_VERSION,
            "request_id": "workspace-exact",
            "ok": True,
            "result": {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "visibility_mode": "normal",
                "workspace_root": str(root.resolve()),
                "head": head,
                "branch": branch,
                "dirty_path_count": 1,
                "indexed_file_count": 1,
            },
        }
    finally:
        _stop_server(stop_event, executor, future)


def test_workspace_status_location_hint_resolves_most_specific_registered_workspace(
    tmp_path: Path,
) -> None:
    root, database, _project_id, workspace_id, _git_identity = _registered_workspace_database(
        tmp_path
    )
    nested = root / "src" / "package"
    nested.mkdir(parents=True)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        status = request_workspace_status(
            socket_path,
            [WorkspaceHint(nested, "cwd", WorkspaceHintMatchMode.LOCATION)],
        )
        assert status.workspace_id == workspace_id
        assert status.workspace_root == root.resolve()
    finally:
        _stop_server(stop_event, executor, future)


def test_workspace_status_stronger_unmatched_hint_fails_without_fallback(tmp_path: Path) -> None:
    root, database, _project_id, _workspace_id, _git_identity = _registered_workspace_database(
        tmp_path
    )
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with pytest.raises(IpcRemoteError) as exc_info:
            request_workspace_status(
                socket_path,
                [
                    WorkspaceHint(tmp_path / "unknown", "explicit-root"),
                    WorkspaceHint(root, "cwd", WorkspaceHintMatchMode.LOCATION),
                ],
            )
        assert exc_info.value.code == "workspace_resolution_error"
        assert "explicit-root" in exc_info.value.message
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 1, 1)
    finally:
        _stop_server(stop_event, executor, future)


@pytest.mark.parametrize(
    "params",
    [
        {"hints": []},
        {"hints": [{"path": "relative", "source": "cwd", "match_mode": "location"}]},
        {
            "hints": [
                {
                    "path": "/repo",
                    "source": "cwd",
                    "match_mode": "location",
                    "extra": True,
                }
            ]
        },
    ],
)
def test_workspace_status_rejects_malformed_params_and_daemon_recovers(
    tmp_path: Path,
    params: dict[str, object],
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        payload = {
            "version": PROTOCOL_VERSION,
            "request_id": "bad-workspace",
            "method": "workspace_status",
            "params": params,
        }
        response = _raw_request(
            socket_path,
            (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
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


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":2,"request_id":"bad","method":"status"}\n',
        b'{"version":1,"request_id":"bad","method":"project_status"}\n',
        b"not-json\n",
    ],
)
def test_invalid_protocol_requests_fail_closed_and_daemon_recovers(
    tmp_path: Path, payload: bytes
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        response = _raw_request(socket_path, payload)
        assert response == {
            "version": PROTOCOL_VERSION,
            "request_id": None,
            "ok": False,
            "error": {"code": "invalid_request", "message": "IPC request is invalid"},
        }
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_status_request_rejects_extra_fields_without_mutating_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        response = _raw_request(
            socket_path,
            b'{"version":1,"request_id":"bad","method":"status","params":{}}\n',
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


def test_oversized_request_is_bounded_and_next_client_still_succeeds(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        response = _raw_request(socket_path, b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n")
        assert response["ok"] is False
        assert response["error"] == {
            "code": "message_too_large",
            "message": "IPC request exceeds byte limit",
        }
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_socket_is_created_inside_private_directory_with_private_mode(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    socket_path = tmp_path / "private" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) & 0o077 == 0
        assert stat.S_IMODE(socket_path.lstat().st_mode) == 0o600
    finally:
        _stop_server(stop_event, executor, future)


def test_daemon_refuses_insecure_existing_socket_directory(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir(mode=0o755)
    ipc_dir.chmod(0o755)

    with pytest.raises(InsecureSocketDirectoryError, match="must be owned by the current user"):
        serve_daemon(database, ipc_dir / "harness.sock", stop_event=Event())


def test_daemon_refuses_existing_socket_path_without_deleting_it(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir(mode=0o700)
    socket_path = ipc_dir / "harness.sock"
    socket_path.write_text("user-owned", encoding="utf-8")

    with pytest.raises(SocketPathInUseError, match="refusing to replace"):
        serve_daemon(database, socket_path, stop_event=Event())

    assert socket_path.read_text(encoding="utf-8") == "user-owned"


def test_daemon_initializes_database_before_serving_status(tmp_path: Path) -> None:
    database = tmp_path / "state" / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        assert database.is_file()
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_disconnected_client_does_not_stop_daemon(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_slow_client_does_not_block_independent_status_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    first_status_started = Event()
    release_first_status = Event()
    original_serve_global_status = daemon_module._serve_global_status

    def blocking_first_status(
        client: socket.socket, database_connection: sqlite3.Connection, request_id: str
    ) -> None:
        if not first_status_started.is_set():
            first_status_started.set()
            if not release_first_status.wait(timeout=2):
                raise AssertionError("first IPC status handler was not released")
        original_serve_global_status(client, database_connection, request_id)

    monkeypatch.setattr(daemon_module, "_serve_global_status", blocking_first_status)
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow_client:
            slow_client.settimeout(2)
            slow_client.connect(str(socket_path))
            slow_client.sendall(b'{"version":1,"request_id":"slow","method":"status"}\n')
            assert first_status_started.wait(timeout=1)

            with ThreadPoolExecutor(max_workers=1) as client_executor:
                status_future = client_executor.submit(request_status, socket_path)
                assert status_future.result(timeout=1) == StatusResult(SCHEMA_VERSION, 0, 0)

            release_first_status.set()
            response = bytearray()
            while not response.endswith(b"\n"):
                response.extend(slow_client.recv(4096))
            assert json.loads(response.decode("utf-8"))["ok"] is True
    finally:
        release_first_status.set()
        _stop_server(stop_event, executor, future)


def test_slow_scan_does_not_block_status_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, _project_id, workspace_id, _git_identity = _registered_workspace_database(
        tmp_path
    )
    socket_path = tmp_path / "ipc" / "harness.sock"
    scan_started = Event()
    release_scan = Event()
    original_scan_workspace_path = daemon_module.scan_workspace_path

    def blocking_scan_workspace_path(
        connection: sqlite3.Connection, path: Path, *, deadline: float | None = None
    ) -> WorkspaceScanResult:
        scan_started.set()
        if not release_scan.wait(timeout=2):
            raise AssertionError("slow Workspace scan was not released")
        return original_scan_workspace_path(connection, path, deadline=deadline)

    monkeypatch.setattr(daemon_module, "scan_workspace_path", blocking_scan_workspace_path)
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as client_executor:
            scan_future = client_executor.submit(request_workspace_scan, socket_path, root)
            assert scan_started.wait(timeout=1)

            status_future = client_executor.submit(request_status, socket_path)
            assert status_future.result(timeout=1) == StatusResult(SCHEMA_VERSION, 1, 1)

            release_scan.set()
            assert scan_future.result(timeout=3).workspace_id == workspace_id
    finally:
        release_scan.set()
        _stop_server(stop_event, executor, future)


def test_unexpected_client_worker_failure_stops_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    worker_started = Event()

    def fail_client(*_args: object, **_kwargs: object) -> None:
        worker_started.set()
        raise RuntimeError("synthetic client worker failure")

    monkeypatch.setattr(daemon_module, "_serve_client", fail_client)
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            client.sendall(b'{"version":1,"request_id":"boom","method":"status"}\n')
        assert worker_started.wait(timeout=1)
        with pytest.raises(DaemonError, match="IPC client worker stopped unexpectedly"):
            future.result(timeout=2)
    finally:
        stop_event.set()
        executor.shutdown(wait=True)


def test_client_rejects_boolean_protocol_version() -> None:
    with pytest.raises(IpcProtocolError, match="unsupported IPC protocol version"):
        _status_from_response(
            {
                "version": True,
                "request_id": "request",
                "ok": True,
                "result": {
                    "schema_version": SCHEMA_VERSION,
                    "project_count": 0,
                    "workspace_count": 0,
                },
            },
            expected_request_id="request",
        )


def test_receive_frame_timeout_is_total_not_per_chunk() -> None:
    reader, writer = socket.socketpair()
    reader.settimeout(0.1)

    def trickle_request() -> None:
        try:
            for chunk in (b"{", b'"', b"x", b"\n"):
                time.sleep(0.06)
                try:
                    writer.sendall(chunk)
                except OSError:
                    return
        finally:
            writer.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(trickle_request)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match=r"timed out|deadline"):
                _receive_frame(reader)
            elapsed = time.monotonic() - started
            assert elapsed < 0.2
        finally:
            reader.close()
            future.result()
