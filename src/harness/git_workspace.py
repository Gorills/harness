from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
class GitWorkingTreeStatus:
    """Compact live Git state needed by read-only Workspace status surfaces."""

    head: str | None
    branch: str | None
    dirty_file_count: int


def inspect_git_workspace(path: Path) -> GitWorkspaceLayout:
    """Return canonical worktree and shared Git-directory paths for ``path``."""
    invocation_dir = _existing_directory(path)
    workspace_root = _normalize_existing_path(Path(_rev_parse(invocation_dir, "--show-toplevel")))

    common_dir = Path(_rev_parse(invocation_dir, "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = invocation_dir / common_dir

    return GitWorkspaceLayout(
        workspace_root=workspace_root,
        git_common_dir=_normalize_existing_path(common_dir),
    )


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
        )
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
    dirty_file_count = 0
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
            dirty_file_count += 1

    if not head_seen or not branch_seen:
        raise GitWorkspaceError("Git status omitted required branch metadata")
    return GitWorkingTreeStatus(
        head=head,
        branch=branch,
        dirty_file_count=dirty_file_count,
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


def _rev_parse(invocation_dir: Path, argument: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", argument],
            cwd=invocation_dir,
            check=False,
            capture_output=True,
            env=_git_environment(),
        )
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
