import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.registry import (
    ProjectNotFoundError,
    VisibilityMode,
    VisibilityModeConflictError,
    WorkspaceRegistrationConflictError,
    create_project,
    get_project,
    list_workspaces,
    register_workspace,
)
from harness.storage import connect_database, initialize_database


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _initialize_repository(base: Path) -> Path:
    repository = base / "repository"
    repository.mkdir()
    _git(repository, "init")
    (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=harness@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "initial",
    )
    return repository


def _open_database(path: Path) -> sqlite3.Connection:
    initialize_database(path)
    return connect_database(path)


def test_create_project_defaults_to_normal_and_persists(tmp_path: Path) -> None:
    connection = _open_database(tmp_path / "harness.db")
    try:
        project = create_project(connection)

        assert project.visibility_mode is VisibilityMode.NORMAL
        assert get_project(connection, project.project_id) == project
        assert connection.execute(
            "SELECT visibility_mode FROM projects WHERE id = ?", (project.project_id,)
        ).fetchone() == ("normal",)
    finally:
        connection.close()


def test_register_workspace_persists_canonical_git_identity_idempotently(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    nested = repository / "src" / "package"
    nested.mkdir(parents=True)
    connection = _open_database(tmp_path / "harness.db")
    try:
        project = create_project(connection)

        first = register_workspace(connection, project_id=project.project_id, path=nested)
        second = register_workspace(connection, project_id=project.project_id, path=repository)

        assert second == first
        assert first.workspace_root == repository.resolve(strict=True)
        assert first.git_common_dir == (repository / ".git").resolve(strict=True)
        assert list_workspaces(connection, project_id=project.project_id) == (first,)
    finally:
        connection.close()


def test_linked_worktrees_register_as_distinct_workspaces_with_shared_common_dir(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path)
    linked = tmp_path / "linked"
    _git(repository, "worktree", "add", "-b", "linked-test", str(linked))
    connection = _open_database(tmp_path / "harness.db")
    try:
        project = create_project(connection)

        primary = register_workspace(connection, project_id=project.project_id, path=repository)
        secondary = register_workspace(connection, project_id=project.project_id, path=linked)

        assert primary.workspace_id != secondary.workspace_id
        assert primary.workspace_root != secondary.workspace_root
        assert primary.git_common_dir == secondary.git_common_dir
        assert set(list_workspaces(connection, project_id=project.project_id)) == {
            primary,
            secondary,
        }
    finally:
        connection.close()


def test_registration_rejects_unknown_project_without_persisting_workspace(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    connection = _open_database(tmp_path / "harness.db")
    try:
        with pytest.raises(ProjectNotFoundError, match="project is not registered"):
            register_workspace(connection, project_id="missing", path=repository)

        assert list_workspaces(connection) == ()
    finally:
        connection.close()


def test_registration_rejects_reassigning_registered_root_to_another_project(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path)
    connection = _open_database(tmp_path / "harness.db")
    try:
        first_project = create_project(connection)
        second_project = create_project(connection)
        registered = register_workspace(
            connection, project_id=first_project.project_id, path=repository
        )

        with pytest.raises(WorkspaceRegistrationConflictError, match="already registered"):
            register_workspace(connection, project_id=second_project.project_id, path=repository)

        assert list_workspaces(connection) == (registered,)
    finally:
        connection.close()


def test_registration_rejects_conflicting_visibility_for_shared_common_dir(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path)
    linked = tmp_path / "linked"
    _git(repository, "worktree", "add", "-b", "linked-test", str(linked))
    connection = _open_database(tmp_path / "harness.db")
    try:
        normal_project = create_project(connection)
        hidden_project_id = "hidden-project-fixture"
        connection.execute(
            "INSERT INTO projects(id, visibility_mode) VALUES (?, 'hidden')",
            (hidden_project_id,),
        )
        registered = register_workspace(
            connection, project_id=normal_project.project_id, path=repository
        )

        with pytest.raises(VisibilityModeConflictError, match="must share visibility mode"):
            register_workspace(connection, project_id=hidden_project_id, path=linked)

        assert list_workspaces(connection) == (registered,)
    finally:
        connection.close()


def test_list_workspaces_supports_stable_positive_limit(tmp_path: Path) -> None:
    connection = _open_database(tmp_path / "harness.db")
    try:
        project = create_project(connection)
        roots: list[Path] = []
        for index in range(3):
            root = tmp_path / f"repo-{index}"
            root.mkdir()
            _git(root, "init")
            roots.append(root)
        registered = tuple(
            register_workspace(connection, project_id=project.project_id, path=root)
            for root in roots
        )

        expected = tuple(sorted(registered, key=lambda item: item.workspace_id))[:2]
        assert list_workspaces(connection, limit=2) == expected
        assert list_workspaces(connection, project_id=project.project_id, limit=2) == expected
        for invalid in (0, -1, True):
            with pytest.raises(Exception, match="positive integer"):
                list_workspaces(connection, limit=invalid)
    finally:
        connection.close()
