from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.daemon import serve_daemon
from harness.ipc import request_workspace_status
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX IPC slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


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


def _stop_server(
    stop_event: Event,
    executor: ThreadPoolExecutor,
    future: Future[None],
) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def test_workspace_status_round_trip_accepts_long_git_branch(tmp_path: Path) -> None:
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

    branch = "/".join(["a" * 100] * 15)
    assert len(branch) > 1024
    _git(root, "switch", "-c", branch)

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
    finally:
        connection.close()

    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        status = request_workspace_status(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
        )
        assert status.workspace_id == workspace.workspace_id
        assert status.branch == branch
    finally:
        _stop_server(stop_event, executor, future)
