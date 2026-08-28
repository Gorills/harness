from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.storage import SCHEMA_VERSION, connect_database, initialize_database


def test_schema_v9_task_stack_hint_constraints_and_cascade(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        assert SCHEMA_VERSION == 12
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
            ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 1, 'created', 'updated')
            """
        )
        connection.execute(
            "INSERT INTO task_stack_hints(task_id, position, hint) VALUES ('task', 0, 'fastapi')"
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_stack_hints(task_id, position, hint) VALUES ('task', 1, 'fastapi')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_stack_hints(task_id, position, hint) VALUES ('task', 16, 'postgres')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_stack_hints(task_id, position, hint) VALUES ('task', 1, '')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_stack_hints(task_id, position, hint) VALUES ('task', 1, ?)",
                ("é" * 65,),
            )

        connection.execute("DELETE FROM tasks WHERE id = 'task'")
        assert connection.execute("SELECT COUNT(*) FROM task_stack_hints").fetchone() == (0,)
    finally:
        connection.close()


def test_schema_v8_migrates_to_v9_without_losing_tasks(tmp_path: Path) -> None:
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
            ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 1, 'created', 'updated')
            """
        )
        connection.execute("DROP TABLE task_stack_hints")
        for (trigger_name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE '%_search_%'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute("DROP TABLE task_search")
        connection.execute("DROP TABLE knowledge_search")
        connection.execute("DROP TABLE task_checkpoint_verification")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 9")
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION == 12
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id, title FROM tasks").fetchall() == [("task", "Task")]
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'task_stack_hints'"
        ).fetchone() == ("task_stack_hints",)
    finally:
        connection.close()
