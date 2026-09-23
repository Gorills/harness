from __future__ import annotations

import re
import shutil
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import pytest
from test_dashboard_actions import _post, _write_host_profiles
from test_dashboard_navigation_realtime import _database, _git

import harness.dashboard as dashboard
import harness.dashboard_skills as dashboard_skills
import harness.skill_runtime as skill_runtime
from harness.builtin_skills import sync_builtin_skills
from harness.dashboard import DashboardServerManager, _view_fingerprint
from harness.index import scan_workspace
from harness.registry import register_workspace
from harness.skill_policy import get_project_skill_policy
from harness.storage import connect_database


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str, Path]:
    root, database, project_id, _ = _database(tmp_path)
    registry = tmp_path / "registry"
    sync_builtin_skills(registry)
    monkeypatch.setattr(dashboard_skills, "default_skill_registry", lambda: registry)
    monkeypatch.setattr(skill_runtime, "default_skill_registry", lambda: registry)
    monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
    return root, database, project_id, registry


def _read(url: str) -> str:
    with urlopen(url, timeout=3) as response:
        assert response.status == 200
        body: bytes = response.read()
        return body.decode("utf-8")


def _settings_url(base: str, project_id: str) -> str:
    return base + f"projects/{project_id}/settings/"


def test_settings_shows_exact_frontend_preview_and_project_overview_skips_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database, project_id, _ = _setup(tmp_path, monkeypatch)
    _write_host_profiles(database, "codex")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        html = _read(_settings_url(base, project_id))
        assert 'id="skill-preview-web-frontend-included"' in html
        preview = html[html.index('id="skill-preview-web-frontend-included"') :]
        added = preview[
            preview.index("<strong>Добавится</strong>") : preview.index("<strong>Уберётся</strong>")
        ]
        assert added.count("<code>frontend-design</code>") == 1
        assert added.count("<code>public-frontend</code>") == 1
        assert added.count("<li>") == 2
        assert "Останется: 6" in preview
        assert 'name="facet" value="web-frontend"' in preview
        assert 'name="mode" value="included"' in preview
        assert "Применить к папкам: 1" in preview
        with monkeypatch.context() as patch:
            patch.setattr(
                dashboard,
                "read_dashboard_skills",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("overview loaded skills")
                ),
            )
            overview = _read(base + f"projects/{project_id}/")
            assert "Области разработки и настройки проекта" in overview
            assert "Фактическая доставка" not in overview
    finally:
        manager.close()


def test_collision_after_policy_post_is_visible_and_policy_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, project_id, _ = _setup(tmp_path, monkeypatch)
    _write_host_profiles(database, "codex")
    collision = root / ".agents" / "skills" / "frontend-design"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user content\n", encoding="utf-8")
    manager = DashboardServerManager(database)
    try:
        url = _settings_url(manager.get_url(), project_id)
        status, headers, _ = _post(
            url,
            {
                "action": "set_skill_scope",
                "project_id": project_id,
                "facet": "web-frontend",
                "mode": "included",
            },
            origin=manager.get_url().rstrip("/"),
        )
        assert status == 303
        assert headers["Location"] == urlsplit(url).path
        html = _read(url)
        assert 'data-skill-status="conflict"' in html
        assert "Конфликт файлов" in html
        assert ".agents/skills/frontend-design" in html
        assert 'data-skill-status="current"' not in html
        preview = html[html.index('id="skill-preview-web-frontend-excluded"') :]
        removed = preview[
            preview.index("<strong>Уберётся</strong>") : preview.index(
                "<details><summary>Останется:"
            )
        ]
        assert removed.count("<code>frontend-design</code>") == 1
        assert removed.count("<code>public-frontend</code>") == 1
        assert "Останется: 6" in preview
        connection = connect_database(database)
        try:
            assert get_project_skill_policy(connection, project_id).included_facets == (
                "web-frontend",
            )
        finally:
            connection.close()
        assert (collision / "SKILL.md").read_text(encoding="utf-8") == "user content\n"
    finally:
        manager.close()


def test_no_host_and_source_overlay_diagnostics_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, project_id, _ = _setup(tmp_path, monkeypatch)
    manager = DashboardServerManager(database)
    try:
        url = _settings_url(manager.get_url(), project_id)
        no_host = _read(url)
        assert 'data-skill-status="no_host"' in no_host
        assert "Нет подключённого хоста" in no_host
        _write_host_profiles(database, "codex")
        overlay_config = root / ".cursor" / "mcp.json"
        overlay_config.parent.mkdir()
        overlay_config.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(dashboard_skills, "find_isolated_development_root", lambda path: root)
        overlay = _read(url)
        assert 'data-skill-status="source_overlay"' in overlay
        assert "Глобальная установка сохраняет изоляцию" in overlay
        assert 'data-skill-status="current"' not in overlay
    finally:
        manager.close()


def test_custom_description_and_workspace_path_are_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database, project_id, registry = _setup(tmp_path, monkeypatch)
    malicious_root = tmp_path / "repo-<em>injected"
    malicious_root.mkdir()
    (malicious_root / "README.md").write_text("second repo\n", encoding="utf-8")
    _git(malicious_root, "init", "-b", "main")
    _git(malicious_root, "add", "README.md")
    _git(
        malicious_root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    connection = connect_database(database)
    try:
        second = register_workspace(connection, project_id=project_id, path=malicious_root)
        scan_workspace(connection, second.workspace_id)
    finally:
        connection.close()
    skill = registry / "custom-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: custom-skill\ndescription: Use <em>carefully</em> & verify.\n---\n\n# Custom\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text(
        "id: custom-skill\napplies:\n  facets:\n    - software-project\n",
        encoding="utf-8",
    )
    manager = DashboardServerManager(database)
    try:
        html = _read(_settings_url(manager.get_url(), project_id))
        assert "<code>custom-skill</code>" in html
        assert "Use &lt;em&gt;carefully&lt;/em&gt; &amp; verify." in html
        assert "Use <em>carefully</em> & verify." not in html
        assert "<script>" not in html
        assert "repo-&lt;em&gt;injected" in html
        assert "repo-<em>injected" not in html
    finally:
        manager.close()


def test_settings_fingerprint_tracks_projection_repair_without_database_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, project_id, _ = _setup(tmp_path, monkeypatch)
    _write_host_profiles(database, "codex")
    target = root / ".agents" / "skills" / "testing-strategy"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user skill\n", encoding="utf-8")
    manager = DashboardServerManager(database)
    try:
        html = _read(_settings_url(manager.get_url(), project_id))
        assert 'data-skill-status="conflict"' in html
        match = re.search(r'data-events-url="([^"]+)"', html)
        assert match is not None
        event_url = unescape(match.group(1))
        query = parse_qs(urlsplit(event_url).query)
        assert query["view"] == ["project_settings"]
        before = _view_fingerprint(database, "project_settings", project_id, None)
        assert query["snapshot"] == [before]
        shutil.rmtree(target)
        after = _view_fingerprint(database, "project_settings", project_id, None)
        assert after != before
        repaired = _read(_settings_url(manager.get_url(), project_id))
        assert 'data-skill-status="pending"' in repaired
        assert 'data-skill-status="conflict"' not in repaired
    finally:
        manager.close()


def test_isolated_dev_preview_and_post_use_same_default_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, project_id, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(tmp_path))
    monkeypatch.delenv("HARNESS_DEV_SKILL_PROFILES", raising=False)
    manager = DashboardServerManager(database)
    try:
        url = _settings_url(manager.get_url(), project_id)
        before = _read(url)
        assert 'data-skill-status="pending"' in before
        assert "Хосты: codex, cursor" in before
        status, headers, _ = _post(
            url,
            {
                "action": "set_skill_scope",
                "project_id": project_id,
                "facet": "backend-service",
                "mode": "included",
            },
            origin=manager.get_url().rstrip("/"),
        )
        assert status == 303
        assert headers["Location"] == urlsplit(url).path
        after = _read(url)
        assert 'data-skill-status="current"' in after
        assert "Хосты: codex, cursor" in after
        assert "Выбрано скиллов: 7" in after
        assert (root / ".agents" / "skills" / "server-application" / "SKILL.md").is_file()
    finally:
        manager.close()
