from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import TaskBaselineError
from harness.tasks import create_task_with_baseline, get_working_task


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def test_corrupt_persisted_index_aborts_baseline_task_creation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE indexed_files SET kind = 'corrupt' WHERE workspace_id = ?",
            (workspace.workspace_id,),
        )

        with pytest.raises(TaskBaselineError, match="persisted index inspection failed"):
            create_task_with_baseline(connection, workspace.workspace_id, "Must fail closed")

        assert get_working_task(connection, workspace.workspace_id) is None
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (0,)
    finally:
        connection.close()


def test_non_hex_persisted_index_hash_aborts_baseline_task_creation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE indexed_files SET content_sha256 = ? WHERE workspace_id = ?",
            ("+" + "0" * 63, workspace.workspace_id),
        )

        with pytest.raises(TaskBaselineError, match="persisted index inspection failed"):
            create_task_with_baseline(connection, workspace.workspace_id, "Must fail closed")

        assert get_working_task(connection, workspace.workspace_id) is None
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (0,)
    finally:
        connection.close()
