from __future__ import annotations

import http.client
import re
import subprocess
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from harness.dashboard import DashboardServerManager
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_checkpoint, task_feedback, task_start
from harness.tasks import TaskRecord, TaskState, TaskWaitReason


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _database(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "feature_flag.py").write_text("ENABLED = True\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "operator-guide.md").write_text("# Operator guide\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
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
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        return root, database, project.project_id, workspace.workspace_id
    finally:
        connection.close()


def _review_roundtrip(database: Path, workspace_id: str) -> TaskRecord:
    connection = connect_database(database)
    try:
        started = task_start(connection, workspace_id, "Polish <b>dashboard</b>")
        first_review = task_checkpoint(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=started.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="First review <mark>needs escaping</mark>",
            next_step="Check spacing and hierarchy",
        ).task
        resumed = task_feedback(
            connection,
            workspace_id,
            first_review.task_id,
            expected_revision=first_review.revision,
            feedback="Tighten <b>mobile</b> spacing",
        ).task
        return task_checkpoint(
            connection,
            workspace_id,
            resumed.task_id,
            expected_revision=resumed.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Second review is ready",
            next_step="Accept or send one more note",
        ).task
    finally:
        connection.close()


def _read(url: str) -> tuple[int, dict[str, str], str]:
    with urlopen(url, timeout=3) as response:
        return (
            response.status,
            {name: value for name, value in response.getheaders()},
            response.read().decode("utf-8"),
        )


class _DashboardButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "button":
            self.buttons.append(dict(attrs))


def test_dashboard_drilldown_search_timeline_and_assets_are_capability_scoped(
    tmp_path: Path,
) -> None:
    _root, database, project_id, workspace_id = _database(tmp_path)
    task = _review_roundtrip(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        base_url = manager.get_url()
        parsed = urlsplit(base_url)
        origin = f"http://127.0.0.1:{parsed.port}"

        status, headers, overview = _read(base_url)
        assert status == 200
        assert "style-src 'self'" in headers["Content-Security-Policy"]
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert "connect-src 'self'" in headers["Content-Security-Policy"]
        assert "unsafe-inline" not in headers["Content-Security-Policy"]
        assert "<style" not in overview
        assert "dashboard.css" in overview
        assert "dashboard.js" in overview
        assert "Проекты" in overview
        assert 'lang="ru"' in overview
        assert 'class="app-sidebar"' in overview
        assert 'class="project-navigation"' in overview
        assert f"workspaces/{quote(workspace_id, safe='')}/" in overview
        assert f"projects/{quote(project_id, safe='')}/" not in overview
        assert 'class="nav-task"' not in overview
        assert "Поиск по всем задачам" in overview
        assert "Последние задачи" in overview
        assert '<nav class="breadcrumbs"' in overview
        assert "<ol>" in overview
        assert 'aria-current="page"' in overview
        assert "LOCAL CONTROL PLANE" not in overview
        assert 'class="task-git-branch"' in overview
        assert '<strong class="mono">main</strong>' in overview
        parser = _DashboardButtonParser()
        parser.feed(overview)
        refresh_buttons = [attrs for attrs in parser.buttons if "data-refresh-now" in attrs]
        assert refresh_buttons, parser.buttons
        assert all('"' not in name for attrs in parser.buttons for name in attrs)

        status, css_headers, css = _read(base_url + "assets/dashboard.css")
        assert status == 200
        assert css_headers["Content-Type"].startswith("text/css")
        assert "--accent: #748cff" in css
        assert "prefers-reduced-motion" in css

        status, js_headers, javascript = _read(base_url + "assets/dashboard.js")
        assert status == 200
        assert js_headers["Content-Type"].startswith("application/javascript")
        assert "EventSource" in javascript
        assert "hasUnsavedInput" in javascript
        assert "location.reload" not in javascript
        assert "DOMParser" in javascript
        assert "replaceWith" in javascript

        project_url = base_url + f"projects/{quote(project_id, safe='')}/"
        workspace_url = base_url + f"workspaces/{quote(workspace_id, safe='')}/"
        task_url = base_url + f"tasks/{quote(task.task_id, safe='')}/"

        status, _headers, project_page = _read(project_url)
        assert status == 200
        assert project_id in project_page
        assert workspace_id in project_page
        assert 'class="nav-project is-context"' in project_page

        status, _headers, workspace_page = _read(
            workspace_url + "?" + urlencode({"q": "feature flag"})
        )
        assert status == 200
        assert "src/feature_flag.py" in workspace_page
        assert "идентификатор" in workspace_page
        assert "ENABLED = True" not in workspace_page
        assert task.task_id[:10] in workspace_page
        assert 'Ветка <strong class="mono">main</strong>' in workspace_page
        assert "Текущая задача" in workspace_page
        assert "Папка" in workspace_page
        assert "Удаление проекта" in workspace_page
        assert f'action="/projects/{quote(project_id, safe="")}/"' in workspace_page

        status, _headers, task_page = _read(task_url)
        assert status == 200
        assert "История" in task_page
        assert "Замечание" in task_page
        assert "Tighten &lt;b&gt;mobile&lt;/b&gt; spacing" in task_page
        assert "First review &lt;mark&gt;needs escaping&lt;/mark&gt;" in task_page
        assert "<b>dashboard</b>" not in task_page
        assert "<mark>needs escaping</mark>" not in task_page
        assert ">Принять<" in task_page
        assert f"workspaces/{quote(workspace_id, safe='')}/" in task_page
        assert f'href="/workspaces/{quote(workspace_id, safe="")}/"' in task_page
        assert "<span>Задача</span>" in task_page
        assert '<dt>Ветка</dt><dd class="mono">main</dd>' in task_page
        assert (
            '<div class="timeline-branch"><strong>Ветка</strong> '
            '<span class="mono">main</span></div>'
        ) in task_page

        status, _headers, root_task = _read(f"{origin}/tasks/{quote(task.task_id, safe='')}/")
        assert status == 200
        assert "История" in root_task
        status, _headers, _root_css = _read(f"{origin}/assets/dashboard.css")
        assert status == 200
        with pytest.raises(HTTPError) as unknown_prefix:
            urlopen(
                f"{origin}/not-a-dashboard/tasks/{quote(task.task_id, safe='')}/",
                timeout=2,
            )
        assert unknown_prefix.value.code == 404
        with pytest.raises(HTTPError) as malformed_events:
            urlopen(base_url + "events?view=projects", timeout=2)
        assert malformed_events.value.code == 400

        with pytest.raises(HTTPError) as malformed_search:
            urlopen(workspace_url + "?q=feature&extra=1", timeout=2)
        assert malformed_search.value.code == 400

        status, _headers, home_search = _read(base_url + "?" + urlencode({"q": "Polish dashboard"}))
        assert status == 200
        assert "search-hit" in home_search
        assert "Polish" in home_search
        assert "ENABLED = True" not in home_search
        with pytest.raises(HTTPError) as malformed_home_search:
            urlopen(base_url + "?q=feature&extra=1", timeout=2)
        assert malformed_home_search.value.code == 400
    finally:
        manager.close()


def _events_path(page: str) -> str:
    match = re.search(r'data-events-url="([^"]+)"', page)
    if match is None:
        raise AssertionError("dashboard page did not expose a scoped SSE URL")
    return unescape(match.group(1))


def _read_sse_event(response: http.client.HTTPResponse) -> tuple[str, str]:
    event = ""
    data = ""
    while True:
        raw = response.fp.readline()
        if not raw:
            raise AssertionError("SSE stream closed before an event arrived")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if event:
                return event, data
            continue
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = line.removeprefix("data: ")


def test_dashboard_sse_emits_refresh_hint_after_external_task_change(tmp_path: Path) -> None:
    _root, database, _project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        task = task_start(connection, workspace_id, "Realtime task")
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    connection_http: http.client.HTTPConnection | None = None
    try:
        base_url = manager.get_url()
        parsed = urlsplit(base_url)
        assert parsed.hostname is not None and parsed.port is not None
        workspace_url = base_url + f"workspaces/{quote(workspace_id, safe='')}/"
        _status, _headers, workspace_page = _read(workspace_url)
        events_path = _events_path(workspace_page)
        assert "snapshot=" in events_path
        connection_http = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
        connection_http.request("GET", events_path)
        response = connection_http.getresponse()
        assert response.status == 200
        headers = {name: value for name, value in response.getheaders()}
        assert headers["Content-Type"].startswith("text/event-stream")
        assert headers["Cache-Control"] == "no-store"
        assert "connect-src 'self'" in headers["Content-Security-Policy"]
        assert "Server" not in headers
        assert "Date" not in headers

        event, data = _read_sse_event(response)
        assert (event, data) == ("ready", "live")

        db = connect_database(database)
        try:
            task_checkpoint(
                db,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WAITING,
                wait_reason=TaskWaitReason.OPERATOR_REVIEW,
                summary="Realtime review",
                next_step="Wait for operator",
            )
        finally:
            db.close()

        event, data = _read_sse_event(response)
        assert (event, data) == ("refresh", "changed")
        assert "Realtime task" not in data
        assert "Realtime review" not in data
    finally:
        if connection_http is not None:
            connection_http.close()
        manager.close()


def test_dashboard_sse_detects_change_between_page_render_and_stream_connect(
    tmp_path: Path,
) -> None:
    _root, database, _project_id, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        task = task_start(connection, workspace_id, "Snapshot race task")
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    connection_http: http.client.HTTPConnection | None = None
    try:
        base_url = manager.get_url()
        parsed = urlsplit(base_url)
        assert parsed.hostname is not None and parsed.port is not None
        workspace_url = base_url + f"workspaces/{quote(workspace_id, safe='')}/"
        _status, _headers, workspace_page = _read(workspace_url)
        events_path = _events_path(workspace_page)

        db = connect_database(database)
        try:
            task_checkpoint(
                db,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WAITING,
                wait_reason=TaskWaitReason.OPERATOR_REVIEW,
                summary="Changed before SSE connect",
                next_step="Surface freshness immediately",
            )
        finally:
            db.close()

        connection_http = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=4)
        connection_http.request("GET", events_path)
        response = connection_http.getresponse()
        assert response.status == 200
        assert _read_sse_event(response) == ("ready", "live")
        assert _read_sse_event(response) == ("refresh", "changed")
    finally:
        if connection_http is not None:
            connection_http.close()
        manager.close()
