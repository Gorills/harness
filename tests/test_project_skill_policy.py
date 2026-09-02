from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.index import scan_workspace
from harness.registry import create_project, delete_project, register_workspace
from harness.skill_policy import (
    MANAGED_PROJECT_SKILL_FACETS,
    ProjectSkillFacetMode,
    get_project_skill_policy,
    set_project_skill_facet_mode,
)
from harness.skill_runtime import reconcile_workspace_skills
from harness.skills import SKILL_METADATA_FILE_NAME, load_skill_registry, resolve_workspace_skills
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir()
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "init", "-b", "main")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )


def _registered_project(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str, str]:
    root = tmp_path / "repo"
    _make_repo(
        root,
        {
            "apps/api/pyproject.toml": '[project]\nname="api"\ndependencies=["fastapi"]\n',
            "apps/site/package.json": json.dumps(
                {"dependencies": {"next": "16", "react": "19", "react-dom": "19"}}
            ),
        },
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, project.project_id, workspace.workspace_id


def _write_skill(
    registry: Path,
    skill_id: str,
    facet: str,
    *,
    dependencies: tuple[str, ...] = (),
) -> None:
    directory = registry / skill_id
    directory.mkdir(parents=True)
    registry.chmod(0o700)
    (directory / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Use when {facet} work is in scope.\n---\n\n# {skill_id}\n",
        encoding="utf-8",
    )
    lines = [f"id: {skill_id}", "applies:", "  facets:", f"    - {facet}"]
    if dependencies:
        lines.append("  dependencies:")
        lines.extend(f"    - {dependency}" for dependency in dependencies)
    (directory / SKILL_METADATA_FILE_NAME).write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def test_schema_v16_project_skill_policy_is_bounded_and_cascades(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    status = initialize_database(database)
    assert status.schema_version == SCHEMA_VERSION == 17
    connection = connect_database(database)
    try:
        project = create_project(connection)
        assert get_project_skill_policy(connection, project.project_id).excluded_facets == ()
        for facet in MANAGED_PROJECT_SKILL_FACETS:
            set_project_skill_facet_mode(
                connection,
                project.project_id,
                facet,
                ProjectSkillFacetMode.EXCLUDED,
            )
        assert set(get_project_skill_policy(connection, project.project_id).excluded_facets) == set(
            MANAGED_PROJECT_SKILL_FACETS
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO project_skill_exclusions(project_id, facet) VALUES (?, ?)",
                (project.project_id, "software-project"),
            )
        delete_project(connection, project.project_id)
        assert connection.execute("SELECT COUNT(*) FROM project_skill_exclusions").fetchone() == (
            0,
        )
    finally:
        connection.close()


def test_frontend_exclusion_survives_new_matching_skill(tmp_path: Path) -> None:
    root, connection, project_id, workspace_id = _registered_project(tmp_path)
    registry = tmp_path / "skills"
    _write_skill(registry, "backend", "backend-service")
    _write_skill(registry, "frontend", "web-frontend")
    try:
        before = resolve_workspace_skills(connection, workspace_id, load_skill_registry(registry))
        assert {item.definition.skill_id for item in before} == {"backend", "frontend"}
        first = reconcile_workspace_skills(
            connection,
            workspace_id,
            ("codex",),
            registry_root=registry,
        )
        assert set(first.selected_skill_ids) == {"backend", "frontend"}
        assert (root / ".agents" / "skills" / "frontend" / "SKILL.md").is_file()

        set_project_skill_facet_mode(
            connection,
            project_id,
            "web-frontend",
            ProjectSkillFacetMode.EXCLUDED,
        )
        filtered = reconcile_workspace_skills(
            connection,
            workspace_id,
            ("codex",),
            registry_root=registry,
        )
        assert filtered.selected_skill_ids == ("backend",)
        assert not (root / ".agents" / "skills" / "frontend").exists()

        _write_skill(registry, "future-frontend", "web-frontend")
        after_pack_add = reconcile_workspace_skills(
            connection,
            workspace_id,
            ("codex",),
            registry_root=registry,
        )
        assert after_pack_add.selected_skill_ids == ("backend",)
        assert not (root / ".agents" / "skills" / "future-frontend").exists()

        set_project_skill_facet_mode(
            connection,
            project_id,
            "web-frontend",
            ProjectSkillFacetMode.AUTO,
        )
        restored = reconcile_workspace_skills(
            connection,
            workspace_id,
            ("codex",),
            registry_root=registry,
        )
        assert set(restored.selected_skill_ids) == {"backend", "frontend", "future-frontend"}
    finally:
        connection.close()


def test_excluded_surface_cannot_reenter_through_dependency_match(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(
        root,
        {"pyproject.toml": ('[project]\nname="service"\ndependencies=["sqlalchemy"]\n')},
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    registry = tmp_path / "skills"
    _write_skill(
        registry,
        "database-rules",
        "database-backed",
        dependencies=("sqlalchemy",),
    )
    try:
        definitions = load_skill_registry(registry)
        before = resolve_workspace_skills(connection, workspace.workspace_id, definitions)
        assert tuple(item.definition.skill_id for item in before) == ("database-rules",)
        set_project_skill_facet_mode(
            connection,
            project.project_id,
            "database-backed",
            ProjectSkillFacetMode.EXCLUDED,
        )
        after = resolve_workspace_skills(connection, workspace.workspace_id, definitions)
        assert after == ()
    finally:
        connection.close()
