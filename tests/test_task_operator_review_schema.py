from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import harness.storage as storage
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
    assert initialize_database(database).schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    with monkeypatch.context() as previous_schema:
        previous_schema.setattr(storage, "SCHEMA_VERSION", 9)
        initialize_database(database)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        _seed_task(connection)
        connection.execute(
            """
            INSERT INTO task_events(
                task_id, task_revision, event_type, checkpoint_id, created_at
            ) VALUES ('task', 1, 'created', NULL, 'created')
            """
        )
        old_event_id = connection.execute("SELECT id FROM task_events").fetchone()[0]

        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)
    assert status.schema_version == SCHEMA_VERSION
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
