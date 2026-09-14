from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.daemon as daemon
from harness.dashboard import (
    _view_fingerprint,
    read_dashboard_home,
    read_dashboard_project_detail,
    read_dashboard_task_detail,
    read_dashboard_workspace_detail,
    render_workspace_page,
)
from harness.git_applicability import GitApplicabilityError, WorkspaceApplicability
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectRetrievalRefError
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_accept, task_checkpoint, task_start
from harness.tasks import TaskRecord, TaskState, TaskWaitReason, get_relevant_task
from harness.workspace_resolution import WorkspaceHint


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(
        root, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-m", message
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("answer = 1\n")
    _git(root, "init", "-b", "main")
    _commit(root, "baseline")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, database, connection, project.project_id, workspace.workspace_id


def _feature_task(connection: sqlite3.Connection, root: Path, workspace: str) -> TaskRecord:
    _git(root, "checkout", "-b", "feature")
    task = task_start(connection, workspace, "Изменения только feature")
    (root / "source.py").write_text("answer = 2\n")
    _commit(root, "feature code")
    return task_checkpoint(
        connection,
        workspace,
        task.task_id,
        expected_revision=task.revision,
        state=TaskState.WAITING,
        wait_reason=TaskWaitReason.OPERATOR_REVIEW,
        summary="Изменён ответ",
        next_step="Проверить feature",
    ).task


def test_workspace_and_project_filter_branch_while_global_archive_remains_available(
    tmp_path: Path,
) -> None:
    root, database, connection, project, workspace = _setup(tmp_path)
    try:
        task = _feature_task(connection, root, workspace)
        feature_fingerprint = _view_fingerprint(database, "workspace", workspace, None)
        _git(root, "checkout", "main")
        detail = read_dashboard_workspace_detail(database, workspace, search_query="feature")
        assert detail.workspace.task_id is None
        assert detail.workspace.active_task_count == detail.workspace.review_task_count == 0
        assert detail.task_count == 0
        assert detail.recent_tasks == ()
        assert detail.task_search_results == ()
        assert read_dashboard_project_detail(database, project).workspaces[0].task_id is None
        assert _view_fingerprint(database, "workspace", workspace, None) != feature_fingerprint
        assert read_dashboard_home(database).recent_tasks[0].task.task_id == task.task_id
        assert read_dashboard_task_detail(database, task.task_id).task == task
        status = daemon.read_workspace_task_status(connection, [WorkspaceHint(root, "test")])
        assert status.task is None and status.last_checkpoint is None
        assert status.branch == "main"
        with pytest.raises(ProjectRetrievalRefError):
            daemon.read_project_context_result(
                connection, [WorkspaceHint(root, "test")], (f"task:{task.task_id}",)
            )
        _git(root, "merge", "--ff-only", "feature")
        merged = read_dashboard_workspace_detail(database, workspace, search_query="feature")
        assert merged.workspace.task_id == task.task_id
        assert merged.task_count == 1
        assert merged.task_search_results
        assert daemon.read_project_context_result(
            connection, [WorkspaceHint(root, "test")], (f"task:{task.task_id}",)
        ).items
        assert (
            daemon.read_workspace_task_status(connection, [WorkspaceHint(root, "test")]).task
            is not None
        )
    finally:
        connection.close()


def test_branch_filter_precedes_dashboard_history_count_limit_and_page(tmp_path: Path) -> None:
    root, database, connection, _project, workspace = _setup(tmp_path)
    try:
        visible = set()
        for number in range(3):
            task = task_start(connection, workspace, f"main {number}")
            visible.add(task.task_id)
            task_accept(connection, workspace, task.task_id, expected_revision=task.revision)
        _git(root, "checkout", "-b", "feature")
        (root / "source.py").write_text("answer = 99\n")
        _commit(root, "feature")
        for number in range(25):
            task = task_start(connection, workspace, f"feature {number}")
            task_accept(connection, workspace, task.task_id, expected_revision=task.revision)
        _git(root, "checkout", "main")
        page = read_dashboard_workspace_detail(database, workspace, page=2)
        assert page.task_count == 3
        assert page.page == 1
        assert {row.task.task_id for row in page.recent_tasks} == visible
        assert read_dashboard_home(database).task_count == 28
    finally:
        connection.close()


def test_missing_checkout_retains_relocation_controls_without_task_disclosure(
    tmp_path: Path,
) -> None:
    root, database, connection, project, workspace = _setup(tmp_path)
    try:
        task_start(connection, workspace, "Нельзя раскрывать без ветки")
        root.rename(tmp_path / "moved")
        detail = read_dashboard_workspace_detail(database, workspace, search_query="ветки")
        assert detail.workspace.live_error is not None
        assert detail.workspace.task_id is None
        assert detail.task_count == 0
        assert detail.recent_tasks == ()
        assert detail.task_search_results == ()
        html = render_workspace_page(detail, base_path="/")
        assert 'value="relocate_workspace"' in html
        assert "Нельзя раскрывать" not in html
        project_detail = read_dashboard_project_detail(database, project)
        assert project_detail.workspaces[0].task_id is None
        assert project_detail.workspaces[0].live_error is not None
    finally:
        connection.close()


def test_task_status_rejects_branch_switch_during_task_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _database, connection, _project, workspace = _setup(tmp_path)
    try:
        _feature_task(connection, root, workspace)
        original = get_relevant_task

        def switching(
            conn: sqlite3.Connection,
            workspace_id: str,
            *,
            applicability: WorkspaceApplicability | None = None,
        ) -> TaskRecord | None:
            result = original(conn, workspace_id, applicability=applicability)
            _git(root, "checkout", "main")
            return result

        monkeypatch.setattr(daemon, "get_relevant_task", switching)
        with pytest.raises(GitApplicabilityError):
            daemon.read_workspace_task_status(connection, [WorkspaceHint(root, "test")])
        assert not connection.in_transaction
    finally:
        connection.close()


def test_workspace_sse_refreshes_after_branch_only_switch_without_database_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.client
    import time
    from urllib.parse import urlencode, urlsplit

    import harness.dashboard as dashboard_module
    from harness.dashboard import DashboardServerManager

    root, database, connection, _project, workspace = _setup(tmp_path)
    manager = DashboardServerManager(database)
    client: http.client.HTTPConnection | None = None
    try:
        _feature_task(connection, root, workspace)
        monkeypatch.setattr(dashboard_module, "_DASHBOARD_SSE_HEARTBEAT_SECONDS", 0.05)
        monkeypatch.setattr(dashboard_module, "_DASHBOARD_SSE_POLL_SECONDS", 0.01)
        snapshot = _view_fingerprint(database, "workspace", workspace, None)
        url = urlsplit(manager.get_url())
        assert url.hostname is not None
        client = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        query = urlencode({"view": "workspace", "workspace_id": workspace, "snapshot": snapshot})
        client.request("GET", "/events?" + query)
        response = client.getresponse()
        assert response.status == 200
        while response.readline().strip() != b"event: ready":
            pass
        version = connection.execute("PRAGMA data_version").fetchone()
        _git(root, "checkout", "main")
        assert connection.execute("PRAGMA data_version").fetchone() == version
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if response.readline().strip() == b"event: refresh":
                break
        else:
            raise AssertionError("branch-only change did not refresh the Workspace view")
        assert read_dashboard_workspace_detail(database, workspace).task_count == 0
    finally:
        if client is not None:
            client.close()
        manager.close()
        connection.close()
