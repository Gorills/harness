from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness.cursor_adapter import find_isolated_development_root
from harness.git_workspace import GitWorkspaceError, inspect_git_workspace_runtime_identity
from harness.host_adapters import (
    codex_skill_projection_surface,
    cursor_skill_projection_surface,
)
from harness.host_integration_state import load_host_integration_state_for_database
from harness.registry import WorkspaceRecord, get_workspace, list_workspaces
from harness.skills import (
    SkillDefinition,
    SkillError,
    SkillProjectionInspection,
    SkillProjectionResult,
    SkillProjectionSurface,
    apply_skill_projection,
    default_skill_registry,
    inspect_skill_projection,
    load_skill_registry,
    plan_skill_projection,
    resolve_workspace_skills,
    validate_skill_projection_compatibility,
    validate_skill_projection_surface_combination,
)

_SUPPORTED_PROFILES: Final[frozenset[str]] = frozenset({"codex", "cursor"})
_DEFAULT_DEVELOPMENT_PROFILES: Final[tuple[str, ...]] = ("codex", "cursor")
_DEVELOPMENT_PROFILES_ENV: Final[str] = "HARNESS_DEV_SKILL_PROFILES"


class SkillRuntimeError(RuntimeError):
    """Raised when daemon-owned project skill integration cannot be reconciled safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceSkillReconcileResult:
    workspace_id: str
    selected_skill_ids: tuple[str, ...]
    projection: SkillProjectionResult


@dataclass(frozen=True, slots=True)
class WorkspaceSkillInspectionResult:
    """Read-only expected/current generated-skill state for one Workspace."""

    workspace_id: str
    selected_skill_ids: tuple[str, ...]
    projection: SkillProjectionInspection


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


def active_skill_profiles_for_runtime(
    database_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return the owned host profiles whose project skills must stay reconciled.

    Production reads daemon-adjacent installation intent. Isolated development has
    no production host intent, so it uses one explicit compatible profile set and
    never inspects or mutates user-global host configuration.
    """
    values = os.environ if environment is None else environment
    if values.get("HARNESS_DEV_ROOT"):
        configured = values.get(_DEVELOPMENT_PROFILES_ENV)
        profiles = (
            _DEFAULT_DEVELOPMENT_PROFILES
            if configured is None
            else tuple(item.strip() for item in configured.split(",") if item.strip())
        )
        if not profiles:
            raise SkillRuntimeError(
                f"{_DEVELOPMENT_PROFILES_ENV} must select at least one host profile"
            )
    else:
        profiles = tuple(sorted(load_host_integration_state_for_database(database_path).profiles))
    validate_skill_profile_combination(profiles)
    return profiles


def validate_skill_definitions_for_profiles(
    definitions: Sequence[SkillDefinition], profiles: Sequence[str]
) -> None:
    """Validate canonical skill metadata for the selected supported host profiles."""
    validate_skill_projection_compatibility(definitions, _surfaces_for_profiles(profiles))


def validate_skill_profile_combination(profiles: Sequence[str]) -> None:
    """Reject active hosts that cannot share a duplicate-free project skill layout."""
    if not profiles:
        return
    try:
        validate_skill_projection_surface_combination(_surfaces_for_profiles(profiles))
    except SkillError as exc:
        raise SkillRuntimeError(
            "active host profiles cannot share a duplicate-free skill projection"
        ) from exc


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
    if (
        not os.environ.get("HARNESS_DEV_ROOT")
        and find_isolated_development_root(workspace.workspace_root) == workspace.workspace_root
    ):
        raise SkillRuntimeError(
            "global Harness does not project skills into a source-checkout overlay"
        )
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


def inspect_workspace_skills(
    connection: sqlite3.Connection,
    workspace_id: str,
    profiles: Sequence[str],
    *,
    registry_root: Path | None = None,
    deadline: float | None = None,
) -> WorkspaceSkillInspectionResult:
    """Inspect relevant generated skills for one live registered Workspace without mutation."""
    workspace = get_workspace(connection, workspace_id)
    _validate_workspace_identity(workspace, deadline=deadline)
    surfaces = _surfaces_for_profiles(profiles)
    root = default_skill_registry() if registry_root is None else registry_root
    try:
        definitions = load_skill_registry(root)
        resolved = resolve_workspace_skills(
            connection,
            workspace.workspace_id,
            definitions,
            deadline=deadline,
        )
        projection = inspect_skill_projection(
            plan_skill_projection(workspace.workspace_root, resolved, surfaces),
            deadline=deadline,
        )
    except SkillError as exc:
        raise SkillRuntimeError("Workspace skill integration could not be inspected") from exc
    _validate_workspace_identity(workspace, deadline=deadline)
    return WorkspaceSkillInspectionResult(
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
        if profile == "codex":
            surfaces.append(codex_skill_projection_surface())
        elif profile == "cursor":
            surfaces.append(cursor_skill_projection_surface())
    return tuple(surfaces)


def _validate_workspace_identity(
    workspace: WorkspaceRecord, *, deadline: float | None = None
) -> None:
    try:
        identity = inspect_git_workspace_runtime_identity(
            workspace.workspace_root, deadline=deadline
        )
    except GitWorkspaceError:
        raise
    if (
        identity.layout.workspace_root != workspace.workspace_root
        or identity.layout.git_common_dir != workspace.git_common_dir
    ):
        raise SkillRuntimeError("registered Workspace Git identity changed")
