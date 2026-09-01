from __future__ import annotations

import inspect
import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.index as index_module
from harness.daemon import _content_search_document_count, read_workspace_status
from harness.index import MAX_INDEXED_SEARCH_BODY_BYTES, scan_workspace
from harness.ipc import WorkspaceStatusResult
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _registered(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
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
    return root, connection, workspace.workspace_id


def _status(connection: sqlite3.Connection, root: Path) -> WorkspaceStatusResult:
    return read_workspace_status(connection, [WorkspaceHint(root, "explicit-root")])


def test_status_reports_content_search_document_count(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "service.py").write_text("def ready():\n    return True\n", encoding="utf-8")
        (root / "binary.bin").write_bytes(b"searchable-prefix\x00private-binary")
        (root / "oversized.txt").write_bytes(b"x" * (MAX_INDEXED_SEARCH_BODY_BYTES + 1))
        (root / "generated.out").write_text("diagnostic output\n", encoding="utf-8")
        scan_workspace(connection, workspace_id)

        status = _status(connection, root)
        assert status.indexed_file_count == 5
        assert status.content_search_document_count == 2
        assert status.content_search_document_count < status.indexed_file_count
    finally:
        connection.close()


def test_binary_path_only_file_not_counted_as_content_document(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "binary.bin").write_bytes(b"searchable-prefix\x00private-binary")
        scan_workspace(connection, workspace_id)

        status = _status(connection, root)
        assert status.indexed_file_count == 2
        assert status.content_search_document_count == 1
        assert connection.execute(
            """
            SELECT relative_path FROM indexed_search_documents
            WHERE workspace_id = ? ORDER BY relative_path
            """,
            (workspace_id,),
        ).fetchall() == [("tracked.txt",)]
    finally:
        connection.close()


def test_oversized_path_only_file_not_counted_as_content_document(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "oversized.txt").write_bytes(b"x" * (MAX_INDEXED_SEARCH_BODY_BYTES + 1))
        scan_workspace(connection, workspace_id)

        status = _status(connection, root)
        assert status.indexed_file_count == 2
        assert status.content_search_document_count == 1
        assert connection.execute(
            """
            SELECT relative_path FROM indexed_search_documents
            WHERE workspace_id = ? ORDER BY relative_path
            """,
            (workspace_id,),
        ).fetchall() == [("tracked.txt",)]
    finally:
        connection.close()


def test_content_count_updates_after_authoritative_scan(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        scan_workspace(connection, workspace_id)
        before = _status(connection, root)
        assert before.indexed_file_count == 1
        assert before.content_search_document_count == 1

        (root / "added.py").write_text("value = 1\n", encoding="utf-8")
        stale = _status(connection, root)
        assert stale.indexed_file_count == 1
        assert stale.content_search_document_count == 1

        scan_workspace(connection, workspace_id)
        after = _status(connection, root)
        assert after.indexed_file_count == 2
        assert after.content_search_document_count == 2
    finally:
        connection.close()


def test_status_coverage_query_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "service.py").write_text("print('ok')\n", encoding="utf-8")
        (root / "binary.bin").write_bytes(b"\x00secret")
        scan_workspace(connection, workspace_id)

        def refuse_source(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("project_status must not reread Workspace source")

        monkeypatch.setattr(index_module, "_read_stable_search_text", refuse_source)
        monkeypatch.setattr(index_module, "_inspect_entry", refuse_source)
        monkeypatch.setattr(index_module, "scan_workspace", refuse_source)
        monkeypatch.setattr(index_module, "inspect_workspace_index_freshness", refuse_source)

        traced: list[str] = []

        def capture_sql(statement: str) -> None:
            traced.append(" ".join(statement.split()))

        connection.set_trace_callback(capture_sql)
        try:
            status = _status(connection, root)
        finally:
            connection.set_trace_callback(None)

        assert status.indexed_file_count == 3
        assert status.content_search_document_count == 2
        coverage_sql = [
            statement
            for statement in traced
            if "indexed_search_documents" in statement and "COUNT(*)" in statement
        ]
        assert len(coverage_sql) == 1
        sql = coverage_sql[0]
        assert sql.startswith(
            "SELECT COUNT(*) FROM indexed_search_documents AS documents "
            "JOIN indexed_content_search ON documents.id = indexed_content_search.rowid "
            "WHERE documents.workspace_id ="
        )
        assert "MATCH" not in sql
        assert "body" not in sql
        assert not any("indexed_content_search MATCH" in statement for statement in traced)

        helper_source = inspect.getsource(_content_search_document_count)
        assert "COUNT(*)" in helper_source
        assert "indexed_search_documents" in helper_source
        assert "indexed_content_search" in helper_source
        assert "scan_workspace" not in helper_source
        assert "_read_stable_search_text" not in helper_source
        assert "inspect_workspace_index_freshness" not in helper_source
        assert "open(" not in helper_source

        unbound_sql = (
            "SELECT COUNT(*) FROM indexed_search_documents AS documents "
            "JOIN indexed_content_search ON documents.id = indexed_content_search.rowid "
            "WHERE documents.workspace_id = ?"
        )
        plan = " ".join(
            str(part)
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {unbound_sql}", (workspace_id,)
            ).fetchall()
            for part in row
        ).casefold()
        assert "indexed_search_documents" in plan or "indexed_content_search" in plan
    finally:
        connection.close()
