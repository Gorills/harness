from __future__ import annotations

import subprocess
from pathlib import Path

from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import get_task_baseline
from harness.tasks import create_task_record


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def test_create_task_record_always_persists_required_baseline(tmp_path: Path) -> None:
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

        task = create_task_record(connection, workspace.workspace_id, "Canonical creation")
        baseline = get_task_baseline(connection, task.task_id)

        assert baseline.task_id == task.task_id
        assert baseline.snapshot.workspace_id == workspace.workspace_id
        assert task.revision == 1
        assert baseline.snapshot.captured_at == task.created_at == task.updated_at
    finally:
        connection.close()
