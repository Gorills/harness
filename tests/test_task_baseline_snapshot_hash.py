import sqlite3
from pathlib import Path

import pytest

from harness.storage import connect_database, initialize_database
from harness.task_baseline import TaskBaselineError, get_task_baseline


def test_task_baseline_reader_rejects_non_hex_snapshot_hash(tmp_path: Path) -> None:
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
            ) VALUES ('task', 'workspace', 'Task', 'working', NULL, 1, 'created', 'created')
            """
        )
        connection.execute(
            """
            INSERT INTO task_baselines(
                task_id, head, branch, captured_at,
                index_is_fresh, index_file_count, index_snapshot_sha256
            ) VALUES ('task', NULL, 'main', 'captured', 1, 0, ?)
            """,
            ("z" * 64,),
        )

        with pytest.raises(TaskBaselineError, match="invalid persisted types"):
            get_task_baseline(connection, "task")
    finally:
        connection.close()
