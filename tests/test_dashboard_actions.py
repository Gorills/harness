from __future__ import annotations

import http.client
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import SimpleQueue
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from harness.dashboard import DashboardServerManager, _allows_dashboard_mutation
from harness.hidden_projection import CURSOR_HIDDEN_RULE_RELATIVE
from harness.index import scan_workspace
from harness.registry import (
    ProjectNotFoundError,
    VisibilityMode,
    create_project,
    get_project,
    get_workspace,
    register_workspace,
)
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import TaskEventType, list_task_events
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskRecord, TaskState, TaskWaitReason, get_task


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _database(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "tracked.txt")
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
        return root, database, workspace.workspace_id
    finally:
        connection.close()


def _write_host_profiles(database: Path, *profiles: str) -> None:
    path = database.parent / "host-integrations.json"
    path.write_text(
        json.dumps({"version": 1, "profiles": list(profiles)}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _review_task(database: Path, workspace_id: str, title: str = "Review me") -> TaskRecord:
    connection = connect_database(database)
    try:
        started = task_start(connection, workspace_id, title)
        return task_checkpoint(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=started.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Ready",
            next_step="Human review",
        ).task
    finally:
        connection.close()


def _post(
    url: str,
    fields: dict[str, str | int],
    *,
    origin: str | None,
    host: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urlsplit(url)
    assert parsed.hostname is not None and parsed.port is not None
    body = urlencode(fields).encode("ascii")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    if extra_headers:
        headers.update(extra_headers)
    connection.request("POST", parsed.path, body=body, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    result = response.status, {name: value for name, value in response.getheaders()}, payload
    connection.close()
    return result


def _assert_hardened(headers: dict[str, str]) -> None:
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "form-action 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "Server" not in headers
    assert "Date" not in headers


def test_dashboard_feedback_is_same_origin_cas_and_resumes_same_task(tmp_path: Path) -> None:
    _root, database, workspace_id = _database(tmp_path)
    waiting = _review_task(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        with urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
        assert ">Принять<" in body
        assert ">Замечание<" in body
        assert ">Отменить<" in body
        assert f'value="{waiting.revision}"' in body

        fields: dict[str, str | int] = {
            "action": "feedback",
            "workspace_id": workspace_id,
            "task_id": waiting.task_id,
            "expected_revision": waiting.revision,
            "feedback": "On mobile the spacing is still too large",
        }
        status, headers, payload = _post(url, fields, origin=None)
        assert status == 403
        assert "Действие не принято" in payload.decode("utf-8")
        _assert_hardened(headers)

        status, headers, payload = _post(url, fields, origin="https://example.invalid")
        assert status == 403
        assert "Действие не принято" in payload.decode("utf-8")
        _assert_hardened(headers)

        status, headers, payload = _post(
            url,
            fields,
            origin="https://example.invalid",
            extra_headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert status == 403
        _assert_hardened(headers)

        status, headers, payload = _post(
            url,
            fields,
            origin="null",
            extra_headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert status == 403
        _assert_hardened(headers)

        status, headers, _payload = _post(
            url,
            fields,
            origin=origin,
            host="example.invalid",
        )
        assert status == 403
        _assert_hardened(headers)

        connection = connect_database(database)
        try:
            assert get_task(connection, waiting.task_id) == waiting
        finally:
            connection.close()

        status, headers, payload = _post(url, fields, origin=origin)
        assert status == 303
        assert payload == b""
        assert headers["Location"] == parsed.path
        _assert_hardened(headers)

        connection = connect_database(database)
        try:
            resumed = get_task(connection, waiting.task_id)
            assert resumed.task_id == waiting.task_id
            assert resumed.state is TaskState.WORKING
            assert resumed.revision == waiting.revision + 1
            event = list_task_events(connection, waiting.task_id)[-1]
            assert event.event_type is TaskEventType.OPERATOR_FEEDBACK
            assert event.operator_feedback == "On mobile the spacing is still too large"
        finally:
            connection.close()

        stale_fields = dict(fields)
        stale_fields["action"] = "accept"
        stale_fields.pop("feedback")
        status, headers, _payload = _post(url, stale_fields, origin=origin)
        assert status == 409
        _assert_hardened(headers)
        connection = connect_database(database)
        try:
            assert get_task(connection, waiting.task_id) == resumed
        finally:
            connection.close()
    finally:
        manager.close()


def test_dashboard_relocates_workspace_without_losing_identity_or_files(tmp_path: Path) -> None:
    root, database, workspace_id = _database(tmp_path)
    moved = tmp_path / "moved-repo"
    invalidations: SimpleQueue[str] = SimpleQueue()
    manager = DashboardServerManager(database, workspace_invalidations=invalidations)
    try:
        base_url = manager.get_url()
        parsed = urlsplit(base_url)
        origin = f"http://127.0.0.1:{parsed.port}"
        workspace_url = base_url + f"workspaces/{quote(workspace_id, safe='')}/"
        status, headers, _payload = _post(
            workspace_url,
            {
                "action": "relocate_workspace",
                "workspace_id": "another-workspace",
                "new_path": str(root),
            },
            origin=origin,
        )
        assert status == 400
        _assert_hardened(headers)
        root.rename(moved)

        status, headers, payload = _post(
            workspace_url,
            {
                "action": "relocate_workspace",
                "workspace_id": workspace_id,
                "new_path": str(moved),
            },
            origin=origin,
        )

        assert status == 303
        assert payload == b""
        assert headers["Location"] == urlsplit(workspace_url).path
        _assert_hardened(headers)
        assert invalidations.get_nowait() == workspace_id
        connection = connect_database(database)
        try:
            workspace = get_workspace(connection, workspace_id)
            assert workspace.workspace_root == moved.resolve(strict=True)
            assert connection.execute(
                "SELECT COUNT(*) FROM indexed_files WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone() == (0,)
        finally:
            connection.close()
        assert (moved / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
        with urlopen(workspace_url, timeout=2) as response:
            body = response.read().decode("utf-8")
        assert str(moved) in body
        assert "Проект перенесён в другую папку" in body
        assert "harness scan" in body
    finally:
        manager.close()


def test_dashboard_project_deletion_requires_confirmation_and_preserves_files(
    tmp_path: Path,
) -> None:
    root, database, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        project_id = get_workspace(connection, workspace_id).project_id
    finally:
        connection.close()
    manager = DashboardServerManager(database)
    try:
        base_url = manager.get_url()
        parsed = urlsplit(base_url)
        origin = f"http://127.0.0.1:{parsed.port}"
        project_url = base_url + f"projects/{quote(project_id, safe='')}/"
        with urlopen(project_url, timeout=2) as response:
            body = response.read().decode("utf-8")
        assert "Удаление проекта" in body
        assert "Файлы на диске останутся" in body

        fields: dict[str, str | int] = {
            "action": "delete_project",
            "project_id": project_id,
            "confirmation": "УДАЛИТЬ",
        }
        status, headers, _payload = _post(base_url, fields, origin=origin)
        assert status == 400
        _assert_hardened(headers)

        fields["confirmation"] = "не удалять"
        status, headers, _payload = _post(project_url, fields, origin=origin)
        assert status == 400
        _assert_hardened(headers)
        connection = connect_database(database)
        try:
            assert get_project(connection, project_id).project_id == project_id
        finally:
            connection.close()

        fields["confirmation"] = "УДАЛИТЬ"
        status, headers, payload = _post(project_url, fields, origin=origin)
        assert status == 303
        assert payload == b""
        assert headers["Location"] == parsed.path
        _assert_hardened(headers)
        connection = connect_database(database)
        try:
            with pytest.raises(ProjectNotFoundError):
                get_project(connection, project_id)
        finally:
            connection.close()
        assert root.is_dir()
        assert (root / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
    finally:
        manager.close()


def test_dashboard_accept_and_cancel_persist_terminal_operator_events(tmp_path: Path) -> None:
    _root, database, workspace_id = _database(tmp_path)
    review = _review_task(database, workspace_id, title="Accept me")
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        accept: dict[str, str | int] = {
            "action": "accept",
            "workspace_id": workspace_id,
            "task_id": review.task_id,
            "expected_revision": review.revision,
        }
        status, headers, _payload = _post(url, accept, origin=origin)
        assert status == 303
        _assert_hardened(headers)
        connection = connect_database(database)
        try:
            accepted = get_task(connection, review.task_id)
            assert accepted.state is TaskState.COMPLETED
            assert (
                list_task_events(connection, review.task_id)[-1].event_type
                is TaskEventType.ACCEPTED
            )
            second = task_start(connection, workspace_id, "Cancel me")
        finally:
            connection.close()

        cancel: dict[str, str | int] = {
            "action": "cancel",
            "workspace_id": workspace_id,
            "task_id": second.task_id,
            "expected_revision": second.revision,
        }
        status, headers, _payload = _post(url, cancel, origin=origin)
        assert status == 303
        _assert_hardened(headers)
        connection = connect_database(database)
        try:
            cancelled = get_task(connection, second.task_id)
            assert cancelled.state is TaskState.CANCELLED
            assert (
                list_task_events(connection, second.task_id)[-1].event_type
                is TaskEventType.CANCELLED
            )
        finally:
            connection.close()
    finally:
        manager.close()


def test_dashboard_rejects_malformed_and_oversized_mutation_bodies(tmp_path: Path) -> None:
    _root, database, workspace_id = _database(tmp_path)
    waiting = _review_task(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        assert parsed.hostname is not None and parsed.port is not None
        origin = f"http://127.0.0.1:{parsed.port}"

        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "POST",
            parsed.path,
            body=b"action=feedback&bad-field",
            headers={
                "Origin": origin,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()

        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        oversized = b"x" * 8193
        connection.request(
            "POST",
            parsed.path,
            body=oversized,
            headers={
                "Origin": origin,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        connection.close()

        db = connect_database(database)
        try:
            assert get_task(db, waiting.task_id) == waiting
        finally:
            db.close()
    finally:
        manager.close()


def test_dashboard_operator_tracking_and_reopen_use_same_origin_revision_cas(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    connection = connect_database(database)
    try:
        task = task_start(connection, workspace_id, "Track release")
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"

        for action, field, value in (
            ("set_jira", "jira_url", "https://jira.example/browse/HAR-42"),
            ("set_operator_status", "operator_status", "deploy_test"),
            ("comment", "comment", "Ready for rollout"),
        ):
            fields: dict[str, str | int] = {
                "action": action,
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
                field: value,
            }
            status, headers, _payload = _post(url, fields, origin=origin)
            assert status == 303
            _assert_hardened(headers)
            connection = connect_database(database)
            try:
                task = get_task(connection, task.task_id)
            finally:
                connection.close()

        connection = connect_database(database)
        try:
            completed = task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.COMPLETED,
                summary="Done",
            ).task
        finally:
            connection.close()

        status, headers, _payload = _post(
            url,
            {
                "action": "reopen",
                "workspace_id": workspace_id,
                "task_id": completed.task_id,
                "expected_revision": completed.revision,
            },
            origin=origin,
        )
        assert status == 303
        _assert_hardened(headers)

        connection = connect_database(database)
        try:
            reopened = get_task(connection, completed.task_id)
            assert reopened.state is TaskState.WORKING
            assert reopened.jira_url == "https://jira.example/browse/HAR-42"
            assert reopened.operator_status is not None
            assert reopened.operator_status.value == "deploy_test"
            assert tuple(event.event_type for event in list_task_events(connection, task.task_id))[
                -5:
            ] == (
                TaskEventType.JIRA_LINK_UPDATED,
                TaskEventType.OPERATOR_STATUS_UPDATED,
                TaskEventType.OPERATOR_COMMENT,
                TaskEventType.CHECKPOINT,
                TaskEventType.REOPENED,
            )
        finally:
            connection.close()
    finally:
        manager.close()


def test_dashboard_concurrent_review_actions_allow_exactly_one_revision_winner(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    waiting = _review_task(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        assert parsed.port is not None
        origin = f"http://127.0.0.1:{parsed.port}"
        accept: dict[str, str | int] = {
            "action": "accept",
            "workspace_id": workspace_id,
            "task_id": waiting.task_id,
            "expected_revision": waiting.revision,
        }
        feedback: dict[str, str | int] = {
            "action": "feedback",
            "workspace_id": workspace_id,
            "task_id": waiting.task_id,
            "expected_revision": waiting.revision,
            "feedback": "Choose exactly one concurrent human decision",
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_post, url, accept, origin=origin),
                executor.submit(_post, url, feedback, origin=origin),
            ]
            results = [future.result() for future in futures]

        statuses = sorted(status for status, _headers, _payload in results)
        assert statuses == [303, 409]
        for _status, headers, _payload in results:
            _assert_hardened(headers)

        connection = connect_database(database)
        try:
            task = get_task(connection, waiting.task_id)
            assert task.revision == waiting.revision + 1
            assert task.state in {TaskState.WORKING, TaskState.COMPLETED}
            operator_events = [
                event
                for event in list_task_events(connection, waiting.task_id)
                if event.event_type in {TaskEventType.ACCEPTED, TaskEventType.OPERATOR_FEEDBACK}
            ]
            assert len(operator_events) == 1
            assert operator_events[0].task_revision == task.revision
        finally:
            connection.close()
    finally:
        manager.close()


def test_dashboard_visibility_toggle_projects_hidden_rules(tmp_path: Path) -> None:
    root, database, workspace_id = _database(tmp_path)
    _write_host_profiles(database, "cursor")
    connection = connect_database(database)
    try:
        workspace = get_workspace(connection, workspace_id)
        project_id = workspace.project_id
        gitignore_before = (
            (root / ".gitignore").read_bytes() if (root / ".gitignore").exists() else b""
        )
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        workspace_url = url + f"workspaces/{quote(workspace_id, safe='')}/"
        with urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
        assert ">Скрытый<" in body
        assert f'action="{parsed.path}"' in body
        assert "Cursor не блокирует git-команды агента" not in body
        with urlopen(workspace_url, timeout=2) as response:
            workspace_body = response.read().decode("utf-8")
        assert ">Скрытый<" in workspace_body
        assert f'action="{urlsplit(workspace_url).path}"' in workspace_body

        fields: dict[str, str | int] = {
            "action": "set_visibility",
            "project_id": project_id,
            "visibility_mode": "hidden",
        }
        status, headers, payload = _post(url, fields, origin=origin)
        assert status == 303
        assert payload == b""
        _assert_hardened(headers)

        connection = connect_database(database)
        try:
            assert get_project(connection, project_id).visibility_mode is VisibilityMode.HIDDEN
        finally:
            connection.close()
        assert (root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).is_file()
        gitignore_after = (
            (root / ".gitignore").read_bytes() if (root / ".gitignore").exists() else b""
        )
        assert gitignore_after == gitignore_before

        with urlopen(url, timeout=2) as response:
            hidden_home = response.read().decode("utf-8")
        assert ">Обычный<" in hidden_home
        assert "Cursor не блокирует git-команды агента" in hidden_home
        with urlopen(workspace_url, timeout=2) as response:
            hidden_workspace = response.read().decode("utf-8")
        assert ">Обычный<" in hidden_workspace
        assert "Cursor не блокирует git-команды агента" in hidden_workspace
    finally:
        manager.close()


def test_dashboard_visibility_posts_accept_browser_same_origin_variants(tmp_path: Path) -> None:
    _root, database, workspace_id = _database(tmp_path)
    _write_host_profiles(database, "cursor")
    connection = connect_database(database)
    try:
        project_id = get_workspace(connection, workspace_id).project_id
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        hidden: dict[str, str | int] = {
            "action": "set_visibility",
            "project_id": project_id,
            "visibility_mode": "hidden",
        }
        normal: dict[str, str | int] = {
            "action": "set_visibility",
            "project_id": project_id,
            "visibility_mode": "normal",
        }
        status, headers, payload = _post(url, hidden, origin=f"{origin}/")
        assert status == 303
        assert payload == b""
        _assert_hardened(headers)

        status, headers, payload = _post(
            url,
            normal,
            origin=None,
            extra_headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert status == 303
        _assert_hardened(headers)

        status, headers, payload = _post(
            url,
            hidden,
            origin="null",
            extra_headers={"Sec-Fetch-Site": "same-origin"},
        )
        assert status == 303
        _assert_hardened(headers)

        connection = connect_database(database)
        try:
            assert get_project(connection, project_id).visibility_mode is VisibilityMode.HIDDEN
        finally:
            connection.close()
    finally:
        manager.close()


def test_allows_dashboard_mutation_same_origin_rules() -> None:
    expected_host = "127.0.0.1:17373"
    expected_origin = "http://127.0.0.1:17373"

    def allowed(
        *,
        host: str | None,
        origin: str | None = None,
        sec_fetch_site: str | None = None,
    ) -> bool:
        return _allows_dashboard_mutation(
            host=host,
            origin=origin,
            sec_fetch_site=sec_fetch_site,
            expected_host=expected_host,
            expected_origin=expected_origin,
        )

    assert allowed(host=expected_host, origin=expected_origin)
    assert allowed(host=expected_host, origin=f"{expected_origin}/")
    assert allowed(host=expected_host, sec_fetch_site="same-origin")
    assert allowed(host=expected_host, origin="null", sec_fetch_site="same-origin")
    assert not allowed(host=expected_host)
    assert not allowed(
        host=expected_host,
        origin="https://evil.example",
        sec_fetch_site="same-origin",
    )
    assert not allowed(host=expected_host, origin="null", sec_fetch_site="cross-site")


def test_dashboard_visibility_collision_leaves_mode_unchanged(tmp_path: Path) -> None:
    root, database, workspace_id = _database(tmp_path)
    _write_host_profiles(database, "cursor")
    collision = root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()
    collision.parent.mkdir(parents=True)
    collision.write_text("# user rule\n", encoding="utf-8")
    connection = connect_database(database)
    try:
        project_id = get_workspace(connection, workspace_id).project_id
    finally:
        connection.close()

    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        fields: dict[str, str | int] = {
            "action": "set_visibility",
            "project_id": project_id,
            "visibility_mode": "hidden",
        }
        status, headers, _payload = _post(url, fields, origin=origin)
        assert status == 409
        _assert_hardened(headers)
        connection = connect_database(database)
        try:
            assert get_project(connection, project_id).visibility_mode is VisibilityMode.NORMAL
        finally:
            connection.close()
        assert collision.read_text(encoding="utf-8") == "# user rule\n"
    finally:
        manager.close()
