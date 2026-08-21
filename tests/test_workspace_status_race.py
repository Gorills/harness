from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.daemon import read_workspace_status
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint, WorkspaceResolutionError


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _initialize_repository(path: Path, filename: str) -> None:
    path.mkdir()
    _git(path, "init")
    (path / filename).write_text("content\n", encoding="utf-8")
    _git(path, "add", filename)
    _git(
        path,
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


def test_workspace_status_rejects_repository_replacement_during_live_git_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    _initialize_repository(root, "registered.txt")
    _initialize_repository(replacement, "replacement.txt")

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)

        original_run = subprocess.run
        parked = tmp_path / "registered-before-swap"
        swapped = False

        def swapping_run(
            args: list[str],
            *,
            cwd: Path,
            check: bool,
            capture_output: bool,
            env: dict[str, str],
            timeout: float,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal swapped
            if not swapped and args[:2] == ["git", "status"]:
                root.rename(parked)
                replacement.rename(root)
                swapped = True
            return original_run(
                args,
                cwd=cwd,
                check=check,
                capture_output=capture_output,
                env=env,
                timeout=timeout,
            )

        monkeypatch.setattr(subprocess, "run", swapping_run)

        with pytest.raises(WorkspaceResolutionError, match="identity changed during status read"):
            read_workspace_status(
                connection,
                [WorkspaceHint(root, "explicit-root")],
            )

        assert swapped
        assert workspace.workspace_root == root.resolve()
    finally:
        connection.close()
