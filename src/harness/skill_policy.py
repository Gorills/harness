from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from harness.registry import get_project

# These are stable, human-manageable development surfaces. `software-project` stays internal/core:
# disabling it would remove the quality baseline rather than one owned part of a stack.
MANAGED_PROJECT_SKILL_FACETS: tuple[str, ...] = (
    "backend-service",
    "web-frontend",
    "mobile-app",
    "database-backed",
    "godot-project",
    "containerized",
    "observability",
    "ci-pipeline",
    "deployment-ops",
)
_MANAGED_PROJECT_SKILL_FACET_SET = frozenset(MANAGED_PROJECT_SKILL_FACETS)


class ProjectSkillPolicyError(RuntimeError):
    """Raised when persisted or requested Project skill policy is invalid."""


class ProjectSkillFacetMode(StrEnum):
    AUTO = "auto"
    INCLUDED = "included"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class ProjectSkillPolicy:
    project_id: str
    included_facets: tuple[str, ...]
    excluded_facets: tuple[str, ...]


def get_project_skill_policy(
    connection: sqlite3.Connection,
    project_id: str,
) -> ProjectSkillPolicy:
    """Load the durable Project skill-surface exclusions in stable display order."""
    get_project(connection, project_id)
    included_rows = connection.execute(
        """
        SELECT facet
        FROM project_skill_inclusions
        WHERE project_id = ?
        ORDER BY facet
        """,
        (project_id,),
    ).fetchall()
    excluded_rows = connection.execute(
        """
        SELECT facet
        FROM project_skill_exclusions
        WHERE project_id = ?
        ORDER BY facet
        """,
        (project_id,),
    ).fetchall()
    included = _facets_from_policy_rows(included_rows)
    excluded = _facets_from_policy_rows(excluded_rows)
    overlap = set(included) & set(excluded)
    if overlap:
        raise ProjectSkillPolicyError("Project skill policy includes and excludes the same facet")
    return ProjectSkillPolicy(
        project_id=project_id,
        included_facets=included,
        excluded_facets=excluded,
    )


def _facets_from_policy_rows(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    facets: list[str] = []
    for row in rows:
        facet = row[0]
        if not isinstance(facet, str) or facet not in _MANAGED_PROJECT_SKILL_FACET_SET:
            raise ProjectSkillPolicyError("Project skill policy contains an unsupported facet")
        facets.append(facet)
    return tuple(facets)


def set_project_skill_facet_mode(
    connection: sqlite3.Connection,
    project_id: str,
    facet: str,
    mode: ProjectSkillFacetMode,
) -> ProjectSkillPolicy:
    """Persist one human Project-scope override; Auto is represented by absence."""
    if facet not in _MANAGED_PROJECT_SKILL_FACET_SET:
        raise ProjectSkillPolicyError("Project skill facet is not user-manageable")
    if not isinstance(mode, ProjectSkillFacetMode):
        raise ProjectSkillPolicyError("Project skill facet mode is unsupported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        get_project(connection, project_id)
        connection.execute(
            "DELETE FROM project_skill_inclusions WHERE project_id = ? AND facet = ?",
            (project_id, facet),
        )
        connection.execute(
            "DELETE FROM project_skill_exclusions WHERE project_id = ? AND facet = ?",
            (project_id, facet),
        )
        if mode is ProjectSkillFacetMode.EXCLUDED:
            connection.execute(
                """
                INSERT INTO project_skill_exclusions(project_id, facet)
                VALUES (?, ?)
                """,
                (project_id, facet),
            )
        elif mode is ProjectSkillFacetMode.INCLUDED:
            connection.execute(
                """
                INSERT INTO project_skill_inclusions(project_id, facet)
                VALUES (?, ?)
                """,
                (project_id, facet),
            )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return get_project_skill_policy(connection, project_id)
