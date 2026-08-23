import sqlite3
from pathlib import Path

import pytest

from harness.storage import connect_database, initialize_database


def test_waiting_task_requires_database_level_wait_reason(tmp_path: Path) -> None:
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

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO tasks(
                    id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
                ) VALUES ('task', 'workspace', 'Needs review', 'waiting', NULL, 1, 'now', 'now')
                """
            )
    finally:
        connection.close()
