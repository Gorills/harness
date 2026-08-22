from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from harness.daemon import serve_daemon
from harness.index import IndexedFileKind, scan_workspace
from harness.ipc import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    IpcRemoteError,
    StatusResult,
    WorkspaceSearchHit,
    WorkspaceSearchResult,
    request_status,
    request_workspace_search,
)
from harness.registry import create_project, register_workspace
from harness.search import SearchMatchKind
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


def _raw_request(socket_path: Path, payload: dict[str, object]) -> tuple[bytes, dict[str, object]]:
    wire = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        client.sendall(wire)
        response = bytearray()
        while not response.endswith(b"\n"):
            response.extend(client.recv(4096))
    value: object = json.loads(response.decode("utf-8"))
    assert isinstance(value, dict)
    return bytes(response), cast(dict[str, object], value)


def test_workspace_search_round_trip_is_scoped_bounded_and_mechanical(tmp_path: Path) -> None:
    root, database, project_id, workspace_id = _registered_workspace_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        result = request_workspace_search(
            socket_path,
            [WorkspaceHint(root.resolve(), "explicit-root")],
            "rotate refresh token",
            limit=2,
        )
        assert result == WorkspaceSearchResult(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace_id,
            project_id=project_id,
            workspace_root=root.resolve(),
            results=(
                WorkspaceSearchHit(
                    relative_path="src/rotateRefreshToken.py",
                    kind=IndexedFileKind.FILE,
                    size_bytes=(root / "src" / "rotateRefreshToken.py").stat().st_size,
                    match_kind=SearchMatchKind.IDENTIFIER_TOKENS,
                ),
                WorkspaceSearchHit(
                    relative_path="tests/rotate_refresh_token_test.py",
                    kind=IndexedFileKind.FILE,
                    size_bytes=(root / "tests" / "rotate_refresh_token_test.py").stat().st_size,
                    match_kind=SearchMatchKind.IDENTIFIER_TOKENS,
                ),
            ),
        )

        raw_bytes, raw = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "search-exact",
                "method": "workspace_search",
                "params": {
                    "hints": [
                        {
                            "path": str(root.resolve()),
                            "source": "cwd",
                            "match_mode": WorkspaceHintMatchMode.LOCATION.value,
                        }
                    ],
                    "query": "src/rotateRefreshToken.py",
                    "limit": 1,
                },
            },
        )
        assert len(raw_bytes) <= MAX_MESSAGE_BYTES + 1
        assert raw == {
            "version": PROTOCOL_VERSION,
            "request_id": "search-exact",
            "ok": True,
            "result": {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "project_id": project_id,
                "workspace_root": str(root.resolve()),
                "results": [
                    {
                        "relative_path": "src/rotateRefreshToken.py",
                        "kind": "file",
                        "size_bytes": (root / "src" / "rotateRefreshToken.py").stat().st_size,
                        "match_kind": "exact_path",
                    }
                ],
            },
        }
        serialized = json.dumps(raw, sort_keys=True)
        assert "content_sha256" not in serialized
        assert "TOKEN = 1" not in serialized
    finally:
        _stop_server(stop_event, executor, future)


@pytest.mark.parametrize(
    "params",
    [
        {"hints": [], "query": "token", "limit": 10},
        {
            "hints": [{"path": "/repo", "source": "cwd", "match_mode": "location"}],
            "query": "   ",
            "limit": 10,
        },
        {
            "hints": [{"path": "/repo", "source": "cwd", "match_mode": "location"}],
            "query": "token",
            "limit": 0,
        },
        {
            "hints": [{"path": "/repo", "source": "cwd", "match_mode": "location"}],
            "query": "token",
            "limit": 10,
            "extra": True,
        },
    ],
)
def test_workspace_search_rejects_malformed_params_and_daemon_recovers(
    tmp_path: Path,
    params: dict[str, object],
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        _raw_bytes, response = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "bad-search",
                "method": "workspace_search",
                "params": params,
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


def test_workspace_search_response_overflow_returns_bounded_error(tmp_path: Path) -> None:
    directory = "d" * 120
    files = {
        f"{directory}/file-{index:02d}-{'x' * 150}.txt": "x\n" for index in range(50)
    }
    root, database, _project_id, _workspace_id = _registered_workspace_database(
        tmp_path,
        files=files,
    )
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        with pytest.raises(IpcRemoteError) as exc_info:
            request_workspace_search(
                socket_path,
                [WorkspaceHint(root.resolve(), "cwd", WorkspaceHintMatchMode.LOCATION)],
                "x",
                limit=50,
            )
        assert exc_info.value.code == "response_too_large"
        assert exc_info.value.message == "Workspace search result exceeds IPC byte limit"
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 1, 1)
    finally:
        _stop_server(stop_event, executor, future)
