from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from harness.index import scan_workspace
from harness.registry import ensure_workspace_registration


@dataclass(frozen=True, slots=True)
class WorkspaceScanResult:
    """Compact public scan result after registration and file-index reconciliation."""

    project_id: str
    workspace_id: str
    visibility_mode: str
    workspace_root: Path
    project_created: bool
    workspace_created: bool
    file_count: int
    added: int
    updated: int
    removed: int


def scan_path(connection: sqlite3.Connection, path: Path) -> WorkspaceScanResult:
    """Ensure durable Workspace identity, then reconcile its deterministic file inventory."""
    registration = ensure_workspace_registration(connection, path=path)
    scan = scan_workspace(connection, registration.workspace.workspace_id)
    return WorkspaceScanResult(
        project_id=registration.project.project_id,
        workspace_id=registration.workspace.workspace_id,
        visibility_mode=registration.project.visibility_mode.value,
        workspace_root=registration.workspace.workspace_root,
        project_created=registration.project_created,
        workspace_created=registration.workspace_created,
        file_count=scan.file_count,
        added=scan.added,
        updated=scan.updated,
        removed=scan.removed,
    )
