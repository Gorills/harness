from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import harness.storage as storage
from harness.storage import connect_database, initialize_database


def test_schema_v11_backfills_and_tracks_semantic_search_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 10)
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
            INSERT INTO tasks(id, workspace_id, title, state, wait_reason, revision, created_at, updated_at)
            VALUES ('task', 'workspace', 'Refresh migration task', 'working', NULL, 3, 'c', 'u')
            """
        )
        connection.execute(
            """
            INSERT INTO task_checkpoints(
                id, task_id, task_revision, state, wait_reason, summary, next_step,
                created_at, baseline_head, current_head, current_branch, current_dirty_path_count
            ) VALUES (
                'checkpoint', 'task', 2, 'working', NULL,
                'Rotate refresh token', 'Verify old credential rejection', 'cp',
                NULL, NULL, 'main', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at)
            VALUES ('task', 1, 'created', NULL, NULL, 'c')
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at)
            VALUES ('task', 2, 'checkpoint', 'checkpoint', NULL, 'cp')
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at)
            VALUES ('task', 3, 'operator_feedback', NULL, 'Preserve session marker', 'f')
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type,
                created_at, updated_at, freshness
            ) VALUES (
                'knowledge', 'project', 'invariant', 'Refresh invariant',
                'Old refresh credential becomes invalid', 'operator', 'c', 'u', 'fresh'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", 11)
    status = initialize_database(database)
    assert status.schema_version == 11

    connection = connect_database(database)
    try:
        assert connection.execute(
            "SELECT knowledge_id FROM knowledge_search WHERE knowledge_search MATCH 'refresh'"
        ).fetchall() == [("knowledge",)]
        task_fragments = connection.execute(
            """
            SELECT fragment_ref
            FROM task_search
            WHERE task_search MATCH 'refresh OR session'
            ORDER BY fragment_ref
            """
        ).fetchall()
        assert task_fragments == [
            ("checkpoint:checkpoint",),
            ("event:3",),
            ("task:task",),
        ]

        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type,
                created_at, updated_at, freshness
            ) VALUES ('new-card', 'project', 'caveat', 'Cache invalidation',
                      'Refresh cache carefully', 'operator', 'c', 'u', 'fresh')
            """
        )
        assert connection.execute(
            "SELECT knowledge_id FROM knowledge_search WHERE knowledge_search MATCH 'cache'"
        ).fetchall() == [("new-card",)]
        connection.execute(
            "UPDATE knowledge_cards SET body = 'Completely different' WHERE id = 'new-card'"
        )
        assert connection.execute(
            "SELECT knowledge_id FROM knowledge_search WHERE knowledge_search MATCH 'cache'"
        ).fetchall() == [("new-card",)]
        connection.execute(
            "UPDATE knowledge_cards SET title = 'Different title' WHERE id = 'new-card'"
        )
        assert (
            connection.execute(
                "SELECT knowledge_id FROM knowledge_search WHERE knowledge_search MATCH 'cache'"
            ).fetchall()
            == []
        )

        connection.execute(
            """
            INSERT INTO tasks(id, workspace_id, title, state, wait_reason, revision, created_at, updated_at)
            VALUES ('new-task', 'workspace', 'Cookie repair', 'completed', NULL, 1, 'c', 'u')
            """
        )
        assert connection.execute(
            "SELECT task_id FROM task_search WHERE task_search MATCH 'cookie'"
        ).fetchall() == [("new-task",)]
        connection.execute("DELETE FROM tasks WHERE id = 'new-task'")
        assert (
            connection.execute(
                "SELECT task_id FROM task_search WHERE task_search MATCH 'cookie'"
            ).fetchall()
            == []
        )
    finally:
        connection.close()


def test_schema_v11_migration_failure_rolls_back_derived_search_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 10)
    initialize_database(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 11)

    def fail_after_first_trigger(connection: sqlite3.Connection, _script: str) -> None:
        connection.execute(
            """
            CREATE TRIGGER partial_search_trigger
            AFTER INSERT ON knowledge_cards
            BEGIN
                SELECT NEW.id;
            END
            """
        )
        raise sqlite3.OperationalError("synthetic v11 trigger failure")

    monkeypatch.setattr(storage, "_execute_sql_script_in_transaction", fail_after_first_trigger)
    with pytest.raises(sqlite3.OperationalError, match="synthetic v11 trigger failure"):
        initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (10,)
        search_objects = connection.execute(
            """
            SELECT name
            FROM sqlite_schema
            WHERE name IN ('knowledge_search', 'task_search', 'partial_search_trigger')
            ORDER BY name
            """
        ).fetchall()
        assert search_objects == []
    finally:
        connection.close()


def test_schema_v14_adds_rebuildable_content_search_without_fabricated_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "content-search.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 13)
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
            ) VALUES ('workspace', 'service.py', 'file', 12, ?)
            """,
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", 14)
    status = initialize_database(database)
    assert status.schema_version == 14

    connection = connect_database(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM indexed_search_documents").fetchone() == (
            0,
        )
        assert connection.execute("SELECT COUNT(*) FROM indexed_content_search").fetchone() == (0,)
        assert connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'trigger' AND name LIKE 'indexed_content_search_%'
            ORDER BY name
            """
        ).fetchall() == [
            ("indexed_content_search_delete",),
        ]
    finally:
        connection.close()


def test_schema_v14_migration_failure_rolls_back_content_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "content-search-rollback.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 13)
    initialize_database(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 14)

    def fail_trigger_install(connection: sqlite3.Connection, _script: str) -> None:
        connection.execute(
            """
            CREATE TRIGGER partial_content_search_trigger
            AFTER INSERT ON indexed_search_documents
            BEGIN
                SELECT NEW.id;
            END
            """
        )
        raise sqlite3.OperationalError("synthetic v14 trigger failure")

    monkeypatch.setattr(storage, "_execute_sql_script_in_transaction", fail_trigger_install)
    with pytest.raises(sqlite3.OperationalError, match="synthetic v14 trigger failure"):
        initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (13,)
        assert (
            connection.execute(
                """
            SELECT name FROM sqlite_schema
            WHERE name IN (
                'indexed_search_documents', 'indexed_content_search',
                'partial_content_search_trigger'
            )
            """
            ).fetchall()
            == []
        )
    finally:
        connection.close()
