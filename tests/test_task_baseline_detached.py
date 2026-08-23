from pathlib import Path
import subprocess

from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.tasks import create_task_with_baseline


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_task_baseline_supports_detached_head(tmp_path: Path) -> None:
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
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--detach", head)

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)

        created = create_task_with_baseline(connection, workspace.workspace_id, "Detached")

        assert created.baseline.snapshot.head == head
        assert created.baseline.snapshot.branch is None
    finally:
        connection.close()
