from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness.git_workspace import GitWorkspaceError, inspect_git_workspace_runtime_identity
from harness.host_adapters import claude_code_skill_projection_surface
from harness.registry import WorkspaceRecord, get_workspace, list_workspaces
from harness.skills import (
    SkillError,
    SkillProjectionResult,
    SkillProjectionSurface,
    apply_skill_projection,
    default_skill_registry,
    load_skill_registry,
    plan_skill_projection,
    resolve_workspace_skills,
)

_SUPPORTED_PROFILES: Final[frozenset[str]] = frozenset({"claude-code"})


class SkillRuntimeError(RuntimeError):
    """Raised when daemon-owned project skill integration cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceSkillReconcileResult:
    workspace_id: str
    selected_skill_ids: tuple[str, ...]
    projection: SkillProjectionResult


@dataclass(frozen=True, slots=True)
class SkillCleanupResult:
    workspace_count: int
    cleaned_workspace_count: int
    skipped_workspace_count: int
    removed: int
    exclude_changed_count: int


def supported_skill_profiles() -> frozenset[str]:
    """Return host profiles with implemented deterministic project-skill surfaces."""
    return _SUPPORTED_PROFILES


def reconcile_workspace_skills(
    connection: sqlite3.Connection,
    workspace_id: str,
    profiles: Sequence[str],
    *,
    registry_root: Path | None = None,
) -> WorkspaceSkillReconcileResult:
    """Resolve and reconcile relevant skills for one registered live Workspace."""
    workspace = get_workspace(connection, workspace_id)
    _validate_workspace_identity(workspace)
    surfaces = _surfaces_for_profiles(profiles)
    root = default_skill_registry() if registry_root is None else registry_root
    try:
        definitions = load_skill_registry(root)
        resolved = resolve_workspace_skills(connection, workspace.workspace_id, definitions)
        projection = apply_skill_projection(
            plan_skill_projection(workspace.workspace_root, resolved, surfaces)
        )
    except SkillError as exc:
        raise SkillRuntimeError("Workspace skill integration could not be reconciled") from exc
    _validate_workspace_identity(workspace)
    return WorkspaceSkillReconcileResult(
        workspace_id=workspace.workspace_id,
        selected_skill_ids=tuple(item.definition.skill_id for item in resolved),
        projection=projection,
    )


def cleanup_projected_skills(
    connection: sqlite3.Connection,
    profiles: Sequence[str],
) -> SkillCleanupResult:
    """Remove only Harness-owned generated skills for safely identifiable live Workspaces."""
    surfaces = _surfaces_for_profiles(profiles)
    workspaces = list_workspaces(connection)
    cleaned = 0
    skipped = 0
    removed = 0
    exclude_changed_count = 0
    for workspace in workspaces:
        try:
            _validate_workspace_identity(workspace)
            projection = apply_skill_projection(
                plan_skill_projection(workspace.workspace_root, (), surfaces)
            )
        except (GitWorkspaceError, SkillRuntimeError, SkillError):
            skipped += 1
            continue
        cleaned += 1
        removed += projection.removed
        exclude_changed_count += int(projection.exclude_changed)
    return SkillCleanupResult(
        workspace_count=len(workspaces),
        cleaned_workspace_count=cleaned,
        skipped_workspace_count=skipped,
        removed=removed,
        exclude_changed_count=exclude_changed_count,
    )


def _surfaces_for_profiles(profiles: Sequence[str]) -> tuple[SkillProjectionSurface, ...]:
    normalized = tuple(profiles)
    if not normalized or len(set(normalized)) != len(normalized):
        raise SkillRuntimeError("host skill profiles must be non-empty and unique")
    unknown = set(normalized) - _SUPPORTED_PROFILES
    if unknown:
        raise SkillRuntimeError("unsupported host skill profile")
    surfaces = []
    for profile in normalized:
        if profile == "claude-code":
            surfaces.append(claude_code_skill_projection_surface())
    return tuple(surfaces)


def _validate_workspace_identity(workspace: WorkspaceRecord) -> None:
    try:
        identity = inspect_git_workspace_runtime_identity(workspace.workspace_root)
    except GitWorkspaceError:
        raise
    if (
        identity.layout.workspace_root != workspace.workspace_root
        or identity.layout.git_common_dir != workspace.git_common_dir
    ):
        raise SkillRuntimeError("registered Workspace Git identity changed")
