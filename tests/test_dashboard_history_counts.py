from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import urlopen

import pytest

from harness.dashboard import (
    DashboardError,
    DashboardServerManager,
    _parse_page_request,
    _parse_sse_view,
    _view_fingerprint,
    read_dashboard_home,
    read_dashboard_project_detail,
    read_dashboard_task_detail,
    read_dashboard_workspace_detail,
    render_project_page,
    render_projects_page,
    render_task_page,
    render_workspace_page,
)
from harness.registry import create_project, register_workspace
from harness.search import SearchError
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import TaskCheckpointError, list_task_events
from harness.task_workflow import task_accept, task_checkpoint, task_start
from harness.tasks import TaskState, TaskWaitReason


def _database(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        return database, project.project_id, workspace.workspace_id
    finally:
        connection.close()


def _metric(html: str, label: str) -> int:
    match = re.search(
        rf'<span class="metric-label">{re.escape(label)}</span>'
        r'<strong class="metric-value">(\d+)</strong>',
        html,
    )
    assert match is not None
    return int(match[1])


class _HistoryLinks(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.links: dict[str, str] = {}
        self.events_url = ""
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "body":
            self.events_url = attributes.get("data-events-url") or ""
        if tag == "a" and attributes.get("rel") in {"prev", "next"}:
            relation = attributes.get("rel")
            href = attributes.get("href")
            assert relation is not None and href is not None
            self.links[relation] = href


def _read(url: str) -> str:
    with urlopen(url, timeout=3) as response:
        assert response.status == 200
        body = response.read()
        assert isinstance(body, bytes)
        return body.decode("utf-8")


def test_dashboard_metrics_count_every_waiting_and_working_task(tmp_path: Path) -> None:
    database, project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        for reason in (
            TaskWaitReason.OPERATOR_REVIEW,
            TaskWaitReason.OPERATOR_REVIEW,
            TaskWaitReason.EXTERNAL,
        ):
            task = task_start(connection, workspace_id, f"Ожидание {reason}")
            task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WAITING,
                wait_reason=reason,
                summary="Проверка завершена",
                next_step="Дождаться результата",
            )
        completed = task_start(connection, workspace_id, "Завершённая задача")
        task_accept(
            connection, workspace_id, completed.task_id, expected_revision=completed.revision
        )
        task_start(connection, workspace_id, "Текущая работа")
        other_root = tmp_path / "other"
        other_root.mkdir()
        other_project = create_project(connection)
        other_workspace = register_workspace(
            connection, project_id=other_project.project_id, path=other_root
        )
        task_start(connection, other_workspace.workspace_id, "Другой проект")
    finally:
        connection.close()

    home = read_dashboard_home(database)
    assert _metric(render_projects_page(home), "Активные задачи") == 5
    assert _metric(render_projects_page(home), "На ревью") == 2
    project = render_project_page(
        read_dashboard_project_detail(database, project_id), base_path="/"
    )
    assert "<span>Активные задачи: 4</span>" in project
    assert "<span>Ожидают проверки: 2</span>" in project


def test_task_history_pages_keep_all_tasks_and_search_query(tmp_path: Path) -> None:
    database, _project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    task_ids: set[str] = set()
    try:
        for number in range(26):
            task = task_start(connection, workspace_id, f"История {number:02}")
            task_ids.add(task.task_id)
            task_accept(connection, workspace_id, task.task_id, expected_revision=task.revision)
    finally:
        connection.close()

    first = read_dashboard_home(database, search_query="История")
    second = read_dashboard_home(database, search_query="История", page=2)
    assert len(first.recent_tasks) == 24
    assert len(second.recent_tasks) == 2
    first_ids = {row.task.task_id for row in first.recent_tasks}
    second_ids = {row.task.task_id for row in second.recent_tasks}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == task_ids
    assert first.task_count == second.task_count == 26
    assert (
        "?q=%D0%98%D1%81%D1%82%D0%BE%D1%80%D0%B8%D1%8F&amp;page=2#history"
        in render_projects_page(first)
    )
    assert "Страница 2 из 2" in render_projects_page(second)
    assert read_dashboard_home(database, page=999).page == 2

    workspace = read_dashboard_workspace_detail(database, workspace_id, page=2)
    assert {row.task.task_id for row in workspace.recent_tasks} == second_ids
    assert "Страница 2 из 2" in render_workspace_page(workspace, base_path="/")

    manager = DashboardServerManager(database)
    try:
        base_url = manager.get_url()
        for route in ("", f"workspaces/{workspace_id}/"):
            first_html = _read(base_url + route + "?" + urlencode({"q": "История"}))
            next_url = urljoin(base_url, _HistoryLinks(first_html).links["next"])
            assert urlsplit(next_url).path == "/" + route
            second_html = _read(next_url)
            assert "Страница 2 из 2" in second_html
            page_links = _HistoryLinks(second_html)
            assert "next" not in page_links.links
            assert "q=" in page_links.links["prev"]
            assert "page=1#history" in page_links.links["prev"]
            view, identity, query, snapshot, page = _parse_sse_view(
                urlsplit(page_links.events_url).query
            )
            assert page == 2
            assert query == "История"
            assert _view_fingerprint(database, view, identity, query, page) == snapshot
    finally:
        manager.close()


def test_old_timeline_page_has_its_checkpoints_and_latest_task_summary(tmp_path: Path) -> None:
    database, _project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        task = task_start(connection, workspace_id, "Длинная история")
        for number in range(63):
            task = task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WORKING,
                summary=f"Этап {number:03}",
            ).task
        all_events = list_task_events(connection, task.task_id)
    finally:
        connection.close()

    first = read_dashboard_task_detail(database, task.task_id)
    second = read_dashboard_task_detail(database, task.task_id, page=2)
    assert len(first.events) == 60
    assert len(second.events) == 4
    assert {event.event_id for event in first.events}.isdisjoint(
        {event.event_id for event in second.events}
    )
    assert second.events + first.events == all_events
    assert second.event_count == 64
    assert second.latest_checkpoint is not None
    assert second.latest_checkpoint.summary == "Этап 062"
    assert [checkpoint.summary for checkpoint in second.checkpoints] == [
        "Этап 000",
        "Этап 001",
        "Этап 002",
    ]
    html = render_task_page(second, base_path="/")
    assert "Этап 000" in html
    assert "Этап 062" in html
    assert "Страница 2 из 2" in html
    manager = DashboardServerManager(database)
    try:
        base_url = manager.get_url()
        first_html = _read(base_url + f"tasks/{task.task_id}/")
        next_url = urljoin(base_url, _HistoryLinks(first_html).links["next"])
        assert urlsplit(next_url).fragment == "timeline"
        second_html = _read(next_url)
        assert "Этап 000" in second_html
        assert "Этап 062" in second_html
        assert 'id="timeline"' in second_html
        page_links = _HistoryLinks(second_html)
        assert "next" not in page_links.links
        view, identity, query, snapshot, page = _parse_sse_view(
            urlsplit(page_links.events_url).query
        )
        assert page == 2
        assert _view_fingerprint(database, view, identity, query, page) == snapshot
    finally:
        manager.close()


@pytest.mark.parametrize("offset", [-1, True, 1.5, 2**63])
def test_task_event_offset_rejects_invalid_values(tmp_path: Path, offset: object) -> None:
    database, _project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        task = task_start(connection, workspace_id, "История")
        with pytest.raises(TaskCheckpointError, match="offset"):
            list_task_events(connection, task.task_id, limit=2, offset=offset)  # type: ignore[arg-type]
        with pytest.raises(TaskCheckpointError, match="bounded limit"):
            list_task_events(connection, task.task_id, offset=1)
    finally:
        connection.close()


@pytest.mark.parametrize("route", ["/", "/workspaces/example/", "/tasks/example/"])
@pytest.mark.parametrize(
    "query",
    ["page=0", "page=-1", "page=", "page=x", "page=2147483648", "page=1&page=2", "page=１２"],
)
def test_history_page_query_rejects_invalid_numbers_and_duplicates(route: str, query: str) -> None:
    with pytest.raises(SearchError):
        _parse_page_request("/", route, query)


@pytest.mark.parametrize("query", ["q=a&q=b", "q=a&page=2&extra=x", "page=2&q=" + "я" * 129])
def test_history_pagination_keeps_strict_search_schema(query: str) -> None:
    with pytest.raises(SearchError):
        _parse_page_request("/", "/", query)


def test_history_pagination_does_not_expand_project_or_task_query_scope() -> None:
    with pytest.raises(DashboardError):
        _parse_page_request("/", "/projects/example/", "page=2")
    with pytest.raises(DashboardError):
        _parse_page_request("/", "/tasks/example/", "q=hidden&page=2")
    with pytest.raises(DashboardError):
        _parse_sse_view(
            urlencode({"view": "task", "task_id": "example", "snapshot": "0" * 64, "page": "0"})
        )
