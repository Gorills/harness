from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import harness.tasks as tasks
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import TaskBaselineError
from harness.tasks import create_task_record, get_working_task


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def test_create_task_record_rolls_back_when_baseline_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)

    def fail_persistence(*args: object, **kwargs: object) -> object:
        raise TaskBaselineError("fixture persistence failure")

    monkeypatch.setattr(tasks, "persist_task_baseline", fail_persistence)
    try:
        with pytest.raises(TaskBaselineError, match="fixture persistence failure"):
            create_task_record(connection, workspace.workspace_id, "Atomic creation")

        assert get_working_task(connection, workspace.workspace_id) is None
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (0,)
    finally:
        connection.close()
