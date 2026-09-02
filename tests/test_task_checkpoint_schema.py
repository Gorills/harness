from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.task_checkpoints import TaskCheckpointError, get_task_checkpoint


def test_schema_v7_checkpoint_constraints_and_cascade(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
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

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_checkpoints(
                    id, task_id, task_revision, state, wait_reason, summary, next_step,
                    created_at, baseline_head, current_head, current_branch,
                    current_dirty_path_count
                ) VALUES (
                    'bad', 'task', 2, 'waiting', NULL, 'Summary', 'Next',
                    'now', NULL, NULL, NULL, 0
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
                'checkpoint', 'task', 2, 'working', NULL, 'Summary', NULL,
                'now', NULL, NULL, NULL, 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_checkpoint_changed_paths(checkpoint_id, relative_path)
            VALUES ('checkpoint', 'src/app.py')
            """
        )
        connection.execute(
            "INSERT INTO task_checkpoint_verification(checkpoint_id,position,name,status,evidence,source) VALUES ('checkpoint',0,'tests','passed','pytest: passed','agent_reported')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO task_checkpoint_verification(checkpoint_id,position,name,status,evidence,source) VALUES ('checkpoint',1,'bad','unknown','none','agent_reported')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id, created_at
                ) VALUES ('task', 3, 'checkpoint', 'checkpoint', 'now')
                """
            )

        connection.execute(
            """
            INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
            VALUES ('task', 2, 'checkpoint', 'checkpoint', 'now')
            """
        )
        connection.execute("DELETE FROM tasks WHERE id = 'task'")
        assert connection.execute("SELECT COUNT(*) FROM task_checkpoints").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM task_checkpoint_changed_paths"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM task_checkpoint_verification"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone() == (0,)
    finally:
        connection.close()


def test_schema_version_includes_knowledge_schema() -> None:
    assert SCHEMA_VERSION == 17


def test_checkpoint_reader_rejects_corrupt_unsafe_changed_path(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
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
                'now', NULL, NULL, NULL, 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_checkpoint_changed_paths(checkpoint_id, relative_path)
            VALUES ('checkpoint', '../escape')
            """
        )

        with pytest.raises(TaskCheckpointError, match="unsafe Task checkpoint changed path"):
            get_task_checkpoint(connection, "checkpoint")
    finally:
        connection.close()
