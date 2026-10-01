from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from test_dashboard_actions import _post
from test_dashboard_navigation_realtime import _database, _git, _read, _review_roundtrip

from harness.dashboard import (
    DashboardServerManager,
    _parse_sse_view,
    _view_fingerprint,
    read_dashboard_project_detail,
)
from harness.registry import create_project, register_workspace
from harness.storage import connect_database
from harness.task_workflow import task_feedback, task_start
from harness.tasks import TaskState, get_task


def test_project_tasks_cover_all_folders_with_bounded_search_and_pagination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, database, project_id, workspace_id = _database(tmp_path)
    review = _review_roundtrip(database, workspace_id)
    worktree = tmp_path / "other-folder"
    _git(root, "worktree", "add", "-b", "feature", str(worktree))
    connection = connect_database(database)
    try:
        second = register_workspace(connection, project_id=project_id, path=worktree)
        active = task_start(connection, second.workspace_id, "Поиск работы во второй папке")
        foreign_root = tmp_path / "foreign"
        foreign_root.mkdir()
        foreign = create_project(connection)
        foreign_workspace = register_workspace(
            connection, project_id=foreign.project_id, path=foreign_root
        )
        task_start(connection, foreign_workspace.workspace_id, "Поиск работы в чужом проекте")
    finally:
        connection.close()
    detail = read_dashboard_project_detail(database, project_id, search_query="Поиск работы")
    assert detail.task_count == 2
    assert [row.task.task_id for row in detail.recent_tasks] == [active.task_id]
    review_detail = read_dashboard_project_detail(database, project_id, scope="review")
    assert [row.task.task_id for row in review_detail.recent_tasks] == [review.task_id]
    assert review_detail.recent_tasks[0].summary == "Second review is ready"
    assert [hit.ref.partition("#")[0] for hit in detail.task_search_results] == [
        f"task:{active.task_id}"
    ]
    monkeypatch.setattr("harness.dashboard._DASHBOARD_RECENT_TASK_LIMIT", 1)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        _, _, home_html = _read(base)
        directory = re.search(
            rf'<article class="project-row"[^>]+data-filter-text="([^"]+)"[^>]*>'
            rf'<div class="project-row-name"><a class="project-name" href="/projects/{project_id}/"',
            home_html,
        )
        assert directory is not None
        assert str(root) in unescape(directory[1])
        assert str(worktree) in unescape(directory[1])
        path = f"projects/{project_id}/?" + urlencode({"q": "Поиск работы", "page": 2})
        _, _, html = _read(base + path)
        assert review.task_id not in html
        assert f'data-task-id="{active.task_id}"' in html
        assert ">Обзор</a>" not in html
        assert "Все папки" in html
        events = re.search(r'data-events-url="([^"]+)"', html)
        assert events is not None
        view, identity, query, snapshot, page = _parse_sse_view(urlsplit(unescape(events[1])).query)
        assert (view, identity, query, page) == ("project", project_id, "Поиск работы", 1)
        assert snapshot == _view_fingerprint(database, view, identity, query, page)
    finally:
        manager.close()


def test_inline_accept_uses_rendered_identity_revision_and_keeps_the_project_page(
    tmp_path: Path,
) -> None:
    _root, database, project_id, workspace_id = _database(tmp_path)
    task = _review_roundtrip(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        project_url = base + f"projects/{project_id}/?q=Polish"
        _, _, html = _read(project_url)
        article = re.search(
            rf'<article class="task-row" data-task-id="{task.task_id}".*?</article>', html
        )
        assert article is not None
        form = re.search(r'<form method="post" action="">(.*?)</form>', article[0])
        assert form is not None
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form[1]))
        assert fields == {
            "action": "accept",
            "workspace_id": workspace_id,
            "task_id": task.task_id,
            "expected_revision": str(task.revision),
        }
        status, headers, _body = _post(project_url, fields, origin=base.rstrip("/"))
        assert status == 303
        assert headers["Location"] == urlsplit(project_url).path + "?q=Polish"
        status, _, _ = _post(project_url, fields, origin=base.rstrip("/"))
        assert status == 409
        connection = connect_database(database)
        try:
            accepted = get_task(connection, task.task_id)
            assert accepted.state is TaskState.COMPLETED
            assert accepted.revision == task.revision + 1
        finally:
            connection.close()
        _, _, after = _read(project_url)
        assert 'name="action" value="accept"' not in after
        assert 'name="action" value="set_state"' in after
    finally:
        manager.close()


def test_feedback_replaces_obsolete_review_next_step(tmp_path: Path) -> None:
    _root, database, project_id, workspace_id = _database(tmp_path)
    task = _review_roundtrip(database, workspace_id)
    connection = connect_database(database)
    try:
        task_feedback(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=task.revision,
            feedback="Исправить восстановление после разрыва соединения",
        )
    finally:
        connection.close()
    detail = read_dashboard_project_detail(database, project_id)
    assert detail.recent_tasks[0].next_step == "Исправить восстановление после разрыва соединения"
    assert detail.workspaces[0].next_step == detail.recent_tasks[0].next_step
    assert detail.recent_tasks[0].task.state is TaskState.WORKING
