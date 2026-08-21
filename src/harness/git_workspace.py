from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


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


def _existing_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise NotGitWorkspaceError(f"workspace path does not exist or cannot be resolved: {path}") from exc
    if not resolved.is_dir():
        raise NotGitWorkspaceError(f"workspace path is not a directory: {resolved}")
    return resolved


def _normalize_existing_path(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitWorkspaceError(f"Git reported a path that cannot be resolved: {path}") from exc
    return Path(os.path.normcase(str(resolved)))


def _rev_parse(invocation_dir: Path, argument: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", argument],
            cwd=invocation_dir,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise GitExecutableUnavailableError("Git executable is not available") from exc
    except OSError as exc:
        raise GitWorkspaceError(f"Git could not inspect workspace at {invocation_dir}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip()
        message = f"path is not inside an inspectable Git worktree: {invocation_dir}"
        if detail:
            message = f"{message}: {detail}"
        raise NotGitWorkspaceError(message)

    output = result.stdout.rstrip("\r\n")
    if not output:
        raise GitWorkspaceError(f"Git returned no value for {argument} at {invocation_dir}")
    return output
