from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.storage import SCHEMA_VERSION, initialize_database


def _seed_task(connection: sqlite3.Connection) -> None:
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
        ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 4, 'created', 'updated')
        """
    )


def test_schema_v10_operator_event_constraints(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        assert SCHEMA_VERSION == 18
        _seed_task(connection)
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
            ) VALUES ('task', 2, 'operator_feedback', NULL, 'Change spacing', 'feedback')
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
            ) VALUES ('task', 3, 'accepted', NULL, NULL, 'accepted')
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
            ) VALUES ('task', 4, 'cancelled', NULL, NULL, 'cancelled')
            """
        )

        invalid = (
            ("operator_feedback", None),
            ("accepted", "not allowed"),
            ("cancelled", "not allowed"),
        )
        for event_type, feedback in invalid:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO task_events(
                        task_id, task_revision, event_type, checkpoint_id,
                        operator_feedback, created_at
                    ) VALUES ('task', 5, ?, NULL, ?, 'bad')
                    """,
                    (event_type, feedback),
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id,
                    operator_feedback, created_at
                ) VALUES ('task', 5, 'operator_feedback', NULL, ?, 'too-large')
                """,
                ("é" * 513,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id,
                    operator_feedback, created_at
                ) VALUES ('task', 3, 'cancelled', NULL, NULL, 'duplicate-revision-action')
                """
            )
    finally:
        connection.close()


def test_schema_v9_migrates_events_to_v10_without_fabricating_operator_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        _seed_task(connection)
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
            ) VALUES ('task', 1, 'created', NULL, NULL, 'created')
            """
        )
        old_event_id = connection.execute("SELECT id FROM task_events").fetchone()[0]

        connection.execute("DROP INDEX task_events_one_created_per_task_idx")
        connection.execute("DROP INDEX task_events_one_resumed_per_revision_idx")
        connection.execute("DROP INDEX task_events_one_operator_action_per_revision_idx")
        connection.execute("ALTER TABLE task_events RENAME TO task_events_v10_current")
        connection.execute(
            """
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                task_revision INTEGER NOT NULL CHECK (task_revision > 0),
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('created', 'resumed', 'checkpoint')
                ),
                checkpoint_id TEXT UNIQUE,
                created_at TEXT NOT NULL CHECK (created_at <> ''),
                CHECK (
                    (event_type = 'created' AND task_revision = 1 AND checkpoint_id IS NULL)
                    OR (event_type = 'resumed' AND task_revision > 1 AND checkpoint_id IS NULL)
                    OR (event_type = 'checkpoint' AND task_revision > 1 AND checkpoint_id IS NOT NULL)
                ),
                FOREIGN KEY (checkpoint_id, task_id, task_revision)
                    REFERENCES task_checkpoints(id, task_id, task_revision) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(id, task_id, task_revision, event_type, checkpoint_id, created_at)
            SELECT id, task_id, task_revision, event_type, checkpoint_id, created_at
            FROM task_events_v10_current
            """
        )
        connection.execute("DROP TABLE task_events_v10_current")
        connection.execute("CREATE INDEX task_events_task_id_idx ON task_events(task_id, id)")
        connection.execute(
            """
            CREATE UNIQUE INDEX task_events_one_created_per_task_idx
            ON task_events(task_id) WHERE event_type = 'created'
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX task_events_one_resumed_per_revision_idx
            ON task_events(task_id, task_revision) WHERE event_type = 'resumed'
            """
        )
        for (trigger_name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE '%_search_%'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute("DROP TABLE indexed_code_unit_search")
        connection.execute("DROP TABLE indexed_code_units")
        connection.execute("DROP TABLE indexed_code_unit_files")
        connection.execute("DROP TABLE indexed_content_search")
        connection.execute("DROP TABLE indexed_search_documents")
        connection.execute("DROP TABLE project_skill_exclusions")
        connection.execute("DROP TABLE workspace_search_index_dirty_paths")
        connection.execute("DROP TABLE workspace_search_index_state")
        connection.execute("DROP TABLE workspace_index_reconcile")
        connection.execute("DROP TABLE task_search")
        connection.execute("DROP TABLE knowledge_search")
        connection.execute("DROP TABLE task_checkpoint_verification")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 10")
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)
    assert status.schema_version == SCHEMA_VERSION == 18
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            """
            SELECT id, task_id, task_revision, event_type, checkpoint_id,
                   operator_feedback, created_at
            FROM task_events
            """
        ).fetchall() == [(old_event_id, "task", 1, "created", None, None, "created")]
        assert connection.execute(
            """
            SELECT COUNT(*) FROM task_events
            WHERE event_type IN ('accepted', 'operator_feedback', 'cancelled')
            """
        ).fetchone() == (0,)
    finally:
        connection.close()
