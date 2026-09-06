from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.builtin_skills import sync_builtin_skills
from harness.git_workspace import inspect_workspace_layout, layout_has_git
from harness.hidden_projection import HiddenProjectionError
from harness.index import list_indexed_files, scan_workspace
from harness.registry import (
    VisibilityMode,
    WorkspaceNotFoundError,
    WorkspaceRegistrationConflictError,
    attach_workspace_git_if_present,
    create_project,
    register_workspace,
    register_workspace_for_init,
    register_workspace_for_scan,
    workspace_has_git,
)
from harness.skill_policy import ProjectSkillFacetMode, set_project_skill_facet_mode
from harness.skill_runtime import reconcile_workspace_skills
from harness.skills import (
    ResolvedSkill,
    detect_workspace_stack,
    load_skill_registry,
    resolve_workspace_skills,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.visibility import set_project_visibility


def _ids(resolved: tuple[ResolvedSkill, ...]) -> tuple[str, ...]:
    return tuple(item.definition.skill_id for item in resolved)


def test_inspect_workspace_layout_binds_a_directory_without_git(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir()
    layout = inspect_workspace_layout(root)
    assert layout.workspace_root == root.resolve()
    assert layout.git_common_dir == layout.workspace_root
    assert layout_has_git(layout) is False


def test_init_registers_filesystem_workspace_and_indexes_files(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir()
    (root / "readme.txt").write_text("hello\n", encoding="utf-8")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        registration = register_workspace_for_init(connection, path=root)
        assert registration.project_created is True
        assert registration.workspace_created is True
        assert workspace_has_git(registration.workspace) is False
        scan_workspace(connection, registration.workspace.workspace_id)
        records = list_indexed_files(connection, registration.workspace.workspace_id)
        assert {record.relative_path for record in records} == {"readme.txt"}
        stack = detect_workspace_stack(connection, registration.workspace.workspace_id)
        assert stack.facets == frozenset({"software-project"})
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(WorkspaceNotFoundError, match="harness init"):
            register_workspace_for_scan(connection, path=other)
    finally:
        connection.close()


def test_init_is_idempotent_and_rejects_overlapping_roots(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    nested = root / "nested"
    nested.mkdir(parents=True)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        first = register_workspace_for_init(connection, path=root)
        second = register_workspace_for_init(connection, path=root)
        assert second.workspace.workspace_id == first.workspace.workspace_id
        assert second.project_created is False
        with pytest.raises(WorkspaceRegistrationConflictError, match="overlaps"):
            register_workspace_for_init(connection, path=nested)
    finally:
        connection.close()


def test_git_init_attaches_identity_without_new_workspace(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        assert workspace_has_git(workspace) is False
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        attached = attach_workspace_git_if_present(connection, workspace.workspace_id)
        assert attached.workspace_id == workspace.workspace_id
        assert workspace_has_git(attached) is True
        assert attached.git_common_dir == (root / ".git").resolve()
    finally:
        connection.close()


def test_included_godot_surface_projects_before_files_exist(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    try:
        registration = register_workspace_for_init(connection, path=root)
        scan_workspace(connection, registration.workspace.workspace_id)
        before = resolve_workspace_skills(
            connection, registration.workspace.workspace_id, load_skill_registry(registry)
        )
        assert "godot-development" not in _ids(before)
        set_project_skill_facet_mode(
            connection,
            registration.project.project_id,
            "godot-project",
            ProjectSkillFacetMode.INCLUDED,
        )
        after = resolve_workspace_skills(
            connection, registration.workspace.workspace_id, load_skill_registry(registry)
        )
        assert "godot-development" in _ids(after)
        assert "testing-strategy" in _ids(after)
        projected = reconcile_workspace_skills(
            connection,
            registration.workspace.workspace_id,
            ("cursor",),
            registry_root=registry,
        )
        assert "godot-development" in projected.selected_skill_ids
        assert (root / ".agents" / "skills" / "godot-development" / "SKILL.md").is_file()
        assert (root / ".agents" / "skills" / "testing-strategy" / "SKILL.md").is_file()
    finally:
        connection.close()


def test_hidden_is_refused_for_filesystem_workspace(tmp_path: Path) -> None:
    root = tmp_path / "trial"
    root.mkdir()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        registration = register_workspace_for_init(connection, path=root)
        with pytest.raises(HiddenProjectionError, match="Git Workspace"):
            set_project_visibility(
                connection,
                mode=VisibilityMode.HIDDEN,
                host_profiles=("cursor",),
                project_id=registration.project.project_id,
            )
    finally:
        connection.close()


def test_schema_21_persists_skill_inclusions(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    status = initialize_database(database)
    assert status.schema_version == SCHEMA_VERSION == 21
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
            ("project_skill_inclusions",),
        ).fetchone() == ("project_skill_inclusions",)
    finally:
        connection.close()
