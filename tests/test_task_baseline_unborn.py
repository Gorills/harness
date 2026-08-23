from pathlib import Path
import subprocess

from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.tasks import create_task_with_baseline


def test_task_baseline_supports_unborn_head(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)

        created = create_task_with_baseline(connection, workspace.workspace_id, "Unborn")

        assert created.baseline.snapshot.head is None
        assert created.baseline.snapshot.branch == "main"
        assert created.task.revision == 1
    finally:
        connection.close()
