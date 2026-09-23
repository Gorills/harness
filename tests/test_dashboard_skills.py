from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.dashboard_skills as dashboard_skills
from harness.builtin_skills import sync_builtin_skills
from harness.dashboard_skills import (
    DashboardModePreview,
    DashboardSkillsSnapshot,
    read_dashboard_skills,
)
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.skill_policy import ProjectSkillFacetMode, set_project_skill_facet_mode
from harness.skill_runtime import reconcile_workspace_skills
from harness.storage import connect_database, initialize_database


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _workspace(tmp_path: Path) -> tuple[sqlite3.Connection, Path, str, str, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "README.md")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    registry = tmp_path / "registry"
    sync_builtin_skills(registry)
    return connection, database, project.project_id, workspace.workspace_id, registry


def _preview(
    snapshot: DashboardSkillsSnapshot, facet: str, mode: ProjectSkillFacetMode
) -> DashboardModePreview:
    row = snapshot.workspaces[0]
    return next(
        option
        for item in row.facets
        if item.facet == facet
        for option in item.options
        if option.mode is mode
    )


def test_read_only_preview_and_current_projection(tmp_path: Path) -> None:
    connection, database, project_id, workspace_id, registry = _workspace(tmp_path)
    try:
        before = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex", "cursor"),
        )
        row = before.workspaces[0]
        assert row.projection_status == "pending"
        assert row.matching == 0
        assert row.missing_or_changed == 6
        assert len(row.selected) == 6
        assert (
            "server-application"
            in _preview(before, "backend-service", ProjectSkillFacetMode.INCLUDED).add
        )
        assert (
            "frontend-design"
            in _preview(before, "web-frontend", ProjectSkillFacetMode.INCLUDED).add
        )
        assert (
            "public-frontend"
            in _preview(before, "web-frontend", ProjectSkillFacetMode.INCLUDED).add
        )
        assert (
            connection.execute("SELECT count(*) FROM project_skill_inclusions").fetchone()[0] == 0
        )
        assert not (tmp_path / "repo" / ".agents").exists()

        reconcile_workspace_skills(
            connection, workspace_id, ("codex", "cursor"), registry_root=registry
        )
        current = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex", "cursor"),
        )
        assert current.workspaces[0].projection_status == "current"
        assert current.workspaces[0].matching == 6
        (tmp_path / "repo" / ".agents" / "skills" / "testing-strategy" / "SKILL.md").write_text(
            "changed\n", encoding="utf-8"
        )
        changed = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex", "cursor"),
        )
        assert changed.workspaces[0].projection_status == "pending"
        assert changed.workspaces[0].missing_or_changed == 1
    finally:
        connection.close()


def test_collision_is_reported_with_target_path(tmp_path: Path) -> None:
    connection, database, project_id, _, registry = _workspace(tmp_path)
    try:
        target = tmp_path / "repo" / ".agents" / "skills" / "testing-strategy"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("user content\n", encoding="utf-8")
        result = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex",),
        )
        assert result.workspaces[0].projection_status == "conflict"
        assert ".agents/skills/testing-strategy" in result.workspaces[0].detail
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "user content\n"
    finally:
        connection.close()


def test_no_host_and_source_overlay_do_not_claim_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, database, project_id, _, registry = _workspace(tmp_path)
    try:
        no_host = read_dashboard_skills(
            connection, project_id, database_path=database, registry_root=registry, profiles=()
        )
        assert no_host.workspaces[0].projection_status == "no_host"
        monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
        overlay_config = tmp_path / "repo" / ".cursor" / "mcp.json"
        overlay_config.parent.mkdir()
        overlay_config.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(dashboard_skills, "find_isolated_development_root", lambda path: path)
        overlay = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex",),
        )
        assert overlay.workspaces[0].projection_status == "source_overlay"
        assert overlay.workspaces[0].matching == 0
    finally:
        connection.close()


def test_preview_preserves_shared_skill_with_other_included_facet(tmp_path: Path) -> None:
    connection, database, project_id, _, registry = _workspace(tmp_path)
    try:
        set_project_skill_facet_mode(
            connection, project_id, "web-frontend", ProjectSkillFacetMode.INCLUDED
        )
        set_project_skill_facet_mode(
            connection, project_id, "mobile-app", ProjectSkillFacetMode.INCLUDED
        )
        result = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex",),
        )
        excluded = _preview(result, "web-frontend", ProjectSkillFacetMode.EXCLUDED)
        assert "public-frontend" in excluded.remove
        assert "frontend-design" in excluded.keep
        assert "frontend-design" not in excluded.remove
        assert len({item.skill_id for item in result.workspaces[0].selected}) == len(
            result.workspaces[0].selected
        )
    finally:
        connection.close()


def test_empty_registry_is_not_reported_as_delivered(tmp_path: Path) -> None:
    connection, database, project_id, _, _ = _workspace(tmp_path)
    try:
        result = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=tmp_path / "missing",
            profiles=("codex",),
        )
        assert result.workspaces == ()
        assert "empty" in result.error
    finally:
        connection.close()


def test_multi_workspace_preview_uses_each_index_and_reports_independent_state(
    tmp_path: Path,
) -> None:
    connection, database, project_id, first_id, registry = _workspace(tmp_path)
    second_root = tmp_path / "web"
    second_root.mkdir()
    (second_root / "index.html").write_text("<html></html>\n", encoding="utf-8")
    _git(second_root, "init", "-b", "main")
    _git(second_root, "add", "index.html")
    _git(
        second_root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    try:
        second = register_workspace(connection, project_id=project_id, path=second_root)
        scan_workspace(connection, second.workspace_id)
        collision = tmp_path / "repo" / ".agents" / "skills" / "testing-strategy"
        collision.mkdir(parents=True)
        (collision / "SKILL.md").write_text("user skill\n", encoding="utf-8")
        result = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex",),
        )
        by_id = {row.workspace_id: row for row in result.workspaces}
        assert len(by_id) == 2
        assert by_id[first_id].projection_status == "conflict"
        assert by_id[second.workspace_id].projection_status == "pending"
        assert "frontend-design" not in {item.skill_id for item in by_id[first_id].selected}
        assert "frontend-design" in {item.skill_id for item in by_id[second.workspace_id].selected}
    finally:
        connection.close()


def test_oversized_cursor_config_fails_closed_before_host_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, database, project_id, _, registry = _workspace(tmp_path)
    monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
    config = tmp_path / "repo" / ".cursor" / "mcp.json"
    config.parent.mkdir()
    with config.open("wb") as output:
        output.truncate(1024 * 1024 + 1)
    monkeypatch.setattr(
        dashboard_skills,
        "find_isolated_development_root",
        lambda path: (_ for _ in ()).throw(AssertionError("unbounded overlay read")),
    )
    try:
        result = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex",),
        )
        assert result.workspaces[0].projection_status == "error"
        assert "exceeds dashboard inspection limit" in result.workspaces[0].detail
    finally:
        connection.close()


def test_absent_cursor_overlay_config_skips_host_adapter_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, database, project_id, _, registry = _workspace(tmp_path)
    monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
    monkeypatch.setattr(
        dashboard_skills,
        "find_isolated_development_root",
        lambda path: (_ for _ in ()).throw(AssertionError("unnecessary Git probe")),
    )
    try:
        result = read_dashboard_skills(
            connection,
            project_id,
            database_path=database,
            registry_root=registry,
            profiles=("codex",),
        )
        assert result.workspaces[0].projection_status == "pending"
    finally:
        connection.close()
