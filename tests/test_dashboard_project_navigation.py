from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

import pytest
from test_dashboard_actions import _post
from test_dashboard_navigation_realtime import _database, _git, _read, _review_roundtrip

from harness.dashboard import DashboardServerManager
from harness.registry import create_project, register_workspace
from harness.storage import connect_database
from harness.task_workflow import task_start


def _tabs(html: str) -> str:
    match = re.search(r'<nav class="project-tabs".*?</nav>', html)
    assert match is not None
    return unescape(match[0])


def test_every_project_screen_keeps_navigation_and_vault_returns_to_exact_task(
    tmp_path: Path,
) -> None:
    root, database, project_id, workspace_id = _database(tmp_path)
    _review_roundtrip(database, workspace_id)
    worktree = tmp_path / "second-worktree"
    _git(root, "worktree", "add", "-b", "feature", str(worktree))
    connection = connect_database(database)
    try:
        second = register_workspace(connection, project_id=project_id, path=worktree)
        task = task_start(connection, second.workspace_id, "Задача во второй папке")
    finally:
        connection.close()
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        project = f"projects/{project_id}/"
        tasks = f"workspaces/{second.workspace_id}/"
        task_path = f"tasks/{task.task_id}/"
        context = urlencode({"workspace": second.workspace_id, "task": task.task_id})
        vault = f"vault/{project_id}/?{context}"
        for path, current in (
            (project, "Обзор"),
            (tasks, "Задачи"),
            (task_path, "Задачи"),
            (project + "settings/", "Настройки"),
            (vault, "Заметки и доступы"),
        ):
            _, _, html = _read(base + path)
            tabs = _tabs(html)
            assert f'href="/{project}"' in tabs
            assert f'href="/{project}settings/"' in tabs
            assert re.search(r'aria-current="(?:page|true)">' + current + r"</a>", tabs)
            assert ">Обзор</a>" in tabs and ">Задачи</a>" in tabs
            assert ">Заметки и доступы</a>" in tabs and ">Настройки</a>" in tabs
            if path in (tasks, task_path, vault):
                assert f'href="/{tasks}"' in tabs  # Never jump to the attention workspace.
            if path == task_path:
                assert f'href="/{vault}"' in tabs
                assert html.index('id="task-actions"') < html.index('id="timeline"')
            if path == tasks:
                assert f'href="/workspaces/{workspace_id}/"' in html
                assert "Состояние задачи" not in html
                assert 'name="visibility_mode"' not in html
            if path == vault:
                assert f'href="/{task_path}">Вернуться к задаче</a>' in tabs
                assert "<script" not in html
                frame = re.search(r'<iframe[^>]+src="([^"]+)"', html)
                assert frame is not None
                assert task.task_id not in frame[1] and second.workspace_id not in frame[1]
        _, _, home = _read(base)
        assert f'href="/{project}"' in home
        assert "Проиндексировано" not in home
    finally:
        manager.close()


def test_vault_navigation_rejects_foreign_ambiguous_or_missing_context(tmp_path: Path) -> None:
    _root, database, project_id, workspace_id = _database(tmp_path)
    task = _review_roundtrip(database, workspace_id)
    connection = connect_database(database)
    try:
        foreign_project = create_project(connection)
    finally:
        connection.close()
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        paths = [
            f"vault/{foreign_project.project_id}/?workspace={workspace_id}&task={task.task_id}",
            f"vault/{project_id}/?workspace=missing&task={task.task_id}",
            f"vault/{project_id}/?workspace={workspace_id}&task=missing",
            f"vault/{project_id}/?task={task.task_id}",
            f"vault/{project_id}/?workspace={workspace_id}&workspace={workspace_id}",
            f"vault/{project_id}/?workspace=",
            f"vault/{project_id}/?return=https://example.invalid",
            f"vault/all/?workspace={workspace_id}",
            "tasks/missing/",
        ]
        for path in paths:
            with pytest.raises(HTTPError) as error:
                urlopen(base + path, timeout=3)
            assert error.value.code == 404
            html = error.value.read().decode()
            assert 'href="/"' in html and 'href="/vault/all/"' in html
            assert "<iframe" not in html
        _, _, empty_project = _read(base + f"projects/{foreign_project.project_id}/")
        assert ">Задачи</a>" not in _tabs(empty_project)
        assert ">Заметки и доступы</a>" in _tabs(empty_project)
        _, _, all_notes = _read(base + "vault/all/")
        assert 'href="/vault/all/" aria-current="page"' in all_notes
        assert 'class="project-tabs"' not in all_notes
    finally:
        manager.close()


def test_project_settings_post_redirect_recovery_and_legacy_route(tmp_path: Path) -> None:
    _root, database, project_id, workspace_id = _database(tmp_path)
    task = _review_roundtrip(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        origin = base.rstrip("/")
        project = base + f"projects/{project_id}/"
        settings = project + "settings/"
        fields: dict[str, str | int] = {
            "action": "set_skill_scope",
            "project_id": project_id,
            "facet": "godot-project",
            "mode": "excluded",
        }
        status, headers, _ = _post(settings, fields, origin=origin)
        assert status == 303 and headers["Location"] == urlsplit(settings).path
        _, _, html = _read(settings)
        assert 'id="skill-scope"' in html
        assert 'name="action" value="delete_project"' in html
        assert 'name="action" value="relocate_workspace"' in html
        status, _, _ = _post(project, fields, origin=origin)
        assert status == 303  # Existing mutation URLs remain compatible.
        fields["mode"] = "invalid"
        status, _, body = _post(settings, fields, origin=origin)
        assert status == 400 and ">Настройки</a>" in _tabs(body.decode())
        status, _, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "feedback",
                "task_id": task.task_id,
                "workspace_id": workspace_id,
                "expected_revision": task.revision - 1,
                "feedback": "Несохранённое замечание",
            },
            origin=origin,
        )
        assert status == 409
        html = body.decode()
        assert "Несохранённое замечание" in html
        assert f"/vault/{project_id}/?workspace={workspace_id}&task={task.task_id}" in _tabs(html)
    finally:
        manager.close()
