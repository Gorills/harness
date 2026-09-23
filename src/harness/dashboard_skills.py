"""Read-only skill selection and delivery facts for Project settings."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.cursor_adapter import find_isolated_development_root
from harness.git_workspace import inspect_workspace_runtime_identity
from harness.host_adapters import codex_skill_projection_surface, cursor_skill_projection_surface
from harness.registry import get_project, list_workspaces, workspace_layout_compatible
from harness.skill_policy import (
    MANAGED_PROJECT_SKILL_FACETS,
    ProjectSkillFacetMode,
    get_project_skill_policy,
)
from harness.skill_runtime import active_skill_profiles_for_runtime
from harness.skills import (
    SkillDefinition,
    SkillProjectionCollisionError,
    default_skill_registry,
    detect_workspace_stack,
    inspect_skill_projection,
    load_skill_registry,
    plan_skill_projection,
    resolve_skills,
)

_MAX_CURSOR_CONFIG_BYTES = 1024 * 1024

ProjectionStatus = Literal["current", "pending", "conflict", "source_overlay", "no_host", "error"]


@dataclass(frozen=True, slots=True)
class DashboardSkillCatalogItem:
    skill_id: str
    description: str
    facets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardSelectedSkill:
    skill_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardModePreview:
    mode: ProjectSkillFacetMode
    add: tuple[str, ...]
    remove: tuple[str, ...]
    keep: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DashboardFacetPreview:
    facet: str
    current_mode: ProjectSkillFacetMode
    options: tuple[DashboardModePreview, ...]


@dataclass(frozen=True, slots=True)
class DashboardSkillWorkspace:
    workspace_id: str
    workspace_root: Path
    detected_facets: tuple[str, ...]
    selected: tuple[DashboardSelectedSkill, ...]
    profiles: tuple[str, ...]
    projection_status: ProjectionStatus
    matching: int
    missing_or_changed: int
    stale_owned: int
    detail: str
    facets: tuple[DashboardFacetPreview, ...]


@dataclass(frozen=True, slots=True)
class DashboardSkillsSnapshot:
    catalog: tuple[DashboardSkillCatalogItem, ...]
    workspaces: tuple[DashboardSkillWorkspace, ...]
    error: str = ""


def _bounded_detail(error: Exception) -> str:
    """Keep operator errors useful without allowing a whole file body into the page."""
    return str(error).replace("\n", " ")[:320]


def _mode_for_facet(facet: str, included: set[str], excluded: set[str]) -> ProjectSkillFacetMode:
    if facet in included:
        return ProjectSkillFacetMode.INCLUDED
    if facet in excluded:
        return ProjectSkillFacetMode.EXCLUDED
    return ProjectSkillFacetMode.AUTO


def _is_source_overlay(workspace_root: Path, deadline: float) -> bool:
    if os.environ.get("HARNESS_DEV_ROOT"):
        return False
    if time.monotonic() >= deadline:
        raise TimeoutError("Skill preview timed out")
    config = workspace_root / ".cursor" / "mcp.json"
    try:
        metadata = config.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISREG(metadata.st_mode) and metadata.st_size > _MAX_CURSOR_CONFIG_BYTES:
        raise RuntimeError("Cursor MCP config exceeds dashboard inspection limit")
    # The host adapter remains authoritative for overlay identity. Its Git call
    # has a separate five-second timeout; the dashboard budget is cooperative.
    return find_isolated_development_root(workspace_root) == workspace_root


def _catalog(definitions: Sequence[SkillDefinition]) -> tuple[DashboardSkillCatalogItem, ...]:
    return tuple(
        DashboardSkillCatalogItem(
            definition.skill_id,
            dict(definition.frontmatter_text_fields).get("description", ""),
            definition.applies.facets,
        )
        for definition in definitions
    )


def read_dashboard_skills(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    database_path: Path,
    registry_root: Path | None = None,
    profiles: Sequence[str] | None = None,
) -> DashboardSkillsSnapshot:
    """Read exact resolver previews and current native projection state without mutation.

    The caller owns the connection. One registry read and one indexed stack read are
    shared across every mode preview for a Workspace. A shared three-second
    cooperative deadline covers indexed stack and projection inspection; it is
    not a total wall-clock limit. Registry loading hashes portable files without
    a deadline. The overlay adapter has a separate five-second Git timeout and
    reads its config after a one-MiB size preflight. Once the deadline expires,
    remaining Workspaces do not start another indexed/projection read.
    """
    deadline = time.monotonic() + 3.0
    try:
        get_project(connection, project_id)
        policy = get_project_skill_policy(connection, project_id)
        workspaces = list_workspaces(connection, project_id=project_id)
    except (sqlite3.Error, RuntimeError) as exc:
        return DashboardSkillsSnapshot(
            (), (), f"Project skill settings unavailable: {_bounded_detail(exc)}"
        )

    try:
        definitions = load_skill_registry(
            default_skill_registry() if registry_root is None else registry_root
        )
    except (OSError, RuntimeError) as exc:
        return DashboardSkillsSnapshot(
            (), (), f"Skill registry unavailable: {_bounded_detail(exc)}"
        )
    catalog = _catalog(definitions)
    if not definitions:
        return DashboardSkillsSnapshot(
            catalog, (), "Skill registry is empty; no skills can be delivered"
        )

    profile_error = ""
    try:
        active_profiles = tuple(
            active_skill_profiles_for_runtime(database_path) if profiles is None else profiles
        )
        if len(set(active_profiles)) != len(active_profiles) or set(active_profiles) - {
            "codex",
            "cursor",
        }:
            raise ValueError("unsupported or duplicate host profile")
    except (OSError, RuntimeError, ValueError) as exc:
        active_profiles = ()
        profile_error = f"Host profiles unavailable: {_bounded_detail(exc)}"
    surfaces = tuple(
        codex_skill_projection_surface()
        if profile == "codex"
        else cursor_skill_projection_surface()
        for profile in active_profiles
    )
    included = set(policy.included_facets)
    excluded = set(policy.excluded_facets)
    result: list[DashboardSkillWorkspace] = []
    for workspace in workspaces:
        selected: tuple[DashboardSelectedSkill, ...] = ()
        facet_previews: tuple[DashboardFacetPreview, ...] = ()
        detected_facets: tuple[str, ...] = ()
        status: ProjectionStatus = "error"
        detail = ""
        matching = missing = stale = 0
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("Skill preview timed out")
            stack = detect_workspace_stack(connection, workspace.workspace_id, deadline=deadline)
            detected_facets = tuple(sorted(stack.facets))
            current = resolve_skills(
                definitions,
                stack,
                included_facets=policy.included_facets,
                excluded_facets=policy.excluded_facets,
            )
            selected = tuple(
                DashboardSelectedSkill(item.definition.skill_id, item.match_reasons)
                for item in current
            )
            current_ids = {item.skill_id for item in selected}
            preview_rows: list[DashboardFacetPreview] = []
            for facet in MANAGED_PROJECT_SKILL_FACETS:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Skill preview timed out")
                options: list[DashboardModePreview] = []
                for mode in ProjectSkillFacetMode:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Skill preview timed out")
                    next_included = (included - {facet}) | (
                        {facet} if mode is ProjectSkillFacetMode.INCLUDED else set()
                    )
                    next_excluded = (excluded - {facet}) | (
                        {facet} if mode is ProjectSkillFacetMode.EXCLUDED else set()
                    )
                    candidate = resolve_skills(
                        definitions,
                        stack,
                        included_facets=next_included,
                        excluded_facets=next_excluded,
                    )
                    candidate_ids = {item.definition.skill_id for item in candidate}
                    options.append(
                        DashboardModePreview(
                            mode,
                            tuple(sorted(candidate_ids - current_ids)),
                            tuple(sorted(current_ids - candidate_ids)),
                            tuple(sorted(current_ids & candidate_ids)),
                        )
                    )
                preview_rows.append(
                    DashboardFacetPreview(
                        facet, _mode_for_facet(facet, included, excluded), tuple(options)
                    )
                )
            facet_previews = tuple(preview_rows)

            if not active_profiles:
                status, detail = "no_host", profile_error or "No active host profile"
            elif _is_source_overlay(workspace.workspace_root, deadline):
                status, detail = (
                    "source_overlay",
                    "Global Harness does not project skills into this source checkout",
                )
            else:
                identity = inspect_workspace_runtime_identity(
                    workspace.workspace_root, deadline=deadline
                )
                if not workspace_layout_compatible(workspace, identity.layout):
                    raise RuntimeError("registered Workspace identity changed")
                projection = inspect_skill_projection(
                    plan_skill_projection(workspace.workspace_root, current, surfaces),
                    deadline=deadline,
                )
                matching = projection.matching
                missing = projection.missing_or_changed
                stale = projection.stale_owned
                status = "current" if projection.is_current else "pending"
                if not projection.exclude_current:
                    detail = "Git-local skill exclusions need reconciliation"
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            status = "conflict" if isinstance(exc, SkillProjectionCollisionError) else "error"
            detail = _bounded_detail(exc)
        result.append(
            DashboardSkillWorkspace(
                workspace.workspace_id,
                workspace.workspace_root,
                detected_facets,
                selected,
                active_profiles,
                status,
                matching,
                missing,
                stale,
                detail,
                facet_previews,
            )
        )
    return DashboardSkillsSnapshot(catalog, tuple(result), profile_error)
