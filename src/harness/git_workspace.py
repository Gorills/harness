from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

_GIT_COMMAND_TIMEOUT_SECONDS = 1.5
_GIT_CONTEXT_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_PREFIX",
)


class GitWorkspaceError(RuntimeError):
    """Base class for Git Workspace inspection failures."""


class GitWorkspaceDeadlineExceededError(GitWorkspaceError):
    """Raised when a caller-supplied Git Workspace inspection deadline expires."""


class GitExecutableUnavailableError(GitWorkspaceError):
    """Raised when the Git executable cannot be started."""


class NotGitWorkspaceError(GitWorkspaceError):
    """Raised when a filesystem path is not inside a Git worktree."""


@dataclass(frozen=True, slots=True)
class GitWorkspaceLayout:
    """Filesystem identity needed to distinguish a worktree from its shared repository."""

    workspace_root: Path
    git_common_dir: Path


@dataclass(frozen=True, slots=True)
class GitWorkspaceRuntimeIdentity:
    """Ephemeral filesystem identity used to detect one-read Workspace replacement races."""

    layout: GitWorkspaceLayout
    git_dir: Path
    workspace_root_identity: tuple[int, int]
    git_dir_identity: tuple[int, int]
    git_common_dir_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class GitWorkingTreeStatus:
    """Compact live Git state needed by read-only Workspace status surfaces."""

    head: str | None
    branch: str | None
    dirty_path_count: int


def inspect_git_workspace(path: Path, *, deadline: float | None = None) -> GitWorkspaceLayout:
    """Return canonical worktree and shared Git-directory paths for ``path``."""
    _require_git_workspace_deadline(deadline)
    invocation_dir = _existing_directory(path)
    workspace_root = _normalize_existing_path(
        Path(_rev_parse(invocation_dir, "--show-toplevel", deadline=deadline))
    )

    common_dir = Path(_rev_parse(invocation_dir, "--git-common-dir", deadline=deadline))
    if not common_dir.is_absolute():
        common_dir = invocation_dir / common_dir

    layout = GitWorkspaceLayout(
        workspace_root=workspace_root,
        git_common_dir=_normalize_existing_path(common_dir),
    )
    _require_git_workspace_deadline(deadline)
    return layout


def inspect_git_workspace_runtime_identity(
    path: Path,
    *,
    deadline: float | None = None,
) -> GitWorkspaceRuntimeIdentity:
    """Return canonical Git paths plus ephemeral inode identity for one live status read."""
    _require_git_workspace_deadline(deadline)
    layout = inspect_git_workspace(path, deadline=deadline)
    git_dir = Path(_rev_parse(layout.workspace_root, "--git-dir", deadline=deadline))
    if not git_dir.is_absolute():
        git_dir = layout.workspace_root / git_dir
    normalized_git_dir = _normalize_existing_path(git_dir)
    identity = GitWorkspaceRuntimeIdentity(
        layout=layout,
        git_dir=normalized_git_dir,
        workspace_root_identity=_filesystem_identity(layout.workspace_root),
        git_dir_identity=_filesystem_identity(normalized_git_dir),
        git_common_dir_identity=_filesystem_identity(layout.git_common_dir),
    )
    _require_git_workspace_deadline(deadline)
    return identity


def inspect_git_working_tree_status(path: Path) -> GitWorkingTreeStatus:
    """Return branch/HEAD and a bounded dirty-path count from stable Git porcelain output."""
    invocation_dir = _existing_directory(path)
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            cwd=invocation_dir,
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitWorkspaceError(f"Git status inspection timed out at {invocation_dir}") from exc
    except FileNotFoundError as exc:
        raise GitExecutableUnavailableError("Git executable is not available") from exc
    except OSError as exc:
        raise GitWorkspaceError(f"Git could not inspect status at {invocation_dir}") from exc

    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        message = f"Git could not inspect working tree status: {invocation_dir}"
        if detail:
            message = f"{message}: {detail}"
        raise NotGitWorkspaceError(message)

    head: str | None = None
    branch: str | None = None
    head_seen = False
    branch_seen = False
    dirty_path_count = 0
    for raw_line in result.stdout.splitlines():
        if raw_line.startswith(b"# branch.oid "):
            if head_seen:
                raise GitWorkspaceError("Git status returned duplicate branch.oid metadata")
            value = os.fsdecode(raw_line[len(b"# branch.oid ") :])
            if not value:
                raise GitWorkspaceError("Git status returned an empty branch.oid value")
            head = None if value == "(initial)" else value
            head_seen = True
            continue
        if raw_line.startswith(b"# branch.head "):
            if branch_seen:
                raise GitWorkspaceError("Git status returned duplicate branch.head metadata")
            value = os.fsdecode(raw_line[len(b"# branch.head ") :])
            if not value:
                raise GitWorkspaceError("Git status returned an empty branch.head value")
            branch = None if value == "(detached)" else value
            branch_seen = True
            continue
        if raw_line.startswith(b"# "):
            continue
        if raw_line:
            dirty_path_count += 1

    if not head_seen or not branch_seen:
        raise GitWorkspaceError("Git status omitted required branch metadata")
    return GitWorkingTreeStatus(
        head=head,
        branch=branch,
        dirty_path_count=dirty_path_count,
    )


def _existing_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NotGitWorkspaceError(
            f"workspace path does not exist or cannot be resolved: {path}"
        ) from exc
    if not resolved.is_dir():
        raise NotGitWorkspaceError(f"workspace path is not a directory: {resolved}")
    return resolved


def _normalize_existing_path(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitWorkspaceError(f"Git reported a path that cannot be resolved: {path}") from exc
    return Path(os.path.normcase(str(resolved)))


def _filesystem_identity(path: Path) -> tuple[int, int]:
    try:
        path_stat = path.stat()
    except OSError as exc:
        raise GitWorkspaceError(f"Git identity path cannot be inspected: {path}") from exc
    return path_stat.st_dev, path_stat.st_ino


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for variable in _GIT_CONTEXT_ENVIRONMENT:
        environment.pop(variable, None)
    return environment


def _decode_git_value(value: bytes) -> str:
    output = os.fsdecode(value)
    if output.endswith("\n"):
        output = output[:-1]
    if output.endswith("\r"):
        output = output[:-1]
    return output


def _rev_parse(
    invocation_dir: Path,
    argument: str,
    *,
    deadline: float | None = None,
) -> str:
    timeout = _git_command_timeout(deadline)
    try:
        result = subprocess.run(
            ["git", "rev-parse", argument],
            cwd=invocation_dir,
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if deadline is not None and monotonic() >= deadline:
            raise GitWorkspaceDeadlineExceededError(
                f"Git workspace inspection deadline exceeded at {invocation_dir}"
            ) from exc
        raise GitWorkspaceError(f"Git workspace inspection timed out at {invocation_dir}") from exc
    except FileNotFoundError as exc:
        raise GitExecutableUnavailableError("Git executable is not available") from exc
    except OSError as exc:
        raise GitWorkspaceError(f"Git could not inspect workspace at {invocation_dir}") from exc

    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        message = f"path is not inside an inspectable Git worktree: {invocation_dir}"
        if detail:
            message = f"{message}: {detail}"
        raise NotGitWorkspaceError(message)

    output = _decode_git_value(result.stdout)
    if not output:
        raise GitWorkspaceError(f"Git returned no value for {argument} at {invocation_dir}")
    return output


def _git_command_timeout(deadline: float | None) -> float:
    if deadline is None:
        return _GIT_COMMAND_TIMEOUT_SECONDS
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise GitWorkspaceDeadlineExceededError("Git workspace inspection deadline exceeded")
    return min(_GIT_COMMAND_TIMEOUT_SECONDS, remaining)


def _require_git_workspace_deadline(deadline: float | None) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise GitWorkspaceDeadlineExceededError("Git workspace inspection deadline exceeded")
