from __future__ import annotations

import inspect
import os
import sqlite3
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest

import harness.index as index_module
import harness.storage as storage
from harness.daemon import (
    _index_reconcile_provenance,
    read_workspace_status,
    serve_daemon,
)
from harness.index import (
    IndexingError,
    IndexReconcileKind,
    list_indexed_files,
    scan_workspace,
    scan_workspace_paths,
)
from harness.ipc import WorkspaceStatusResult, request_workspace_status
from harness.registry import create_project, get_workspace, register_workspace
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _registered(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection, str]:
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
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    return root, database, connection, workspace.workspace_id


def _status(connection: sqlite3.Connection, root: Path) -> WorkspaceStatusResult:
    return read_workspace_status(connection, [WorkspaceHint(root, "explicit-root")])


def _assert_utc_timestamp(value: str | None) -> str:
    assert value is not None
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    return value


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


def test_index_revision_advances_after_successful_full_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamps = [
        "2026-09-01T12:00:00.000000+00:00",
        "2026-09-01T12:00:01.000000+00:00",
    ]
    clock = iter(stamps)
    monkeypatch.setattr(index_module, "_utc_timestamp", lambda: next(clock))

    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        before = _status(connection, root)
        assert before.index_revision is None
        assert before.last_successful_reconcile_at is None
        assert before.last_reconcile_kind is None
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_index_reconcile WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (0,)

        first = scan_workspace(connection, workspace_id)
        after_first = _status(connection, root)
        assert first.added == 1
        assert after_first.index_revision == 1
        assert after_first.last_reconcile_kind == "full"
        assert after_first.last_successful_reconcile_at == stamps[0]

        second = scan_workspace(connection, workspace_id)
        after_second = _status(connection, root)
        assert (second.added, second.updated, second.removed) == (0, 0, 0)
        assert after_second.index_revision == 2
        assert after_second.last_reconcile_kind == "full"
        assert after_second.last_successful_reconcile_at == stamps[1]
        assert after_second.last_successful_reconcile_at != after_first.last_successful_reconcile_at
    finally:
        connection.close()


def test_index_revision_advances_after_successful_incremental_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamps = [
        "2026-09-01T13:00:00.000000+00:00",
        "2026-09-01T13:00:02.000000+00:00",
    ]
    clock = iter(stamps)
    monkeypatch.setattr(index_module, "_utc_timestamp", lambda: next(clock))

    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        scan_workspace(connection, workspace_id)
        after_full = _status(connection, root)
        assert after_full.index_revision == 1
        assert after_full.last_reconcile_kind == "full"
        assert after_full.last_successful_reconcile_at == stamps[0]

        (root / "added.txt").write_text("added\n", encoding="utf-8")
        incremental = scan_workspace_paths(connection, workspace_id, ("added.txt",))
        after_incremental = _status(connection, root)
        assert incremental.added == 1
        assert after_incremental.index_revision == 2
        assert after_incremental.last_reconcile_kind == "incremental"
        assert after_incremental.last_successful_reconcile_at == stamps[1]
        assert after_incremental.indexed_file_count == after_full.indexed_file_count + 1
    finally:
        connection.close()


def test_persist_snapshot_kind_is_independent_of_expected_existing(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        scan_workspace(connection, workspace_id)
        workspace = get_workspace(connection, workspace_id)
        existing = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        result = index_module._persist_snapshot(
            connection,
            workspace,
            dict(existing),
            eligible_knowledge_ids=frozenset(),
            deadline=None,
            kind=IndexReconcileKind.FULL,
            expected_existing=existing,
        )
        after = _status(connection, root)
        assert (result.added, result.updated, result.removed) == (0, 0, 0)
        assert after.index_revision == 2
        assert after.last_reconcile_kind == "full"
    finally:
        connection.close()


def test_failed_reconcile_does_not_advance_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        scan_workspace(connection, workspace_id)
        before = _status(connection, root)
        assert before.index_revision == 1
        persisted = connection.execute(
            """
            SELECT index_revision, last_successful_reconcile_at, last_reconcile_kind
            FROM workspace_index_reconcile
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone()

        def fail_search(*_args: object, **_kwargs: object) -> None:
            raise IndexingError("synthetic reconcile failure")

        monkeypatch.setattr(index_module, "_reconcile_search_documents", fail_search)
        (root / "added.txt").write_text("added\n", encoding="utf-8")
        with pytest.raises(IndexingError, match="synthetic reconcile failure"):
            scan_workspace(connection, workspace_id)

        after = _status(connection, root)
        assert after.index_revision == before.index_revision
        assert after.last_successful_reconcile_at == before.last_successful_reconcile_at
        assert after.last_reconcile_kind == before.last_reconcile_kind
        assert after.indexed_file_count == before.indexed_file_count
        assert (
            connection.execute(
                """
                SELECT index_revision, last_successful_reconcile_at, last_reconcile_kind
                FROM workspace_index_reconcile
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()
            == persisted
        )
    finally:
        connection.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX daemon provenance slice")
def test_reconcile_provenance_survives_daemon_restart(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered(tmp_path)
    try:
        scan_workspace(connection, workspace_id)
        local = _status(connection, root)
        assert local.index_revision == 1
        assert local.last_reconcile_kind == "full"
        _assert_utc_timestamp(local.last_successful_reconcile_at)
    finally:
        connection.close()

    socket_path = tmp_path / "ipc" / "harness.sock"
    hints = [WorkspaceHint(root, "explicit-root")]
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        first = request_workspace_status(socket_path, hints)
        assert first.workspace_id == workspace_id
        assert first.index_revision == 1
        assert first.last_reconcile_kind == "full"
        assert first.last_successful_reconcile_at == local.last_successful_reconcile_at
    finally:
        _stop_server(stop_event, executor, future)

    stop_event, executor, future = _start_server(database, socket_path)
    try:
        second = request_workspace_status(socket_path, hints)
        assert second.index_revision == first.index_revision
        assert second.last_successful_reconcile_at == first.last_successful_reconcile_at
        assert second.last_reconcile_kind == first.last_reconcile_kind
    finally:
        _stop_server(stop_event, executor, future)


def test_project_status_reads_provenance_without_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        scan_workspace(connection, workspace_id)

        def refuse_scan(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("project_status must not scan the Workspace tree for provenance")

        monkeypatch.setattr(index_module, "scan_workspace", refuse_scan)
        monkeypatch.setattr(index_module, "scan_workspace_paths", refuse_scan)
        monkeypatch.setattr(index_module, "_build_snapshot", refuse_scan)
        monkeypatch.setattr(index_module, "_inspect_entry", refuse_scan)
        monkeypatch.setattr(index_module, "inspect_workspace_index_freshness", refuse_scan)

        traced: list[str] = []

        def capture_sql(statement: str) -> None:
            traced.append(" ".join(statement.split()))

        connection.set_trace_callback(capture_sql)
        try:
            status = _status(connection, root)
        finally:
            connection.set_trace_callback(None)

        assert status.index_revision == 1
        assert status.last_reconcile_kind == "full"
        _assert_utc_timestamp(status.last_successful_reconcile_at)

        provenance_sql = [
            statement
            for statement in traced
            if "workspace_index_reconcile" in statement and "SELECT" in statement
        ]
        assert len(provenance_sql) == 1
        assert "index_revision" in provenance_sql[0]
        assert "last_successful_reconcile_at" in provenance_sql[0]
        assert "last_reconcile_kind" in provenance_sql[0]
        assert not any(
            "indexed_files" in statement and "INSERT" in statement for statement in traced
        )

        helper_source = inspect.getsource(_index_reconcile_provenance)
        assert "workspace_index_reconcile" in helper_source
        assert "scan_workspace" not in helper_source
        assert "_build_snapshot" not in helper_source
        assert "inspect_workspace_index_freshness" not in helper_source
        assert "open(" not in helper_source
        status_source = inspect.getsource(read_workspace_status)
        assert "_index_reconcile_provenance" in status_source
        assert "scan_workspace" not in status_source
        assert "inspect_workspace_index_freshness" not in status_source
    finally:
        connection.close()


def test_schema_v15_adds_reconcile_provenance_without_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "provenance.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 14)
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace', 'project', '/repo', '/repo/.git')
            """
        )
        connection.execute(
            """
            INSERT INTO indexed_files(
                workspace_id, relative_path, kind, size_bytes, content_sha256
            ) VALUES ('workspace', 'tracked.txt', 'file', 8, ?)
            """,
            ("0" * 64,),
        )
        connection.commit()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert "workspace_index_reconcile" not in tables
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", 15)
    status = initialize_database(database)
    assert status.schema_version == 15

    connection = connect_database(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM workspace_index_reconcile").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM indexed_files").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)
    finally:
        connection.close()


def test_schema_v15_migration_failure_rolls_back_provenance_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "provenance-rollback.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 14)
    initialize_database(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 15)
    original_apply = storage._apply_migration

    def fail_after_create(connection: sqlite3.Connection, target_version: int) -> None:
        original_apply(connection, target_version)
        if target_version == 15:
            raise sqlite3.OperationalError("synthetic v15 provenance failure")

    monkeypatch.setattr(storage, "_apply_migration", fail_after_create)
    with pytest.raises(sqlite3.OperationalError, match="synthetic v15 provenance failure"):
        initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (14,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert "workspace_index_reconcile" not in tables
    finally:
        connection.close()
