import sqlite3
from pathlib import Path

import pytest

from harness.storage import connect_database, initialize_database
from harness.task_baseline import TaskBaselineError, get_task_baseline


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
        ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 1, 'created', 'created')
        """
    )


def test_task_baseline_schema_enforces_freshness_and_fingerprint_kind(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        _seed_task(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_baselines(
                    task_id, head, branch, captured_at,
                    index_is_fresh, index_file_count, index_snapshot_sha256
                ) VALUES ('task', NULL, 'main', 'captured', 2, 0, ?)
                """,
                ("0" * 64,),
            )

        connection.execute(
            """
            INSERT INTO task_baselines(
                task_id, head, branch, captured_at,
                index_is_fresh, index_file_count, index_snapshot_sha256
            ) VALUES ('task', NULL, 'main', 'captured', 1, 0, ?)
            """,
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_baseline_dirty_paths(
                    task_id, relative_path, original_relative_path,
                    status_code, fingerprint_kind, state_sha256
                ) VALUES ('task', 'file.txt', NULL, ' M', 'unsupported', ?)
                """,
                ("1" * 64,),
            )
    finally:
        connection.close()


def test_task_baseline_reader_rejects_corrupt_unsafe_path(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        _seed_task(connection)
        connection.execute(
            """
            INSERT INTO task_baselines(
                task_id, head, branch, captured_at,
                index_is_fresh, index_file_count, index_snapshot_sha256
            ) VALUES ('task', NULL, 'main', 'captured', 1, 0, ?)
            """,
            ("0" * 64,),
        )
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            INSERT INTO task_baseline_dirty_paths(
                task_id, relative_path, original_relative_path,
                status_code, fingerprint_kind, state_sha256
            ) VALUES ('task', '../escape', NULL, ' M', 'file', ?)
            """,
            ("1" * 64,),
        )

        with pytest.raises(TaskBaselineError, match="unsafe Task baseline path"):
            get_task_baseline(connection, "task")
    finally:
        connection.close()
