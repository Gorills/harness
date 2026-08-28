from __future__ import annotations

import os
import socket
import stat
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from harness.daemon import DaemonError, serve_daemon
from harness.dashboard import (
    DashboardError,
    DashboardGitBranch,
    DashboardServerManager,
    dashboard_token_path,
    dashboard_url_path,
    read_dashboard_task_detail,
    read_dashboard_workspace_detail,
    read_dashboard_workspace_rows,
    render_projects_page,
    render_task_page,
    render_workspace_page,
)
from harness.index import scan_workspace
from harness.ipc import (
    DashboardUrlResult,
    IpcError,
    IpcRemoteError,
    request_dashboard_url,
    request_runtime_diagnostics,
    request_status,
)
from harness.registry import create_project, register_workspace
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.task_checkpoints import checkpoint_task
from harness.task_workflow import task_start
from harness.tasks import TaskState, TaskWaitReason, create_task_record

pytestmark = pytest.mark.skipif(os.name == "nt", reason="dashboard daemon discovery uses POSIX IPC")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path) -> None:
    path.mkdir()
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(path, "init", "-b", "main")
    _git(path, "add", "tracked.txt")
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


def _registered_database(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    _make_repo(root)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        return root, database, workspace.workspace_id
    finally:
        connection.close()


def _start_server(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop_event)
    deadline = time.monotonic() + 3
    while True:
        if future.done():
            future.result()
        try:
            if request_status(socket_path).schema_version == SCHEMA_VERSION:
                return stop_event, executor, future
        except IpcError:
            pass
        if time.monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon did not become ready")
        time.sleep(0.01)


def _stop_server(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def test_dashboard_loopback_page_is_capability_scoped_and_escapes_task_text(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _registered_database(tmp_path)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "<script>alert('task')</script>")
        checkpoint_task(
            connection,
            task.task_id,
            expected_revision=task.revision,
            expected_workspace_id=workspace_id,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Ready for review",
            next_step='<img src=x onerror="alert(1)">',
        )
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        assert manager.get_url() == url
        parsed = urlsplit(url)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        assert parsed.path != "/"
        assert parsed.query == ""
        assert parsed.fragment == ""

        with pytest.raises(HTTPError) as denied:
            urlopen(f"http://127.0.0.1:{parsed.port}/", timeout=2)
        assert denied.value.code == 404

        with urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "Проекты · Harness" in body
        assert "Проекты, активные задачи и точки внимания" in body
        assert "ревью" in body
        assert "&lt;script&gt;alert(&#x27;task&#x27;)&lt;/script&gt;" in body
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in body
        assert "<script>alert('task')</script>" not in body
        assert '<img src=x onerror="alert(1)">' not in body
    finally:
        manager.close()


def test_dashboard_keeps_persisted_overview_when_workspace_git_is_unavailable(
    tmp_path: Path,
) -> None:
    root, database, workspace_id = _registered_database(tmp_path)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "Completed task")
        checkpoint_task(
            connection,
            task.task_id,
            expected_revision=task.revision,
            expected_workspace_id=workspace_id,
            state=TaskState.COMPLETED,
            summary="Done",
        )
    finally:
        connection.close()

    root.rename(tmp_path / "repo-moved")
    rows = read_dashboard_workspace_rows(database)

    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == workspace_id
    assert row.task_title == "Completed task"
    assert row.task_state == "completed"
    assert row.task_git_branch == DashboardGitBranch(captured=True, name="main")
    assert row.branch is None
    assert row.dirty_path_count is None
    assert row.live_error == "Git status unavailable"
    assert row.indexed_file_count == 1
    html = render_projects_page(rows, base_path="/cap/")
    assert 'class="task-git-branch"' in html
    assert '<strong class="mono">main</strong>' in html
    assert "Git недоступен" in html


def test_daemon_starts_dashboard_with_runtime_and_reuses_url_over_user_ipc(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    url_file = dashboard_url_path(socket_path)
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        diagnostics = request_runtime_diagnostics(socket_path)
        assert diagnostics.dashboard_running is True
        published = url_file.read_text(encoding="ascii").strip()
        assert stat.S_IMODE(url_file.stat().st_mode) == 0o600
        first = request_dashboard_url(socket_path)
        second = request_dashboard_url(socket_path)
        assert isinstance(first, DashboardUrlResult)
        assert second == first
        assert first.url == published
        with urlopen(first.url, timeout=2) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
        assert "Пока нет рабочих копий" in body
        assert "harness scan" in body
        assert 'lang="ru"' in body
    finally:
        _stop_server(stop_event, executor, future)
    assert not url_file.exists()


def test_dashboard_start_failure_is_bounded_and_daemon_keeps_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        monkeypatch.setattr(
            DashboardServerManager,
            "get_url",
            lambda _self: (_ for _ in ()).throw(DashboardError("startup failed")),
        )
        with pytest.raises(IpcRemoteError) as raised:
            request_dashboard_url(socket_path)
        assert raised.value.code == "dashboard_unavailable"
        assert request_status(socket_path).schema_version == SCHEMA_VERSION
        assert not future.done()
    finally:
        _stop_server(stop_event, executor, future)


def test_dashboard_shutdown_failure_does_not_leak_daemon_socket_or_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    original_close = DashboardServerManager.close

    def close_then_fail(manager: DashboardServerManager) -> None:
        original_close(manager)
        raise DashboardError("synthetic shutdown failure")

    stop_event, executor, future = _start_server(database, socket_path)
    request_dashboard_url(socket_path)
    monkeypatch.setattr(DashboardServerManager, "close", close_then_fail)
    stop_event.set()
    executor.shutdown(wait=True)
    with pytest.raises(DaemonError, match="dashboard server did not stop cleanly"):
        future.result()
    assert not socket_path.exists()

    monkeypatch.setattr(DashboardServerManager, "close", original_close)
    second_stop, second_executor, second_future = _start_server(database, socket_path)
    _stop_server(second_stop, second_executor, second_future)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert isinstance(port, int) and 1 <= port <= 65535
    return port


def test_dashboard_reuses_durable_token_and_preferred_port_across_restarts(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    port = _free_loopback_port()
    token_file = dashboard_token_path(database)
    first = DashboardServerManager(database, port=port)
    try:
        url = first.get_url()
        parsed = urlsplit(url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == port
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    finally:
        first.close()

    second = DashboardServerManager(database, port=port)
    try:
        assert second.get_url() == url
        with urlopen(url, timeout=2) as response:
            assert response.status == 200
    finally:
        second.close()
    assert token_file.exists()


def test_dashboard_busy_preferred_port_is_bounded(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    with socket.create_server(("127.0.0.1", 0), reuse_port=False) as occupied:
        port = occupied.getsockname()[1]
        manager = DashboardServerManager(database, port=port)
        with pytest.raises(DashboardError, match="could not bind"):
            manager.get_url()
        assert not manager.is_running()


def test_daemon_binds_selected_dashboard_listen_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    port = _free_loopback_port()
    monkeypatch.setattr(
        "harness.runtime_paths.dashboard_listen_port",
        lambda _socket, **_kwargs: port,
    )
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        url = request_dashboard_url(socket_path).url
        parsed = urlsplit(url)
        assert parsed.port == port
        with urlopen(url, timeout=2) as response:
            assert response.status == 200
    finally:
        _stop_server(stop_event, executor, future)


def test_dashboard_keeps_task_git_branch_after_live_checkout_moves(tmp_path: Path) -> None:
    root, database, workspace_id = _registered_database(tmp_path)
    _git(root, "switch", "-c", "feature/dashboard-branch")
    connection = connect_database(database)
    try:
        task_start(connection, workspace_id, "Work on feature")
    finally:
        connection.close()
    _git(root, "switch", "main")

    rows = read_dashboard_workspace_rows(database)
    assert len(rows) == 1
    row = rows[0]
    assert row.branch == "main"
    assert row.task_git_branch == DashboardGitBranch(captured=True, name="feature/dashboard-branch")
    overview = render_projects_page(rows, base_path="/cap/")
    assert 'class="task-git-branch"' in overview
    assert '<strong class="mono">feature/dashboard-branch</strong>' in overview
    assert '<div class="mini-stat"><span>Ветка</span><strong>main</strong></div>' in overview

    workspace = read_dashboard_workspace_detail(database, workspace_id)
    assert workspace.recent_tasks[0].git_branch == DashboardGitBranch(
        captured=True, name="feature/dashboard-branch"
    )
    workspace_html = render_workspace_page(workspace, base_path="/cap/")
    assert 'Ветка <span class="mono">feature/dashboard-branch</span>' in workspace_html

    assert row.task_id is not None
    detail = read_dashboard_task_detail(database, row.task_id)
    assert detail.git_branch == DashboardGitBranch(captured=True, name="feature/dashboard-branch")
    assert detail.baseline_git_branch == DashboardGitBranch(
        captured=True, name="feature/dashboard-branch"
    )
    task_html = render_task_page(detail, base_path="/cap/")
    assert '<dt>Ветка</dt><dd class="mono">feature/dashboard-branch</dd>' in task_html
    assert (
        '<div class="timeline-branch"><strong>Ветка</strong> '
        '<span class="mono">feature/dashboard-branch</span></div>'
    ) in task_html


def test_dashboard_prefers_latest_checkpoint_branch_over_baseline(tmp_path: Path) -> None:
    root, database, workspace_id = _registered_database(tmp_path)
    connection = connect_database(database)
    try:
        task = task_start(connection, workspace_id, "Started on main")
    finally:
        connection.close()
    _git(root, "switch", "-c", "feature/later")
    connection = connect_database(database)
    try:
        checkpoint_task(
            connection,
            task.task_id,
            expected_revision=task.revision,
            expected_workspace_id=workspace_id,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Moved to a feature branch",
            next_step="Review the move",
        )
    finally:
        connection.close()
    _git(root, "switch", "main")

    detail = read_dashboard_task_detail(database, task.task_id)
    assert detail.baseline_git_branch == DashboardGitBranch(captured=True, name="main")
    assert detail.git_branch == DashboardGitBranch(captured=True, name="feature/later")
    html = render_task_page(detail, base_path="/cap/")
    assert '<dt>Ветка</dt><dd class="mono">feature/later</dd>' in html
    assert (
        '<div class="timeline-branch"><strong>Ветка</strong> <span class="mono">main</span></div>'
    ) in html
    assert (
        '<div class="timeline-branch"><strong>Ветка</strong> '
        '<span class="mono">feature/later</span></div>'
    ) in html


def test_dashboard_shows_detached_head_for_task_without_named_branch(tmp_path: Path) -> None:
    root, database, workspace_id = _registered_database(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(root, "checkout", "--detach", head)
    connection = connect_database(database)
    try:
        create_task_record(connection, workspace_id, "Detached work")
    finally:
        connection.close()

    rows = read_dashboard_workspace_rows(database)
    assert rows[0].task_git_branch == DashboardGitBranch(captured=True, name=None)
    html = render_projects_page(rows, base_path="/cap/")
    assert '<strong class="mono">(detached)</strong>' in html
    assert rows[0].branch is None
    assert '<div class="mini-stat"><span>Ветка</span><strong>—</strong></div>' in html
