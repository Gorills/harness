from __future__ import annotations

import json
import os
import socket
import stat
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from harness.daemon import (
    InsecureSocketDirectoryError,
    SocketPathInUseError,
    serve_daemon,
)
from harness.ipc import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    IpcProtocolError,
    StatusResult,
    _receive_frame,
    _status_from_response,
    request_status,
)
from harness.storage import connect_database, initialize_database

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX IPC slice")


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
            schema_version=2,
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
            "result": {"schema_version": 2, "project_count": 2, "workspace_count": 1},
        }
    finally:
        _stop_server(stop_event, executor, future)

    assert not socket_path.exists()


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
        assert request_status(socket_path) == StatusResult(2, 0, 0)
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
        assert request_status(socket_path) == StatusResult(2, 0, 0)
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
        assert request_status(socket_path) == StatusResult(2, 0, 0)
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
        assert request_status(socket_path) == StatusResult(2, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_disconnected_client_does_not_stop_daemon(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
        assert request_status(socket_path) == StatusResult(2, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_client_rejects_boolean_protocol_version() -> None:
    with pytest.raises(IpcProtocolError, match="unsupported IPC protocol version"):
        _status_from_response(
            {
                "version": True,
                "request_id": "request",
                "ok": True,
                "result": {"schema_version": 2, "project_count": 0, "workspace_count": 0},
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
