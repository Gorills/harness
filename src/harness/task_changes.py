from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic

from harness.git_workspace import (
    GitWorkspaceDeadlineExceededError,
    GitWorkspaceError,
    GitWorkspaceRuntimeIdentity,
    _git_environment,
    inspect_git_workspace_runtime_identity,
)
from harness.registry import WorkspaceRecord, get_workspace
from harness.task_baseline import (
    TaskBaselineChangedError,
    TaskBaselineDirtyPath,
    TaskBaselineError,
    TaskBaselineFingerprintKind,
    TaskBaselineRecord,
    TaskBaselineTimeoutError,
    TaskGitState,
    capture_task_git_state,
    get_task_baseline,
)

_CHANGED_FILES_TIMEOUT_SECONDS = 30.0


class TaskChangedFilesError(RuntimeError):
    """Base class for mechanical Task changed-file calculation failures."""


class TaskChangedFilesChangedError(TaskChangedFilesError):
    """Raised when Workspace state changes during changed-file calculation."""


class TaskChangedFilesTimeoutError(TaskChangedFilesError):
    """Raised when changed-file calculation exceeds its execution deadline."""


@dataclass(frozen=True, slots=True)
class TaskChangedFiles:
    """Stable net changed paths relative to one Task's mechanical baseline."""

    task_id: str
    workspace_id: str
    baseline_head: str | None
    current_head: str | None
    current_branch: str | None
    current_dirty_path_count: int
    relative_paths: tuple[str, ...]


def calculate_task_changed_files(
    connection: sqlite3.Connection,
    task_id: str,
) -> TaskChangedFiles:
    """Calculate stable net changed paths without mutating Task or Workspace state."""
    deadline = monotonic() + _CHANGED_FILES_TIMEOUT_SECONDS
    baseline = _load_baseline(connection, task_id, deadline=deadline)
    workspace = get_workspace(connection, baseline.snapshot.workspace_id)
    _require_deadline(deadline)

    identity_before = _inspect_runtime_identity(workspace.workspace_root, deadline=deadline)
    _require_registered_identity(workspace, identity_before)

    first_git = _capture_git_state(workspace.workspace_root, deadline=deadline)
    committed_paths = _committed_changed_paths(
        workspace.workspace_root,
        baseline.snapshot.head,
        first_git.head,
        deadline=deadline,
    )
    second_git = _capture_git_state(workspace.workspace_root, deadline=deadline)
    if first_git != second_git:
        raise TaskChangedFilesChangedError(
            "Workspace Git state changed during Task changed-file calculation"
        )

    identity_after = _inspect_runtime_identity(workspace.workspace_root, deadline=deadline)
    if identity_after != identity_before:
        raise TaskChangedFilesChangedError(
            "Workspace Git identity changed during Task changed-file calculation"
        )
    current_workspace = get_workspace(connection, workspace.workspace_id)
    _require_deadline(deadline)
    if current_workspace != workspace:
        raise TaskChangedFilesChangedError(
            "Workspace registry identity changed during Task changed-file calculation"
        )

    relative_paths = _merge_changed_paths(
        baseline.snapshot.dirty_paths,
        first_git,
        committed_paths,
        deadline=deadline,
    )
    _require_deadline(deadline)
    return TaskChangedFiles(
        task_id=task_id,
        workspace_id=workspace.workspace_id,
        baseline_head=baseline.snapshot.head,
        current_head=first_git.head,
        current_branch=first_git.branch,
        current_dirty_path_count=len(first_git.dirty_paths),
        relative_paths=relative_paths,
    )


def _load_baseline(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    deadline: float,
) -> TaskBaselineRecord:
    try:
        return get_task_baseline(connection, task_id, deadline=deadline)
    except TaskBaselineTimeoutError as exc:
        raise TaskChangedFilesTimeoutError("Task changed-file baseline read timed out") from exc
    except TaskBaselineError as exc:
        raise TaskChangedFilesError("Task changed-file baseline read failed") from exc


def _capture_git_state(workspace_root: Path, *, deadline: float) -> TaskGitState:
    try:
        return capture_task_git_state(workspace_root, deadline=deadline)
    except TaskBaselineTimeoutError as exc:
        raise TaskChangedFilesTimeoutError("Task changed-file Git inspection timed out") from exc
    except TaskBaselineChangedError as exc:
        raise TaskChangedFilesChangedError(
            "Workspace path changed during Task changed-file calculation"
        ) from exc
    except TaskBaselineError as exc:
        raise TaskChangedFilesError("Task changed-file Git inspection failed") from exc


def _inspect_runtime_identity(
    workspace_root: Path,
    *,
    deadline: float,
) -> GitWorkspaceRuntimeIdentity:
    try:
        identity = inspect_git_workspace_runtime_identity(workspace_root, deadline=deadline)
    except GitWorkspaceDeadlineExceededError as exc:
        raise TaskChangedFilesTimeoutError(
            "Task changed-file Git identity inspection timed out"
        ) from exc
    except GitWorkspaceError as exc:
        raise TaskChangedFilesError("Task changed-file Git identity inspection failed") from exc
    _require_deadline(deadline)
    return identity


def _require_registered_identity(
    workspace: WorkspaceRecord,
    identity: GitWorkspaceRuntimeIdentity,
) -> None:
    if (
        identity.layout.workspace_root != workspace.workspace_root
        or identity.layout.git_common_dir != workspace.git_common_dir
    ):
        raise TaskChangedFilesError(
            "registered Workspace Git identity changed before Task changed-file calculation"
        )


def _committed_changed_paths(
    workspace_root: Path,
    baseline_head: str | None,
    current_head: str | None,
    *,
    deadline: float,
) -> frozenset[str]:
    _require_deadline(deadline)
    if baseline_head == current_head:
        return frozenset()
    if baseline_head is None:
        if current_head is None:
            return frozenset()
        raw = _run_git(
            workspace_root,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            current_head,
            deadline=deadline,
        )
    elif current_head is None:
        raw = _run_git(
            workspace_root,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            baseline_head,
            deadline=deadline,
        )
    else:
        raw = _run_git(
            workspace_root,
            "diff",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=none",
            "-z",
            baseline_head,
            current_head,
            "--",
            deadline=deadline,
        )
    return frozenset(_decode_path_tokens(raw, deadline=deadline))


def _run_git(
    workspace_root: Path,
    *arguments: str,
    deadline: float,
) -> bytes:
    remaining = _remaining_seconds(deadline)
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskChangedFilesTimeoutError("Task changed-file Git comparison timed out") from exc
    except FileNotFoundError as exc:
        raise TaskChangedFilesError(
            "Git executable is not available for Task changed-file calculation"
        ) from exc
    except OSError as exc:
        raise TaskChangedFilesError(
            f"Git could not calculate Task changed files at {workspace_root}"
        ) from exc
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        message = f"Git Task changed-file comparison failed at {workspace_root}"
        if detail:
            message = f"{message}: {detail}"
        raise TaskChangedFilesError(message)
    _require_deadline(deadline)
    return result.stdout


def _decode_path_tokens(raw: bytes, *, deadline: float) -> tuple[str, ...]:
    decoded: list[str] = []
    seen: set[str] = set()
    for raw_path in raw.split(b"\0"):
        _require_deadline(deadline)
        if not raw_path:
            continue
        value = os.fsdecode(raw_path)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TaskChangedFilesError("Git changed path cannot be represented as UTF-8") from exc
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise TaskChangedFilesError(f"unsafe Task changed path: {value!r}")
        if value in seen:
            continue
        seen.add(value)
        decoded.append(value)
    _require_deadline(deadline)
    return tuple(decoded)


def _merge_changed_paths(
    baseline_dirty_paths: tuple[TaskBaselineDirtyPath, ...],
    current_git: TaskGitState,
    committed_paths: frozenset[str],
    *,
    deadline: float,
) -> tuple[str, ...]:
    baseline_by_path = {item.relative_path: item for item in baseline_dirty_paths}
    current_by_path = {item.relative_path: item for item in current_git.dirty_paths}
    changed = set(committed_paths)

    for relative_path in sorted(set(baseline_by_path) | set(current_by_path)):
        _require_deadline(deadline)
        baseline = baseline_by_path.get(relative_path)
        current = current_by_path.get(relative_path)
        if baseline is None:
            assert current is not None
            _add_dirty_record_paths(changed, current)
            continue
        if current is None:
            _add_dirty_record_paths(changed, baseline)
            continue
        unchanged_preexisting = (
            relative_path not in committed_paths
            and baseline.fingerprint_kind is not TaskBaselineFingerprintKind.OPAQUE
            and current.fingerprint_kind is not TaskBaselineFingerprintKind.OPAQUE
            and current == baseline
        )
        if unchanged_preexisting:
            continue
        _add_dirty_record_paths(changed, baseline)
        _add_dirty_record_paths(changed, current)

    _require_deadline(deadline)
    return tuple(sorted(changed))


def _add_dirty_record_paths(changed: set[str], dirty_path: TaskBaselineDirtyPath) -> None:
    changed.add(dirty_path.relative_path)
    if dirty_path.original_relative_path is not None:
        changed.add(dirty_path.original_relative_path)


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TaskChangedFilesTimeoutError("Task changed-file calculation deadline exceeded")
    return remaining


def _require_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise TaskChangedFilesTimeoutError("Task changed-file calculation deadline exceeded")
