from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.knowledge import KnowledgeCorruptionError, get_knowledge_card
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database


def test_schema_v8_knowledge_constraints_and_tables(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        assert SCHEMA_VERSION == 13
        connection.execute("INSERT INTO projects(id) VALUES ('project')")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO knowledge_cards(
                    id, project_id, kind, title, body, source_type,
                    source_task_id, source_checkpoint_id,
                    created_at, updated_at, freshness
                ) VALUES (
                    'bad-provenance', 'project', 'invariant', 'Title', 'Body',
                    'agent_asserted', NULL, NULL, 'now', 'now', 'fresh'
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO knowledge_cards(
                    id, project_id, kind, title, body, source_type,
                    created_at, updated_at, freshness
                ) VALUES (
                    'bad-freshness', 'project', 'invariant', 'Title', 'Body',
                    'operator', 'now', 'now', 'unknown'
                )
                """
            )

        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type,
                created_at, updated_at, freshness
            ) VALUES ('card', 'project', 'behavior', 'Title', 'Body', 'operator', 'now', 'now', 'fresh')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO knowledge_anchors(
                    knowledge_id, workspace_id, relative_path, symbol,
                    fingerprint_kind, content_sha256
                ) VALUES ('card', 'workspace', 'src/app.py', '', 'directory', ?)
                """,
                ("0" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO knowledge_anchors(
                    knowledge_id, workspace_id, relative_path, symbol,
                    fingerprint_kind, content_sha256
                ) VALUES ('card', 'workspace', 'src/app.py', '', 'file', 'short')
                """
            )
    finally:
        connection.close()


def test_schema_v7_migrates_to_v8_without_losing_task_history(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
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
            INSERT INTO tasks(
                id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
            ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 2, 'created', 'updated')
            """
        )
        connection.execute(
            """
            INSERT INTO task_checkpoints(
                id, task_id, task_revision, state, wait_reason, summary, next_step,
                created_at, baseline_head, current_head, current_branch,
                current_dirty_path_count
            ) VALUES (
                'checkpoint', 'task', 2, 'working', NULL, 'Summary', NULL,
                'checkpoint-time', NULL, NULL, 'main', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
            VALUES ('task', 2, 'checkpoint', 'checkpoint', 'checkpoint-time')
            """
        )
        event_id = connection.execute("SELECT id FROM task_events").fetchone()[0]
        connection.execute("DROP INDEX knowledge_anchors_workspace_idx")
        connection.execute("DROP INDEX knowledge_cards_freshness_idx")
        connection.execute("DROP INDEX knowledge_cards_project_idx")
        connection.execute("DROP TABLE task_stack_hints")
        connection.execute("DROP TABLE knowledge_anchors")
        connection.execute("DROP TABLE knowledge_cards")
        for (trigger_name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE '%_search_%'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute("DROP TABLE task_search")
        connection.execute("DROP TABLE knowledge_search")
        connection.execute("DROP TABLE task_checkpoint_verification")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 8")
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION == 13
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id, revision FROM tasks").fetchall() == [("task", 2)]
        assert connection.execute("SELECT id FROM task_checkpoints").fetchall() == [("checkpoint",)]
        assert connection.execute("SELECT id FROM task_events").fetchall() == [(event_id,)]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert {"knowledge_cards", "knowledge_anchors"} <= tables
    finally:
        connection.close()


def test_knowledge_reader_rejects_unsafe_persisted_anchor_path(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type,
                created_at, updated_at, freshness
            ) VALUES ('card', 'project', 'caveat', 'Title', 'Body', 'operator', 'now', 'now', 'fresh')
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_anchors(
                knowledge_id, workspace_id, relative_path, symbol,
                fingerprint_kind, content_sha256
            ) VALUES ('card', 'workspace', '../escape', '', 'file', ?)
            """,
            ("0" * 64,),
        )

        with pytest.raises(KnowledgeCorruptionError, match="unsafe knowledge anchor path"):
            get_knowledge_card(connection, "card")
    finally:
        connection.close()
