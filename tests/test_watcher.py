from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

import harness.watcher as watcher_module
from harness.daemon import serve_daemon
from harness.index import (
    MAX_INCREMENTAL_SCAN_PATHS,
    IndexedFileRecord,
    IndexingError,
    ScanResult,
    list_indexed_files,
    scan_workspace,
    scan_workspace_paths,
)
from harness.ipc import IpcError, StatusResult, request_status, request_workspace_scan
from harness.registry import (
    WorkspaceRecord,
    create_project,
    get_workspace,
    register_workspace,
    relocate_workspace,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.watcher import (
    WATCH_IDLE_WORKSPACE_SAMPLE_LIMIT,
    WATCH_METADATA_FILE_SAMPLE_LIMIT,
    WorkspaceGitSnapshot,
    WorkspaceMetadataSnapshot,
    WorkspaceWatcher,
    read_workspace_change_token,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon watcher slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
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


def _registered(tmp_path: Path) -> tuple[Path, Path, str]:
    root = _repository(tmp_path / "repo")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        return root, database, workspace.workspace_id
    finally:
        connection.close()


def _start_server(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        serve_daemon,
        database,
        socket_path,
        stop_event=stop_event,
        watcher_poll_seconds=0.02,
        watcher_debounce_seconds=0.03,
        watcher_full_reconcile_seconds=5.0,
        watcher_retry_seconds=0.03,
        watcher_token_deadline_seconds=1.0,
        watcher_scan_deadline_seconds=2.0,
    )
    deadline = time.monotonic() + 3.0
    while True:
        if future.done():
            future.result()
        try:
            request_status(socket_path)
            return stop_event, executor, future
        except IpcError:
            pass
        if time.monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon did not become ready")
        time.sleep(0.01)


def _stop_server(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def _wait_for_index(
    database: Path,
    workspace_id: str,
    expected: dict[str, str],
    *,
    timeout: float = 3.0,
) -> None:
    connection = connect_database(database)
    try:
        deadline = time.monotonic() + timeout
        while True:
            actual = {
                record.relative_path: record.content_sha256
                for record in list_indexed_files(connection, workspace_id)
            }
            if actual == expected:
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f"watcher index did not converge: {actual!r}")
            time.sleep(0.02)
    finally:
        connection.close()


def test_change_token_detects_rewrites_while_git_status_shape_is_unchanged(tmp_path: Path) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        workspace = get_workspace(connection, workspace_id)
        tracked = root / "tracked.txt"
        tracked.write_text("first!\n", encoding="utf-8")
        first = read_workspace_change_token(workspace, deadline=time.monotonic() + 2.0)

        tracked.write_text("other!\n", encoding="utf-8")
        second = read_workspace_change_token(workspace, deadline=time.monotonic() + 2.0)

        assert first != second
    finally:
        connection.close()


def test_change_token_refuses_dirty_path_through_symlinked_parent(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    nested = root / "nested"
    nested.mkdir()
    tracked = nested / "tracked.txt"
    tracked.write_text("inside\n", encoding="utf-8")
    _git(root, "add", "nested/tracked.txt")
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
        "nested",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)

        tracked.unlink()
        nested.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "tracked.txt").write_text("outside\n", encoding="utf-8")
        nested.symlink_to(outside, target_is_directory=True)

        with pytest.raises(
            watcher_module.WorkspaceWatchError, match="escapes through a symlinked parent"
        ):
            read_workspace_change_token(workspace, deadline=time.monotonic() + 2.0)
    finally:
        connection.close()


def test_watcher_debounces_rapid_changes_and_scans_final_filesystem_state(tmp_path: Path) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.3,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0

        added = root / "added.txt"
        added.write_text("first\n", encoding="utf-8")
        assert watcher.poll(now=1.0) == 0
        added.write_text("final\n", encoding="utf-8")
        assert watcher.poll(now=1.1) == 0
        assert watcher.poll(now=1.35) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert records["added.txt"].content_sha256 == hashlib.sha256(b"final\n").hexdigest()
    finally:
        connection.close()


def test_idle_watcher_poll_uses_no_git_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, database, _workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        def unexpected_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise AssertionError("idle watcher poll must not invoke Git")

        monkeypatch.setattr(subprocess, "run", unexpected_run)
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=2.0) == 0
    finally:
        connection.close()


def test_idle_watcher_poll_does_not_reload_full_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, database, _workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        def unexpected_list(*_args: object, **_kwargs: object) -> tuple[object, ...]:
            raise AssertionError("idle watcher poll must not reload the full indexed inventory")

        monkeypatch.setattr("harness.watcher.list_indexed_files", unexpected_list)
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=2.0) == 0
    finally:
        connection.close()


def test_idle_watcher_poll_digests_one_directory_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database, _workspace_id = _registered(tmp_path)
    for index in range(WATCH_METADATA_FILE_SAMPLE_LIMIT + 40):
        directory = root / f"pkg{index:03d}"
        directory.mkdir()
        (directory / "mod.py").write_text("x = 1\n", encoding="utf-8")
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=2.0,
            scan_deadline_seconds=5.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        sampled_directory_counts: list[int] = []
        real_digest = watcher_module._digest_workspace_directories

        def counted_digest(
            digest: object,
            workspace_root: Path,
            directory_paths: Sequence[str],
            *,
            deadline: float,
        ) -> None:
            sampled_directory_counts.append(len(directory_paths))
            real_digest(digest, workspace_root, directory_paths, deadline=deadline)

        monkeypatch.setattr(watcher_module, "_digest_workspace_directories", counted_digest)
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=2.0) == 0
        assert sampled_directory_counts
        assert max(sampled_directory_counts) <= WATCH_METADATA_FILE_SAMPLE_LIMIT
    finally:
        connection.close()


def test_idle_poll_samples_a_bounded_workspace_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        for name in ("one", "two", "three"):
            root = _repository(tmp_path / name)
            workspace = register_workspace(connection, project_id=project.project_id, path=root)
            scan_workspace(connection, workspace.workspace_id)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1
        assert watcher.poll(now=0.22) == 1
        assert watcher.poll(now=0.33) == 1

        sampled_roots: list[Path] = []
        real_idle = watcher_module.read_idle_workspace_metadata

        def counted_idle(
            workspace: WorkspaceRecord,
            indexed_files: object,
            directory_paths: object,
            *,
            deadline: float,
        ) -> object:
            sampled_roots.append(workspace.workspace_root)
            return real_idle(workspace, indexed_files, directory_paths, deadline=deadline)

        monkeypatch.setattr(watcher_module, "read_idle_workspace_metadata", counted_idle)
        assert watcher.poll(now=1.0) == 0
        assert len(sampled_roots) <= WATCH_IDLE_WORKSPACE_SAMPLE_LIMIT
        assert len(sampled_roots) == WATCH_IDLE_WORKSPACE_SAMPLE_LIMIT
    finally:
        connection.close()


def test_directory_listing_skips_unreadable_subdirectory(tmp_path: Path) -> None:
    root, _database, _workspace_id = _registered(tmp_path)
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "inside.txt").write_text("secret\n", encoding="utf-8")
    os.chmod(blocked, 0)
    try:
        directories = watcher_module.list_workspace_metadata_directories(
            root, deadline=time.monotonic() + 2.0
        )
    finally:
        os.chmod(blocked, 0o700)
    assert "" in directories
    assert "blocked" in directories


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
def test_unreadable_subdirectory_does_not_hot_loop_full_scan(tmp_path: Path) -> None:
    root, database, _workspace_id = _registered(tmp_path)
    blocked = root / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0)
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=2.0,
            scan_deadline_seconds=5.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=2.0) == 0
    finally:
        os.chmod(blocked, 0o700)
        connection.close()


def test_watcher_uses_incremental_scan_for_bounded_dirty_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        full_calls = 0
        incremental_calls: list[tuple[str, ...]] = []
        real_full_scan = scan_workspace
        real_incremental_scan = scan_workspace_paths

        def counted_full_scan(
            scan_connection: sqlite3.Connection,
            selected_workspace_id: str,
            *,
            deadline: float | None = None,
        ) -> ScanResult:
            nonlocal full_calls
            full_calls += 1
            return real_full_scan(scan_connection, selected_workspace_id, deadline=deadline)

        def counted_incremental_scan(
            scan_connection: sqlite3.Connection,
            selected_workspace_id: str,
            relative_paths: Sequence[str],
            *,
            deadline: float | None = None,
        ) -> ScanResult:
            incremental_calls.append(tuple(relative_paths))
            return real_incremental_scan(
                scan_connection,
                selected_workspace_id,
                relative_paths,
                deadline=deadline,
            )

        monkeypatch.setattr(watcher_module, "scan_workspace", counted_full_scan)
        monkeypatch.setattr(watcher_module, "scan_workspace_paths", counted_incremental_scan)
        (root / "tracked.txt").write_text("incremental\n", encoding="utf-8")
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=1.11) == 1

        assert full_calls == 0
        assert incremental_calls == [("tracked.txt",)]
        record = {
            item.relative_path: item for item in list_indexed_files(connection, workspace_id)
        }["tracked.txt"]
        assert record.content_sha256 == hashlib.sha256(b"incremental\n").hexdigest()
    finally:
        connection.close()


def test_oversized_dirty_workspace_does_not_hot_loop_full_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, _workspace_id = _registered(tmp_path)
    noise = root / "noise"
    noise.mkdir()
    for index in range(MAX_INCREMENTAL_SCAN_PATHS + 20):
        (noise / f"f{index:03d}.txt").write_text("n\n", encoding="utf-8")
    connection = connect_database(database)
    try:
        full_calls = 0
        real_full_scan = scan_workspace

        def counted_full_scan(
            scan_connection: sqlite3.Connection,
            selected_workspace_id: str,
            *,
            deadline: float | None = None,
        ) -> ScanResult:
            nonlocal full_calls
            full_calls += 1
            return real_full_scan(scan_connection, selected_workspace_id, deadline=deadline)

        monkeypatch.setattr(watcher_module, "scan_workspace", counted_full_scan)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=2.0,
            scan_deadline_seconds=5.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1
        assert full_calls == 1

        (noise / "f000.txt").write_text("changed\n", encoding="utf-8")
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=1.11) == 0
        assert watcher.poll(now=2.0) == 0
        assert full_calls == 1
        assert watcher.poll(now=100.12) == 1
        assert full_calls == 2
    finally:
        connection.close()


def test_rotating_metadata_shards_detect_rewrite_beyond_first_sample(tmp_path: Path) -> None:
    root, database, workspace_id = _registered(tmp_path)
    bulk = root / "bulk"
    bulk.mkdir()
    for index in range(300):
        (bulk / f"file_{index:03d}.txt").write_text(f"{index}\n", encoding="utf-8")
    _git(root, "add", "bulk")
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
        "bulk",
    )
    connection = connect_database(database)
    try:
        scan_workspace(connection, workspace_id)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        target = bulk / "file_280.txt"
        target.write_text("rotated\n", encoding="utf-8")
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=1.1) == 0
        assert watcher.poll(now=1.21) == 1

        record = {
            item.relative_path: item for item in list_indexed_files(connection, workspace_id)
        }["bulk/file_280.txt"]
        assert record.content_sha256 == hashlib.sha256(b"rotated\n").hexdigest()
    finally:
        connection.close()


def test_watcher_retries_a_transient_scan_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        original_scan = scan_workspace
        attempts = 0

        def flaky_scan(
            scan_connection: sqlite3.Connection,
            scan_workspace_id: str,
            *,
            deadline: float | None = None,
        ) -> ScanResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise IndexingError("transient watcher scan failure")
            return original_scan(scan_connection, scan_workspace_id, deadline=deadline)

        monkeypatch.setattr(watcher_module, "scan_workspace", flaky_scan)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=1.11) == 0
        assert attempts == 1
        assert watcher.poll(now=1.2) == 0
        assert attempts == 1
        assert watcher.poll(now=1.32) == 1
        assert attempts == 2

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert records["tracked.txt"].content_sha256 == hashlib.sha256(b"changed\n").hexdigest()
    finally:
        connection.close()


def test_watcher_preserves_pending_change_while_manual_scan_lock_is_held(tmp_path: Path) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    scan_lock = Lock()
    try:
        watcher = WorkspaceWatcher(
            connection,
            scan_lock,
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        (root / "tracked.txt").write_text("serialized\n", encoding="utf-8")
        assert watcher.poll(now=1.0) == 0
        scan_lock.acquire()
        try:
            assert watcher.poll(now=1.11) == 0
        finally:
            scan_lock.release()
        assert watcher.poll(now=1.12) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert records["tracked.txt"].content_sha256 == hashlib.sha256(b"serialized\n").hexdigest()
    finally:
        connection.close()


def test_watcher_detects_clean_tracked_tree_change_from_head_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        tracked = root / "tracked.txt"
        tracked.write_text("second\n", encoding="utf-8")
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
            "second",
        )
        scan_workspace(connection, workspace_id)

        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        full_calls = 0
        incremental_calls = 0
        real_full_scan = scan_workspace
        real_incremental_scan = scan_workspace_paths

        def counted_full_scan(
            scan_connection: sqlite3.Connection,
            selected_workspace_id: str,
            *,
            deadline: float | None = None,
        ) -> ScanResult:
            nonlocal full_calls
            full_calls += 1
            return real_full_scan(scan_connection, selected_workspace_id, deadline=deadline)

        def counted_incremental_scan(
            scan_connection: sqlite3.Connection,
            selected_workspace_id: str,
            relative_paths: Sequence[str],
            *,
            deadline: float | None = None,
        ) -> ScanResult:
            nonlocal incremental_calls
            incremental_calls += 1
            return real_incremental_scan(
                scan_connection,
                selected_workspace_id,
                relative_paths,
                deadline=deadline,
            )

        monkeypatch.setattr(watcher_module, "scan_workspace", counted_full_scan)
        monkeypatch.setattr(watcher_module, "scan_workspace_paths", counted_incremental_scan)

        _git(root, "reset", "--hard", "HEAD^")
        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=1.11) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert records["tracked.txt"].content_sha256 == hashlib.sha256(b"tracked\n").hexdigest()
        assert full_calls == 1
        assert incremental_calls == 0
    finally:
        connection.close()


def test_watcher_reconciles_existing_workspace_after_restart(tmp_path: Path) -> None:
    root, database, workspace_id = _registered(tmp_path)
    (root / "tracked.txt").write_text("offline change\n", encoding="utf-8")

    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert (
            records["tracked.txt"].content_sha256 == hashlib.sha256(b"offline change\n").hexdigest()
        )
    finally:
        connection.close()


def test_watcher_reconciles_relocated_workspace_even_when_change_token_is_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        monkeypatch.setattr(
            watcher_module,
            "read_workspace_metadata_snapshot",
            lambda *_args, **_kwargs: WorkspaceMetadataSnapshot("stable", "stable"),
        )
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        moved = tmp_path / "moved-repo"
        root.rename(moved)
        relocate_workspace(connection, workspace_id, new_path=moved)
        assert list_indexed_files(connection, workspace_id) == ()

        assert watcher.poll(now=1.0) == 0
        assert watcher.poll(now=1.11) == 1
        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert records["tracked.txt"].content_sha256 == hashlib.sha256(b"tracked\n").hexdigest()
    finally:
        connection.close()


def test_watcher_reconciles_project_skills_after_authoritative_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    reconciled: list[tuple[str, tuple[str, ...]]] = []
    try:

        def reconcile(
            _connection: sqlite3.Connection,
            selected_workspace_id: str,
            profiles: tuple[str, ...],
        ) -> None:
            reconciled.append((selected_workspace_id, profiles))

        monkeypatch.setattr(watcher_module, "reconcile_workspace_skills", reconcile)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
            skill_profiles_provider=lambda: ("codex", "cursor"),
        )

        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1
        assert reconciled == [(workspace_id, ("codex", "cursor"))]
    finally:
        connection.close()


def test_watcher_rescans_after_sampling_failure_invalidates_prior_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        calls = 0

        real_idle = watcher_module.read_idle_workspace_metadata

        def flaky_token(
            workspace: WorkspaceRecord,
            indexed_files: Sequence[IndexedFileRecord],
            directory_paths: Sequence[str],
            *,
            deadline: float,
        ) -> watcher_module.IdleWorkspaceMetadata:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise watcher_module.WorkspaceWatchError("transient token failure")
            return real_idle(
                workspace,
                indexed_files,
                directory_paths,
                deadline=deadline,
            )

        monkeypatch.setattr(watcher_module, "read_idle_workspace_metadata", flaky_token)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        (root / "tracked.txt").write_text("sampling failed\n", encoding="utf-8")
        assert watcher.poll(now=0.11) == 0
        assert watcher.poll(now=0.22) == 1
        assert watcher.poll(now=0.22) == 0
        assert watcher.poll(now=0.32) == 0
        assert watcher.poll(now=0.43) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert (
            records["tracked.txt"].content_sha256
            == hashlib.sha256(b"sampling failed\n").hexdigest()
        )
    finally:
        connection.close()


def test_watcher_rescans_after_success_with_unknown_pre_scan_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        calls = 0

        real_snapshot = watcher_module.read_workspace_change_snapshot

        def flaky_snapshot(
            workspace: WorkspaceRecord,
            *,
            deadline: float,
        ) -> WorkspaceGitSnapshot:
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise watcher_module.WorkspaceWatchError("transient token failure")
            return real_snapshot(workspace, deadline=deadline)

        monkeypatch.setattr(watcher_module, "read_workspace_change_snapshot", flaky_snapshot)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        (root / "tracked.txt").write_text("during unknown token\n", encoding="utf-8")
        assert watcher.poll(now=0.11) == 0
        assert watcher.poll(now=0.22) == 0
        assert watcher.poll(now=0.32) == 0
        assert watcher.poll(now=0.43) == 0
        assert watcher.poll(now=0.64) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert (
            records["tracked.txt"].content_sha256
            == hashlib.sha256(b"during unknown token\n").hexdigest()
        )
    finally:
        connection.close()


def test_watcher_stop_request_prevents_additional_workspace_work(tmp_path: Path) -> None:
    _root, database, _workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=100.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0, stop_requested=lambda: True) == 0
        assert watcher._states == {}
    finally:
        connection.close()


def test_periodic_reconcile_repairs_a_missed_change_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, workspace_id = _registered(tmp_path)
    connection = connect_database(database)
    try:

        def constant_token(*_args: object, **_kwargs: object) -> WorkspaceMetadataSnapshot:
            return WorkspaceMetadataSnapshot("constant", "constant")

        monkeypatch.setattr(watcher_module, "read_workspace_metadata_snapshot", constant_token)
        watcher = WorkspaceWatcher(
            connection,
            Lock(),
            debounce_seconds=0.1,
            full_reconcile_seconds=1.0,
            retry_seconds=0.2,
            token_deadline_seconds=1.0,
            scan_deadline_seconds=2.0,
        )
        assert watcher.poll(now=0.0) == 0
        assert watcher.poll(now=0.11) == 1

        (root / "tracked.txt").write_text("missed\n", encoding="utf-8")
        assert watcher.poll(now=1.12) == 0
        assert watcher.poll(now=1.23) == 1

        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        assert records["tracked.txt"].content_sha256 == hashlib.sha256(b"missed\n").hexdigest()
    finally:
        connection.close()


def test_daemon_watcher_reconciles_create_modify_delete_without_explicit_scan(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        initial = request_workspace_scan(socket_path, root)
        workspace_id = initial.workspace_id
        tracked = root / "tracked.txt"
        added = root / "added.txt"
        tracked.write_text("changed\n", encoding="utf-8")
        added.write_text("added\n", encoding="utf-8")
        _wait_for_index(
            database,
            workspace_id,
            {
                "added.txt": hashlib.sha256(b"added\n").hexdigest(),
                "tracked.txt": hashlib.sha256(b"changed\n").hexdigest(),
            },
        )

        tracked.unlink()
        added.write_text("updated\n", encoding="utf-8")
        _wait_for_index(
            database,
            workspace_id,
            {"added.txt": hashlib.sha256(b"updated\n").hexdigest()},
        )
        assert request_status(socket_path) == StatusResult(SCHEMA_VERSION, 1, 1)
    finally:
        _stop_server(stop_event, executor, future)
