import os
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

import harness.git_workspace as git_workspace
from harness.git_workspace import (
    GitExecutableUnavailableError,
    NotGitWorkspaceError,
    inspect_git_workspace,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _normalized(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve(strict=True))))


def _initialize_repository(base: Path, name: str = "repository with spaces") -> Path:
    repository = base / name
    repository.mkdir(parents=True)
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


def test_inspect_git_workspace_resolves_root_and_common_dir_from_nested_path(
    tmp_path: Path,
) -> None:
    repository = _initialize_repository(tmp_path)
    nested = repository / "src" / "package"
    nested.mkdir(parents=True)

    layout = inspect_git_workspace(nested)

    assert layout.workspace_root == _normalized(repository)
    assert layout.git_common_dir == _normalized(repository / ".git")


def test_linked_worktree_has_distinct_root_and_shared_common_dir(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    primary_layout = inspect_git_workspace(repository)
    linked = tmp_path / "linked worktree"
    _git(repository, "worktree", "add", "-b", "linked-test", str(linked))
    nested = linked / "nested"
    nested.mkdir()

    linked_layout = inspect_git_workspace(nested)

    assert linked_layout.workspace_root == _normalized(linked)
    assert linked_layout.workspace_root != primary_layout.workspace_root
    assert linked_layout.git_common_dir == primary_layout.git_common_dir


def test_inspection_ignores_inherited_git_repository_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _initialize_repository(tmp_path / "target-parent", "target")
    decoy = _initialize_repository(tmp_path / "decoy-parent", "decoy")
    nested = target / "nested"
    nested.mkdir()
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_COMMON_DIR", str(decoy / ".git"))

    layout = inspect_git_workspace(nested)

    assert layout.workspace_root == _normalized(target)
    assert layout.git_common_dir == _normalized(target / ".git")


def test_inspect_git_workspace_rejects_non_git_directory(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repository"
    outside.mkdir()

    with pytest.raises(NotGitWorkspaceError, match="not inside an inspectable Git worktree"):
        inspect_git_workspace(outside)


def test_inspect_git_workspace_rejects_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(NotGitWorkspaceError, match="does not exist or cannot be resolved"):
        inspect_git_workspace(missing)


def test_inspect_git_workspace_reports_missing_git_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "workspace"
    directory.mkdir()

    def unavailable_git(*args: object, **kwargs: object) -> NoReturn:
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_workspace.subprocess, "run", unavailable_git)

    with pytest.raises(GitExecutableUnavailableError, match="Git executable is not available"):
        inspect_git_workspace(directory)
