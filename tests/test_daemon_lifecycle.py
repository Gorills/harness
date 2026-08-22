from __future__ import annotations

import os
import socket
import stat
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.daemon import (
    DaemonAlreadyRunningError,
    InsecureDaemonLockError,
    serve_daemon,
)
from harness.ipc import IpcError, StatusResult, request_status
from harness.storage import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon lifecycle slice")


def _start_server(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop_event)
    deadline = time.monotonic() + 3
    while True:
        if future.done():
            future.result()
        try:
            if request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0):
                break
        except IpcError:
            pass
        if time.monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon did not become ready")
        time.sleep(0.01)
    return stop_event, executor, future


def _stop_server(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def test_daemon_recovers_stale_owned_socket_after_lock_acquisition(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    socket_path.parent.mkdir(mode=0o700)
    socket_path.parent.chmod(0o700)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale_socket:
        stale_socket.bind(str(socket_path))
    assert stat.S_ISSOCK(socket_path.lstat().st_mode)

    stop_event, executor, future = _start_server(database, socket_path)
    try:
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
        lock_path = socket_path.with_name(f"{socket_path.name}.lock")
        lock_stat = lock_path.lstat()
        assert stat.S_ISREG(lock_stat.st_mode)
        assert stat.S_IMODE(lock_stat.st_mode) == 0o600
        assert lock_stat.st_uid == os.geteuid()
    finally:
        _stop_server(stop_event, executor, future)

    assert not socket_path.exists()
    assert socket_path.with_name(f"{socket_path.name}.lock").is_file()


def test_second_daemon_fails_before_database_mutation_and_first_keeps_serving(
    tmp_path: Path,
) -> None:
    first_database = tmp_path / "first.db"
    second_database = tmp_path / "second.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(first_database, socket_path)
    try:
        with pytest.raises(DaemonAlreadyRunningError, match="already owns the IPC endpoint"):
            serve_daemon(second_database, socket_path, stop_event=Event())

        assert not second_database.exists()
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 0, 0)
    finally:
        _stop_server(stop_event, executor, future)


def test_daemon_refuses_symlinked_singleton_lock_without_touching_target(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    socket_path.parent.mkdir(mode=0o700)
    socket_path.parent.chmod(0o700)
    target = tmp_path / "user-file"
    target.write_text("unchanged", encoding="utf-8")
    lock_path = socket_path.with_name(f"{socket_path.name}.lock")
    lock_path.symlink_to(target)

    with pytest.raises(InsecureDaemonLockError, match="must be a regular file"):
        serve_daemon(database, socket_path, stop_event=Event())

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert not database.exists()
