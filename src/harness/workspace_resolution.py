from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class WorkspaceResolutionError(RuntimeError):
    """Base class for workspace resolution failures."""


class WorkspaceNotFoundError(WorkspaceResolutionError):
    """Raised when no registered Workspace matches the available hints."""


class AmbiguousWorkspaceError(WorkspaceResolutionError):
    """Raised when one hint matches multiple equally specific Workspaces."""


@dataclass(frozen=True, slots=True)
class WorkspaceCandidate:
    """Registered Workspace identity and filesystem root used for resolution."""

    workspace_id: str
    root: Path


@dataclass(frozen=True, slots=True)
class WorkspaceHint:
    """Ordered, host-normalized filesystem evidence for the active Workspace."""

    path: Path
    source: str


@dataclass(frozen=True, slots=True)
class WorkspaceResolution:
    """Resolved Workspace plus the evidence that selected it."""

    workspace_id: str
    workspace_root: Path
    hint_source: str
    matched_path: Path


class WorkspaceResolver:
    """Resolve ordered filesystem hints against registered Workspaces."""

    def __init__(self, workspaces: Sequence[WorkspaceCandidate]) -> None:
        self._workspaces = tuple(
            (workspace, self._normalize(workspace.root)) for workspace in workspaces
        )

    def resolve(self, hints: Sequence[WorkspaceHint]) -> WorkspaceResolution:
        """Return the first unambiguous Workspace selected by ordered hints."""
        if not hints:
            raise WorkspaceNotFoundError("no workspace hints were provided")

        normalized_hints: list[tuple[WorkspaceHint, Path]] = []
        for hint in hints:
            normalized_path = self._normalize(hint.path)
            normalized_hints.append((hint, normalized_path))
            matches = [
                (workspace, root)
                for workspace, root in self._workspaces
                if self._contains(root, normalized_path)
            ]
            if not matches:
                continue

            deepest = max(len(root.parts) for _, root in matches)
            winners = [
                (workspace, root) for workspace, root in matches if len(root.parts) == deepest
            ]
            if len(winners) != 1:
                workspace_ids = ", ".join(
                    sorted(workspace.workspace_id for workspace, _ in winners)
                )
                raise AmbiguousWorkspaceError(
                    f"workspace hint {hint.source!r} at {normalized_path} matches multiple "
                    f"registered workspaces: {workspace_ids}"
                )

            workspace, root = winners[0]
            return WorkspaceResolution(
                workspace_id=workspace.workspace_id,
                workspace_root=root,
                hint_source=hint.source,
                matched_path=normalized_path,
            )

        hint_details = ", ".join(
            f"{hint.source}={normalized_path}" for hint, normalized_path in normalized_hints
        )
        raise WorkspaceNotFoundError(f"no registered workspace matches hints: {hint_details}")

    @staticmethod
    def _normalize(path: Path) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceResolutionError(f"workspace path cannot be normalized: {path}") from exc
        return Path(os.path.normcase(str(resolved)))

    @staticmethod
    def _contains(root: Path, path: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True
