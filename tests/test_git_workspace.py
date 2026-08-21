import os
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from harness.git_workspace import (
    GitExecutableUnavailableError,
    GitWorkspaceError,
    NotGitWorkspaceError,
    inspect_git_working_tree_status,
    inspect_git_workspace,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _git_output(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return os.fsdecode(result.stdout).strip()


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


def test_inspect_working_tree_status_reports_head_branch_and_dirty_count(tmp_path: Path) -> None:
    repository = _initialize_repository(tmp_path)
    expected_head = _git_output(repository, "rev-parse", "HEAD")
    expected_branch = _git_output(repository, "branch", "--show-current")
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")

    status = inspect_git_working_tree_status(repository)

    assert status.head == expected_head
    assert status.branch == expected_branch
    assert status.dirty_file_count == 2


def test_inspect_working_tree_status_supports_unborn_branch(tmp_path: Path) -> None:
    repository = tmp_path / "empty"
    repository.mkdir()
    _git(repository, "init")

    status = inspect_git_working_tree_status(repository)

    assert status.head is None
    assert status.branch
    assert status.dirty_file_count == 0


def test_working_tree_status_ignores_inherited_git_repository_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _initialize_repository(tmp_path / "target-parent", "target")
    decoy = _initialize_repository(tmp_path / "decoy-parent", "decoy")
    target_head = _git_output(target, "rev-parse", "HEAD")
    (target / "target-only.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_COMMON_DIR", str(decoy / ".git"))

    status = inspect_git_working_tree_status(target)

    assert status.head == target_head
    assert status.dirty_file_count == 1


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

    monkeypatch.setattr(subprocess, "run", unavailable_git)

    with pytest.raises(GitExecutableUnavailableError, match="Git executable is not available"):
        inspect_git_workspace(directory)


def test_inspect_git_workspace_bounds_rev_parse_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def timed_out_git(*args: object, **kwargs: object) -> NoReturn:
        timeout = kwargs.get("timeout")
        assert timeout == 1.5
        raise subprocess.TimeoutExpired(["git"], timeout)

    monkeypatch.setattr(subprocess, "run", timed_out_git)

    with pytest.raises(GitWorkspaceError, match="Git workspace inspection timed out"):
        inspect_git_workspace(repository)


def test_inspect_working_tree_status_bounds_git_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def timed_out_git(*args: object, **kwargs: object) -> NoReturn:
        timeout = kwargs.get("timeout")
        assert timeout == 1.5
        raise subprocess.TimeoutExpired(["git"], timeout)

    monkeypatch.setattr(subprocess, "run", timed_out_git)

    with pytest.raises(GitWorkspaceError, match="Git status inspection timed out"):
        inspect_git_working_tree_status(repository)
