from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from harness.hidden_projection import (
    HiddenProjectionError,
    HiddenProjectionResult,
    apply_hidden_projection,
    hidden_worktree_roots,
    remove_hidden_projection,
)
from harness.registry import (
    ProjectRecord,
    RegistryError,
    VisibilityMode,
    WorkspaceRecord,
    get_project,
    list_workspaces,
    register_workspace_for_scan,
    update_project_visibility,
)


@dataclass(frozen=True, slots=True)
class VisibilityChangeResult:
    """Operator-facing outcome of a Hidden/Normal visibility change."""

    project: ProjectRecord
    workspace: WorkspaceRecord
    projection: HiddenProjectionResult


def set_project_visibility(
    connection: sqlite3.Connection,
    *,
    mode: VisibilityMode,
    host_profiles: Sequence[str],
    path: Path | None = None,
    project_id: str | None = None,
    deadline: float | None = None,
) -> VisibilityChangeResult:
    """Apply hygiene-effective Hidden or restore Normal with crash-safe side-effect order."""
    if (path is None) == (project_id is None):
        raise RegistryError("visibility change requires exactly one of path or project_id")
    if path is not None:
        registration = register_workspace_for_scan(connection, path=path)
        project = registration.project
        workspace = registration.workspace
    else:
        assert project_id is not None
        project = get_project(connection, project_id)
        workspaces = list_workspaces(connection, project_id=project_id)
        if not workspaces:
            raise RegistryError(f"project has no registered Workspaces: {project_id}")
        workspace = workspaces[0]
    workspaces = list_workspaces(connection, project_id=project.project_id)
    roots = _projection_roots(workspaces, deadline=deadline)
    profiles = tuple(host_profiles)
    if mode is VisibilityMode.HIDDEN:
        if not profiles:
            raise HiddenProjectionError(
                "no active host profiles; Hidden instructions cannot be projected"
            )
        projection = apply_hidden_projection(roots, profiles, deadline=deadline)
        project = (
            project
            if project.visibility_mode is VisibilityMode.HIDDEN
            else update_project_visibility(connection, project.project_id, VisibilityMode.HIDDEN)
        )
        return VisibilityChangeResult(project=project, workspace=workspace, projection=projection)

    if project.visibility_mode is not VisibilityMode.NORMAL:
        project = update_project_visibility(connection, project.project_id, VisibilityMode.NORMAL)
    projection = remove_hidden_projection(roots, profiles or None, deadline=deadline)
    return VisibilityChangeResult(project=project, workspace=workspace, projection=projection)


def _projection_roots(
    workspaces: Sequence[WorkspaceRecord], *, deadline: float | None
) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for workspace in workspaces:
        if not workspace.workspace_root.is_dir():
            continue
        for root in hidden_worktree_roots(workspace.workspace_root, deadline=deadline):
            if root in seen:
                continue
            seen.add(root)
            roots.append(root)
    if not roots:
        raise HiddenProjectionError("Hidden projection has no live Git worktree root")
    return tuple(roots)
