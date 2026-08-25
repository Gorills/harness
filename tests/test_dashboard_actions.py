from __future__ import annotations

import http.client
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

from harness.dashboard import DashboardServerManager
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import TaskEventType, list_task_events
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskState, TaskWaitReason, get_task


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


def _review_task(database: Path, workspace_id: str, title: str = "Review me"):
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
        assert "Ready for review" in body
        assert ">Accept<" in body
        assert ">Send feedback<" in body
        assert ">Cancel<" in body
        assert f'value="{waiting.revision}"' in body

        fields: dict[str, str | int] = {
            "action": "feedback",
            "workspace_id": workspace_id,
            "task_id": waiting.task_id,
            "expected_revision": waiting.revision,
            "feedback": "On mobile the spacing is still too large",
        }
        status, headers, _payload = _post(url, fields, origin=None)
        assert status == 403
        _assert_hardened(headers)

        status, headers, _payload = _post(url, fields, origin="https://example.invalid")
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


def test_dashboard_accept_and_cancel_persist_terminal_operator_events(tmp_path: Path) -> None:
    _root, database, workspace_id = _database(tmp_path)
    review = _review_task(database, workspace_id, title="Accept me")
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        accept = {
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

        cancel = {
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
        oversized = b"x" * 4097
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
        accept = {
            "action": "accept",
            "workspace_id": workspace_id,
            "task_id": waiting.task_id,
            "expected_revision": waiting.revision,
        }
        feedback = {
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
