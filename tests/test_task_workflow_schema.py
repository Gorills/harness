from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.storage import SCHEMA_VERSION, initialize_database


def test_schema_v7_task_event_lifecycle_constraints(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
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
            ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 3, 'created', 'updated')
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
            VALUES ('task', 1, 'created', NULL, 'created')
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
            VALUES ('task', 3, 'resumed', NULL, 'updated')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id, created_at
                ) VALUES ('task', 2, 'created', NULL, 'bad')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id, created_at
                ) VALUES ('task', 3, 'resumed', 'checkpoint', 'bad')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id, created_at
                ) VALUES ('task', 1, 'checkpoint', NULL, 'bad')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(task_id, task_revision, event_type, created_at)
                VALUES ('task', 1, 'created', 'duplicate')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(task_id, task_revision, event_type, created_at)
                VALUES ('task', 3, 'resumed', 'duplicate')
                """
            )
    finally:
        connection.close()


def test_schema_v6_migrates_checkpoint_events_without_fabricating_lifecycle_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project-v6')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace-v6', 'project-v6', '/v6/repo', '/v6/repo/.git')
            """
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
            ) VALUES (
                'task-v6', 'workspace-v6', 'Existing v6 task', 'working', NULL, 2,
                'created', 'updated'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_checkpoints(
                id, task_id, task_revision, state, wait_reason, summary, next_step,
                created_at, baseline_head, current_head, current_branch,
                current_dirty_path_count
            ) VALUES (
                'checkpoint-v6', 'task-v6', 2, 'working', NULL, 'Existing checkpoint', NULL,
                'checkpoint-time', NULL, NULL, 'main', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
            VALUES ('task-v6', 2, 'checkpoint', 'checkpoint-v6', 'checkpoint-time')
            """
        )
        old_event_id = connection.execute("SELECT id FROM task_events").fetchone()[0]

        connection.execute("DROP INDEX task_events_one_created_per_task_idx")
        connection.execute("DROP INDEX task_events_one_resumed_per_revision_idx")
        connection.execute("ALTER TABLE task_events RENAME TO task_events_v7_current")
        connection.execute(
            """
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                task_revision INTEGER NOT NULL CHECK (task_revision > 1),
                event_type TEXT NOT NULL CHECK (event_type = 'checkpoint'),
                checkpoint_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL CHECK (created_at <> ''),
                FOREIGN KEY (checkpoint_id, task_id, task_revision)
                    REFERENCES task_checkpoints(id, task_id, task_revision) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_events(
                id, task_id, task_revision, event_type, checkpoint_id, created_at
            )
            SELECT id, task_id, task_revision, event_type, checkpoint_id, created_at
            FROM task_events_v7_current
            """
        )
        connection.execute("DROP TABLE task_events_v7_current")
        connection.execute("CREATE INDEX task_events_task_id_idx ON task_events(task_id, id)")
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
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION == 12
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            """
            SELECT id, task_id, task_revision, event_type, checkpoint_id, created_at
            FROM task_events
            """
        ).fetchall() == [
            (
                old_event_id,
                "task-v6",
                2,
                "checkpoint",
                "checkpoint-v6",
                "checkpoint-time",
            )
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE event_type = 'created'"
        ).fetchone() == (0,)
        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
            VALUES ('task-v6', 3, 'resumed', NULL, 'resumed-time')
            """
        )
        assert (
            connection.execute(
                "SELECT id FROM task_events WHERE event_type = 'resumed'"
            ).fetchone()[0]
            > old_event_id
        )
    finally:
        connection.close()
