from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

import harness.git_workspace as git_workspace
import harness.task_baseline as task_baseline
from harness.git_workspace import (
    GitWorkspaceDeadlineExceededError,
    GitWorkspaceRuntimeIdentity,
    inspect_git_workspace_runtime_identity,
)
from harness.task_baseline import TaskBaselineTimeoutError


def test_runtime_identity_caps_rev_parse_to_remaining_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    normalized_repository = repository.resolve(strict=True)
    observed_timeouts: list[float] = []

    def bounded_git(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float)
        observed_timeouts.append(timeout)
        outputs = {
            "--show-toplevel": f"{normalized_repository}\n".encode(),
            "--git-common-dir": b".git\n",
            "--git-dir": b".git\n",
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=outputs[command[-1]],
            stderr=b"",
        )

    monkeypatch.setattr(git_workspace, "monotonic", lambda: 10.0)
    monkeypatch.setattr(subprocess, "run", bounded_git)

    identity = inspect_git_workspace_runtime_identity(repository, deadline=10.25)

    assert identity.layout.workspace_root == normalized_repository
    assert observed_timeouts == [0.25, 0.25, 0.25]


def test_runtime_identity_rejects_expired_deadline_before_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def unexpected_git(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("Git must not start after the caller deadline expires")

    monkeypatch.setattr(git_workspace, "monotonic", lambda: 10.0)
    monkeypatch.setattr(subprocess, "run", unexpected_git)

    with pytest.raises(GitWorkspaceDeadlineExceededError, match="deadline exceeded"):
        inspect_git_workspace_runtime_identity(repository, deadline=10.0)


def test_task_baseline_identity_check_rejects_deadline_overrun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    identity = GitWorkspaceRuntimeIdentity(
        layout=git_workspace.GitWorkspaceLayout(
            workspace_root=workspace_root,
            git_common_dir=workspace_root,
        ),
        git_dir=workspace_root,
        workspace_root_identity=(1, 1),
        git_dir_identity=(1, 1),
        git_common_dir_identity=(1, 1),
    )
    current_time = 100.0

    def identity_inspection(
        path: Path,
        *,
        deadline: float | None = None,
    ) -> GitWorkspaceRuntimeIdentity:
        nonlocal current_time
        assert path == workspace_root
        assert deadline == 130.0
        current_time = 130.0
        return identity

    monkeypatch.setattr(
        task_baseline,
        "inspect_git_workspace_runtime_identity",
        identity_inspection,
    )
    monkeypatch.setattr(task_baseline, "monotonic", lambda: current_time)

    with pytest.raises(TaskBaselineTimeoutError, match="capture deadline exceeded"):
        task_baseline._inspect_git_runtime_identity(workspace_root, deadline=130.0)
