from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from test_dashboard_history_counts import _database, _HistoryLinks
from test_dashboard_navigation_realtime import _read

from harness.dashboard import (
    DashboardError,
    DashboardServerManager,
    _parse_event_scope,
    _parse_page_request,
    _parse_sse_view,
    _view_fingerprint,
    read_dashboard_home,
    read_dashboard_project_detail,
    read_dashboard_workspace_detail,
    render_project_page,
    render_projects_page,
    render_workspace_page,
)
from harness.registry import create_project, register_workspace
from harness.search import SearchError
from harness.storage import connect_database
from harness.task_workflow import task_accept, task_checkpoint, task_set_state, task_start
from harness.tasks import TaskState, TaskWaitReason


def test_review_portfolio_and_project_scopes_have_independent_pages(tmp_path: Path) -> None:
    database, project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    reviews = set()
    active = set()
    try:
        for number in range(25):
            task = task_start(connection, workspace_id, f"Готовый результат {number}")
            task = task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WAITING,
                wait_reason=TaskWaitReason.OPERATOR_REVIEW,
                summary="Можно принимать",
                next_step="Проверить результат",
            ).task
            reviews.add(task.task_id)
        task = task_start(connection, workspace_id, "Старый результат")
        completed = task_accept(
            connection, workspace_id, task.task_id, expected_revision=task.revision
        )
        for number in range(2):
            task = task_start(connection, workspace_id, f"Текущая работа {number}")
            if number == 0:
                task_checkpoint(
                    connection,
                    workspace_id,
                    task.task_id,
                    expected_revision=task.revision,
                    state=TaskState.WAITING,
                    wait_reason=TaskWaitReason.EXTERNAL,
                    summary="Ждём ответа сервиса",
                    next_step="Дождаться ответа",
                )
            else:
                active.add(task.task_id)
        foreign = create_project(connection)
        foreign_root = tmp_path / "foreign"
        foreign_root.mkdir()
        foreign_workspace = register_workspace(
            connection, project_id=foreign.project_id, path=foreign_root
        )
        foreign_task = task_start(
            connection, foreign_workspace.workspace_id, "Работа в другом проекте"
        )
    finally:
        connection.close()
    first = read_dashboard_home(database)
    second = read_dashboard_home(database, page=2)
    assert first.scope == second.scope == "projects"
    assert first.task_count == 29
    assert first.listed_task_count == 25
    assert len(first.recent_tasks) == 24
    assert len(second.recent_tasks) == 1
    assert {row.task.task_id for row in first.recent_tasks + second.recent_tasks} == reviews
    project = read_dashboard_project_detail(database, project_id, page=2)
    assert project.scope == "active"
    assert project.task_count == 28
    assert project.listed_task_count == 1
    assert project.page == 1
    assert active == {row.task.task_id for row in project.recent_tasks}
    assert completed.task.task_id not in {row.task.task_id for row in project.recent_tasks}
    assert foreign_task.task_id not in {row.task.task_id for row in project.recent_tasks}
    archive = read_dashboard_project_detail(database, project_id, scope="archive", page=999)
    assert archive.page == 1
    assert archive.listed_task_count == 1
    assert [row.task.task_id for row in archive.recent_tasks] == [completed.task.task_id]
    workspace = read_dashboard_workspace_detail(database, workspace_id, scope="review", page=2)
    assert workspace.listed_task_count == 25
    assert len(workspace.recent_tasks) == 1
    global_active = read_dashboard_home(database, scope="active", page=2)
    assert global_active.page == 1
    assert global_active.listed_task_count == 2
    assert {row.task.task_id for row in global_active.recent_tasks} == active | {
        foreign_task.task_id
    }
    workspace_active = read_dashboard_workspace_detail(database, workspace_id)
    assert workspace_active.listed_task_count == 1
    assert {row.task.task_id for row in workspace_active.recent_tasks} == active
    assert workspace_active.workspace.active_task_count == 1
    assert workspace_active.workspace.review_task_count == 25
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        for path in (
            "?scope=projects&page=2",
            "?scope=active&page=2",
            f"projects/{project_id}/?scope=archive",
            f"workspaces/{workspace_id}/?scope=review&page=2",
        ):
            _, _, html = _read(base + path)
            events = re.search(r'data-events-url="([^"]+)"', html)
            assert events is not None
            query = urlsplit(unescape(events[1])).query
            view, identity, search, snapshot, page = _parse_sse_view(query)
            scope = _parse_event_scope(query, view)
            assert snapshot == _view_fingerprint(database, view, identity, search, page, scope)
        _, _, search_html = _read(base + "?" + urlencode({"q": "Текущая"}))
        assert len(re.findall('name="action" value="set_state"', search_html)) == 2
        for task_id in active:
            assert search_html.count(f'data-task-id="{task_id}"') == 1
        assert foreign_task.task_id not in search_html
        assert 'class="portfolio-layout"' not in search_html
    finally:
        manager.close()


@pytest.mark.parametrize("reason", list(TaskWaitReason))
def test_waiting_tasks_are_excluded_from_active_lists_and_counts(
    tmp_path: Path, reason: TaskWaitReason
) -> None:
    database, project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        waiting = task_start(connection, workspace_id, "Ожидающая задача")
        task_checkpoint(
            connection,
            workspace_id,
            waiting.task_id,
            expected_revision=waiting.revision,
            state=TaskState.WAITING,
            wait_reason=reason,
            summary="Ожидает следующего действия",
            next_step="Дождаться ответа или проверки",
        )
        working = task_start(connection, workspace_id, "Задача в работе")
    finally:
        connection.close()
    for detail in (
        read_dashboard_home(database, scope="active"),
        read_dashboard_project_detail(database, project_id),
        read_dashboard_workspace_detail(database, workspace_id),
    ):
        assert detail.listed_task_count == 1
        assert [row.task.task_id for row in detail.recent_tasks] == [working.task_id]
    home = read_dashboard_home(database, scope="active")
    assert home.workspaces[0].active_task_count == 1
    assert home.workspaces[0].review_task_count == (reason is TaskWaitReason.OPERATOR_REVIEW)
    assert home.workspaces[0].waiting_task_count == (reason is not TaskWaitReason.OPERATOR_REVIEW)
    assert home.workspaces[0].archived_task_count == 0
    assert ">Архив<span>0</span>" in render_projects_page(home)
    review = read_dashboard_home(database, scope="review")
    assert [row.task.task_id for row in review.recent_tasks] == (
        [waiting.task_id] if reason is TaskWaitReason.OPERATOR_REVIEW else []
    )
    expected_waiting = [] if reason is TaskWaitReason.OPERATOR_REVIEW else [waiting.task_id]
    waiting_home = read_dashboard_home(database, scope="waiting")
    waiting_project = read_dashboard_project_detail(database, project_id, scope="waiting")
    waiting_workspace = read_dashboard_workspace_detail(database, workspace_id, scope="waiting")
    for detail, html, url in (
        (waiting_home, render_projects_page(waiting_home), "/"),
        (
            waiting_project,
            render_project_page(waiting_project, base_path="/"),
            f"/projects/{project_id}/",
        ),
        (
            waiting_workspace,
            render_workspace_page(waiting_workspace, base_path="/"),
            f"/workspaces/{workspace_id}/",
        ),
    ):
        assert detail.listed_task_count == len(expected_waiting)
        assert [row.task.task_id for row in detail.recent_tasks] == expected_waiting
        assert (
            f'<a href="{url}?scope=waiting" aria-current="page">'
            f"Ожидание<span>{len(expected_waiting)}</span></a>"
        ) in html
        assert 'name="action" value="accept"' not in html
    all_tasks = read_dashboard_home(database, scope="all")
    assert {row.task.task_id for row in all_tasks.recent_tasks} == {
        waiting.task_id,
        working.task_id,
    }


def test_waiting_scope_pagination_boundaries_and_sse_follow_current_state(tmp_path: Path) -> None:
    database, project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    waiting_ids = set()
    try:
        for number in range(25):
            task = task_start(connection, workspace_id, f"Ожидание зависимости {number}")
            task = task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WAITING,
                wait_reason=(
                    TaskWaitReason.EXTERNAL if number % 2 else TaskWaitReason.OPERATOR_INPUT
                ),
                summary="Ожидается ответ",
                next_step="Продолжить после ответа",
            ).task
            waiting_ids.add(task.task_id)
        resumed = task
        sibling_root = tmp_path / "sibling"
        sibling_root.mkdir()
        sibling = register_workspace(connection, project_id=project_id, path=sibling_root)
        sibling_task = task_start(connection, sibling.workspace_id, "Ожидание в другой папке")
        task_checkpoint(
            connection,
            sibling.workspace_id,
            sibling_task.task_id,
            expected_revision=sibling_task.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
            summary="Ожидается сервис",
            next_step="Продолжить после ответа сервиса",
        )
        foreign = create_project(connection)
        foreign_root = tmp_path / "foreign"
        foreign_root.mkdir()
        foreign_workspace = register_workspace(
            connection, project_id=foreign.project_id, path=foreign_root
        )
        foreign_task = task_start(connection, foreign_workspace.workspace_id, "Чужая зависимость")
        task_checkpoint(
            connection,
            foreign_workspace.workspace_id,
            foreign_task.task_id,
            expected_revision=foreign_task.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
            summary="Ожидается сервис другого проекта",
            next_step="Продолжить после ответа сервиса",
        )
    finally:
        connection.close()
    for pages, expected in (
        (
            [read_dashboard_home(database, scope="waiting", page=page) for page in (1, 2)],
            waiting_ids | {sibling_task.task_id, foreign_task.task_id},
        ),
        (
            [
                read_dashboard_project_detail(database, project_id, scope="waiting", page=page)
                for page in (1, 2)
            ],
            waiting_ids | {sibling_task.task_id},
        ),
        (
            [
                read_dashboard_workspace_detail(database, workspace_id, scope="waiting", page=page)
                for page in (1, 2)
            ],
            waiting_ids,
        ),
    ):
        assert all(detail.listed_task_count == len(expected) for detail in pages)
        assert len(pages[0].recent_tasks) == 24
        assert len(pages[1].recent_tasks) == len(expected) - 24
        assert {row.task.task_id for detail in pages for row in detail.recent_tasks} == expected
    before = _view_fingerprint(database, "workspace", workspace_id, None, 2, "waiting")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        for route in ("", f"projects/{project_id}/", f"workspaces/{workspace_id}/"):
            _, _, html = _read(base + route + "?scope=waiting")
            links = _HistoryLinks(html)
            assert "scope=waiting" in links.links["next"]
            _, _, second_html = _read(base + unescape(links.links["next"]).removeprefix("/"))
            second_links = _HistoryLinks(second_html)
            assert "scope=waiting" in second_links.links["prev"]
            query = urlsplit(second_links.events_url).query
            view, identity, search, snapshot, page = _parse_sse_view(query)
            scope = _parse_event_scope(query, view)
            assert scope == "waiting"
            assert snapshot == _view_fingerprint(database, view, identity, search, page, scope)
        connection = connect_database(database)
        try:
            task_set_state(
                connection,
                workspace_id,
                resumed.task_id,
                expected_revision=resumed.revision,
                state=TaskState.WORKING,
            )
        finally:
            connection.close()
        after = _view_fingerprint(database, "workspace", workspace_id, None, 2, "waiting")
        assert after != before
        _, _, html = _read(base + f"workspaces/{workspace_id}/?scope=waiting&page=2")
        assert 'aria-current="page">Ожидание<span>24</span>' in html
        waiting = read_dashboard_workspace_detail(database, workspace_id, scope="waiting", page=2)
        assert resumed.task_id not in {row.task.task_id for row in waiting.recent_tasks}
        assert waiting.page == 1
        active = read_dashboard_workspace_detail(database, workspace_id, scope="active")
        assert [row.task.task_id for row in active.recent_tasks] == [resumed.task_id]
    finally:
        manager.close()


@pytest.mark.parametrize("scope", ["", "other", "review&scope=active", "projects"])
def test_task_scopes_are_singular_and_do_not_expand_other_routes(scope: str) -> None:
    with pytest.raises(SearchError):
        _parse_page_request("/", "/projects/example/", f"scope={scope}")
    with pytest.raises(DashboardError):
        _parse_page_request("/", "/tasks/example/", f"scope={scope}")
    with pytest.raises(DashboardError):
        _parse_page_request("/", "/projects/example/settings/", f"scope={scope}")
    with pytest.raises(DashboardError):
        _parse_sse_view(f"view=project&project_id=example&snapshot={'0' * 64}&scope={scope}")


@pytest.mark.parametrize("query", ["scope=", "scope=unknown", "scope=review&scope=active"])
def test_home_scope_schema_rejects_unknown_and_duplicate_filters(query: str) -> None:
    with pytest.raises(SearchError):
        _parse_page_request("/", "/", query)
    with pytest.raises(DashboardError):
        _parse_sse_view(f"view=projects&snapshot={'0' * 64}&{query}")
