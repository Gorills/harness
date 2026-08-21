from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from harness.git_workspace import GitWorkspaceLayout, inspect_git_workspace


class RegistryError(RuntimeError):
    """Base class for Project/Workspace registry failures."""


class ProjectNotFoundError(RegistryError):
    """Raised when a Workspace is registered against an unknown Project."""


class WorkspaceNotFoundError(RegistryError):
    """Raised when a requested Workspace is not registered."""


class WorkspaceRegistrationConflictError(RegistryError):
    """Raised when an existing Workspace root has incompatible registry identity."""


class VisibilityModeConflictError(RegistryError):
    """Raised when one Git common directory would receive contradictory visibility policy."""


class VisibilityMode(StrEnum):
    """Durable Project publication policy values supported by Harness v1."""

    NORMAL = "normal"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """Durable Project identity needed by early registry consumers."""

    project_id: str
    visibility_mode: VisibilityMode


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """Durable physical Workspace identity derived from Git and explicit Project membership."""

    workspace_id: str
    project_id: str
    workspace_root: Path
    git_common_dir: Path


def create_project(connection: sqlite3.Connection) -> ProjectRecord:
    """Create a Project with the required v1 default Normal visibility policy."""
    project = ProjectRecord(project_id=uuid4().hex, visibility_mode=VisibilityMode.NORMAL)
    connection.execute(
        "INSERT INTO projects(id, visibility_mode) VALUES (?, ?)",
        (project.project_id, project.visibility_mode.value),
    )
    return project


def get_project(connection: sqlite3.Connection, project_id: str) -> ProjectRecord:
    """Load one Project by durable identity."""
    row = connection.execute(
        "SELECT id, visibility_mode FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise ProjectNotFoundError(f"project is not registered: {project_id}")
    return _project_from_row(row)


def get_workspace(connection: sqlite3.Connection, workspace_id: str) -> WorkspaceRecord:
    """Load one Workspace by durable identity."""
    row = connection.execute(
        """
        SELECT id, project_id, workspace_root, git_common_dir
        FROM workspaces
        WHERE id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise WorkspaceNotFoundError(f"workspace is not registered: {workspace_id}")
    return _workspace_from_row(row)


def register_workspace(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    path: Path,
) -> WorkspaceRecord:
    """Register one canonical Git worktree under an explicit Project identity."""
    layout = inspect_git_workspace(path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        project = get_project(connection, project_id)
        existing = _workspace_by_root(connection, layout.workspace_root)
        if existing is not None:
            _require_matching_workspace(existing, project_id=project_id, layout=layout)

        _require_common_dir_visibility(
            connection,
            git_common_dir=layout.git_common_dir,
            visibility_mode=project.visibility_mode,
        )
        if existing is not None:
            connection.execute("COMMIT")
            return existing

        workspace = WorkspaceRecord(
            workspace_id=uuid4().hex,
            project_id=project_id,
            workspace_root=layout.workspace_root,
            git_common_dir=layout.git_common_dir,
        )
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES (?, ?, ?, ?)
            """,
            (
                workspace.workspace_id,
                workspace.project_id,
                str(workspace.workspace_root),
                str(workspace.git_common_dir),
            ),
        )
        connection.execute("COMMIT")
        return workspace
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def list_workspaces(
    connection: sqlite3.Connection,
    *,
    project_id: str | None = None,
) -> tuple[WorkspaceRecord, ...]:
    """Return registered Workspaces in stable identity order."""
    if project_id is None:
        rows = connection.execute(
            "SELECT id, project_id, workspace_root, git_common_dir FROM workspaces ORDER BY id"
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, project_id, workspace_root, git_common_dir
            FROM workspaces
            WHERE project_id = ?
            ORDER BY id
            """,
            (project_id,),
        ).fetchall()
    return tuple(_workspace_from_row(row) for row in rows)


def _workspace_by_root(
    connection: sqlite3.Connection,
    workspace_root: Path,
) -> WorkspaceRecord | None:
    row = connection.execute(
        """
        SELECT id, project_id, workspace_root, git_common_dir
        FROM workspaces
        WHERE workspace_root = ?
        """,
        (str(workspace_root),),
    ).fetchone()
    if row is None:
        return None
    return _workspace_from_row(row)


def _require_matching_workspace(
    existing: WorkspaceRecord,
    *,
    project_id: str,
    layout: GitWorkspaceLayout,
) -> None:
    if existing.project_id != project_id:
        raise WorkspaceRegistrationConflictError(
            f"workspace root is already registered to project {existing.project_id}: "
            f"{layout.workspace_root}"
        )
    if existing.git_common_dir != layout.git_common_dir:
        raise WorkspaceRegistrationConflictError(
            "workspace root Git common directory changed since registration: "
            f"{layout.workspace_root}"
        )


def _require_common_dir_visibility(
    connection: sqlite3.Connection,
    *,
    git_common_dir: Path,
    visibility_mode: VisibilityMode,
) -> None:
    rows = connection.execute(
        """
        SELECT DISTINCT projects.visibility_mode
        FROM workspaces
        JOIN projects ON projects.id = workspaces.project_id
        WHERE workspaces.git_common_dir = ?
        """,
        (str(git_common_dir),),
    ).fetchall()
    for row in rows:
        persisted_mode = row[0]
        if not isinstance(persisted_mode, str):
            raise RegistryError("project registry row has invalid persisted types")
        try:
            existing_mode = VisibilityMode(persisted_mode)
        except ValueError as exc:
            raise RegistryError(
                f"project has unsupported visibility mode: {persisted_mode!r}"
            ) from exc
        if existing_mode is not visibility_mode:
            raise VisibilityModeConflictError(
                "workspaces sharing one Git common directory must share visibility mode"
            )


def _project_from_row(row: tuple[object, ...]) -> ProjectRecord:
    project_id, visibility_mode = row
    if not isinstance(project_id, str) or not isinstance(visibility_mode, str):
        raise RegistryError("project registry row has invalid persisted types")
    try:
        mode = VisibilityMode(visibility_mode)
    except ValueError as exc:
        raise RegistryError(
            f"project has unsupported visibility mode: {visibility_mode!r}"
        ) from exc
    return ProjectRecord(project_id=project_id, visibility_mode=mode)


def _workspace_from_row(row: tuple[object, ...]) -> WorkspaceRecord:
    workspace_id, project_id, workspace_root, git_common_dir = row
    if (
        not isinstance(workspace_id, str)
        or not isinstance(project_id, str)
        or not isinstance(workspace_root, str)
        or not isinstance(git_common_dir, str)
    ):
        raise RegistryError("workspace registry row has invalid persisted types")
    return WorkspaceRecord(
        workspace_id=workspace_id,
        project_id=project_id,
        workspace_root=Path(workspace_root),
        git_common_dir=Path(git_common_dir),
    )
