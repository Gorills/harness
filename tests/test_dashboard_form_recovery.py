from __future__ import annotations

import http.client
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest
from test_dashboard_actions import _assert_hardened, _database, _post, _review_task

from harness import dashboard as dashboard_module
from harness.dashboard import DashboardServerManager
from harness.storage import connect_database
from harness.task_checkpoints import list_task_events
from harness.task_workflow import task_accept, task_checkpoint, task_resume
from harness.tasks import TaskState, TaskWaitReason, get_task
from harness.verification import VerificationDraft, VerificationStatus


class _RecoveryForms(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, str]] = []
        self.form: dict[str, str] | None = None
        self.textarea: str | None = None
        self.recovered: set[str] = set()
        self.readonly_text: list[str] = []
        self.readonly = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        name = fields.get("name")
        if tag == "form":
            self.form = {}
        if tag == "input" and name and self.form is not None:
            self.form[name] = fields.get("value") or ""
        if tag == "textarea":
            self.textarea = name
            self.readonly = "readonly" in fields
            if name and self.form is not None:
                self.form[name] = ""
        if name and fields.get("data-recovered-draft") == "true":
            self.recovered.add(name)

    def handle_data(self, data: str) -> None:
        if self.textarea and self.form is not None:
            self.form[self.textarea] += data
        if self.readonly:
            self.readonly_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.form is not None:
            self.forms.append(self.form)
            self.form = None
        if tag == "textarea":
            self.textarea = None
            self.readonly = False


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"http://127.0.0.1:{parsed.port}"


def test_stale_comment_preserves_draft_and_requires_explicit_fresh_revision_retry(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    connection = connect_database(database)
    try:
        resumed = task_resume(
            connection, workspace_id, task.task_id, expected_revision=task.revision
        )
        fresh = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=resumed.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="New checkpoint since the form was opened",
            next_step="Review the new checkpoint",
        ).task
    finally:
        connection.close()
    draft = '\nНе потерять <script>alert("draft")</script>\nВторая строка & кавычки'
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        url = base + f"tasks/{quote(task.task_id, safe='')}/"
        status, headers, body = _post(
            url,
            {
                "action": "comment",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
                "comment": draft,
            },
            origin=_origin(base),
        )
        assert status == 409
        _assert_hardened(headers)
        text = body.decode()
        assert 'role="alert"' in text
        assert "Данные изменились" in text
        assert "New checkpoint since the form was opened" in text
        assert f'class="nav-project-link" href="/workspaces/{workspace_id}/"' in text
        assert "Пока нет проектов" not in text
        assert text.count('data-state="manual"') == 2
        assert text.count("Обновление вручную") == 2
        assert "Подключаемся" not in text
        assert '<script>alert("draft")</script>' not in text
        parsed = _RecoveryForms(text)
        retry = next(form for form in parsed.forms if form.get("action") == "comment")
        assert retry["comment"] == draft
        assert retry["task_id"] == task.task_id
        assert retry["workspace_id"] == workspace_id
        assert retry["expected_revision"] == str(fresh.revision)
        assert "comment" in parsed.recovered
        connection = connect_database(database)
        try:
            assert get_task(connection, task.task_id).revision == fresh.revision
            count_before = len(list_task_events(connection, task.task_id))
        finally:
            connection.close()
        retry_fields: dict[str, str | int] = {**retry}
        status, _headers, body = _post(url, retry_fields, origin=_origin(base))
        assert status == 303
        assert body == b""
        connection = connect_database(database)
        try:
            assert get_task(connection, task.task_id).revision == fresh.revision + 1
            assert len(list_task_events(connection, task.task_id)) == count_before + 1
        finally:
            connection.close()
    finally:
        manager.close()


def test_invalid_jira_retains_editable_value_without_rendering_an_active_link(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        draft = 'javascript:alert("saved draft")'
        status, headers, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "set_jira",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
                "jira_url": draft,
            },
            origin=_origin(base),
        )
        assert status == 400
        _assert_hardened(headers)
        text = body.decode()
        assert 'role="alert"' in text
        parsed = _RecoveryForms(text)
        retry = next(form for form in parsed.forms if form.get("action") == "set_jira")
        assert retry["jira_url"] == draft
        assert "jira_url" in parsed.recovered
        assert 'href="javascript:' not in text
    finally:
        manager.close()


def test_unavailable_feedback_returns_readonly_draft_without_retargeting_action(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    connection = connect_database(database)
    try:
        resumed = task_resume(
            connection, workspace_id, task.task_id, expected_revision=task.revision
        )
        task_accept(connection, workspace_id, task.task_id, expected_revision=resumed.revision)
    finally:
        connection.close()
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        draft = "Сохранить замечание после изменения состояния"
        status, _headers, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "feedback",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
                "feedback": draft,
            },
            origin=_origin(base),
        )
        assert status == 409
        parsed = _RecoveryForms(body.decode())
        assert draft in "".join(parsed.readonly_text)
        assert not any(form.get("action") == "feedback" for form in parsed.forms)
    finally:
        manager.close()


def test_invalid_relocation_preserves_path_for_the_same_workspace(tmp_path: Path) -> None:
    root, database, workspace_id = _database(tmp_path)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        status, _headers, body = _post(
            base + f"workspaces/{workspace_id}/",
            {
                "action": "relocate_workspace",
                "workspace_id": workspace_id,
                "new_path": "relative/<folder>",
            },
            origin=_origin(base),
        )
        assert status == 400
        parsed = _RecoveryForms(body.decode())
        retry = next(form for form in parsed.forms if form.get("action") == "relocate_workspace")
        assert retry["new_path"] == "relative/<folder>"
        assert retry["workspace_id"] == workspace_id
        assert root.exists()
    finally:
        manager.close()


@pytest.mark.parametrize("origin", ["https://foreign.example", None])
def test_rejected_origin_never_reflects_submitted_draft(tmp_path: Path, origin: str | None) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        status, headers, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "comment",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
                "comment": "DO-NOT-REFLECT",
            },
            origin=origin,
        )
        assert status == 403
        _assert_hardened(headers)
        assert b"DO-NOT-REFLECT" not in body
        assert b"<form" not in body
    finally:
        manager.close()


def test_malformed_form_has_bounded_error_without_arbitrary_field_reflection(
    tmp_path: Path,
) -> None:
    _root, database, _workspace_id = _database(tmp_path)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        parsed = urlsplit(base)
        assert parsed.hostname is not None
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "POST",
            "/",
            body=b"action=comment&comment=DO-NOT-REFLECT&comment=duplicate",
            headers={"Origin": _origin(base), "Content-Type": "application/x-www-form-urlencoded"},
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 400
        assert b'role="alert"' in body
        assert b"DO-NOT-REFLECT" not in body
        assert len(body) < 16 * 1024
    finally:
        manager.close()


@pytest.mark.parametrize("wrong_identity", ["workspace", "task_page"])
def test_recovery_never_retargets_a_form_with_mismatched_identity(
    tmp_path: Path, wrong_identity: str
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id, title="TARGET-TITLE-MUST-NOT-APPEAR")
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        page_task_id = task.task_id if wrong_identity == "workspace" else "another-task"
        status, _headers, body = _post(
            base + f"tasks/{page_task_id}/",
            {
                "action": "comment",
                "workspace_id": "another-workspace"
                if wrong_identity == "workspace"
                else workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision - 1,
                "comment": "Keep my own draft",
            },
            origin=_origin(base),
        )
        assert status == 409
        text = body.decode()
        assert "TARGET-TITLE-MUST-NOT-APPEAR" not in text
        parsed = _RecoveryForms(text)
        assert not parsed.forms
        assert "Keep my own draft" in "".join(parsed.readonly_text)
        connection = connect_database(database)
        try:
            assert get_task(connection, task.task_id).revision == task.revision
        finally:
            connection.close()
    finally:
        manager.close()


def test_oversized_revision_integer_returns_400_without_reflecting_the_revision(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        huge_revision = "9" * 5000
        status, headers, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "comment",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": huge_revision,
                "comment": "Keep this text",
            },
            origin=_origin(base),
        )
        assert status == 400
        _assert_hardened(headers)
        assert huge_revision.encode() not in body
        forms = _RecoveryForms(body.decode()).forms
        assert any(form.get("comment") == "Keep this text" for form in forms)
        assert all(form.get("expected_revision") == str(task.revision) for form in forms)
        connection = connect_database(database)
        try:
            assert get_task(connection, task.task_id).revision == task.revision
        finally:
            connection.close()
    finally:
        manager.close()


def test_recovery_navigation_failure_does_not_claim_an_empty_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)

    def unavailable_rows(
        _database_path: Path,
    ) -> tuple[dashboard_module.DashboardWorkspaceRow, ...]:
        raise OSError("NAVIGATION-ERROR-MUST-NOT-APPEAR")

    monkeypatch.setattr(dashboard_module, "read_dashboard_workspace_rows", unavailable_rows)
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        status, _headers, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "set_jira",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
                "jira_url": "invalid-url",
            },
            origin=_origin(base),
        )
        assert status == 400
        text = body.decode()
        assert "Навигация временно недоступна" in text
        assert "Пока нет проектов" not in text
        assert "NAVIGATION-ERROR-MUST-NOT-APPEAR" not in text
        assert any(form.get("jira_url") == "invalid-url" for form in _RecoveryForms(text).forms)
    finally:
        manager.close()


def test_stale_accept_recovery_shows_failed_verification_before_fresh_action(
    tmp_path: Path,
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    connection = connect_database(database)
    try:
        resumed = task_resume(
            connection, workspace_id, task.task_id, expected_revision=task.revision
        )
        fresh = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=resumed.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="New report with a failed check",
            next_step="Review failed verification",
            verification=[
                VerificationDraft(
                    "Deployment smoke",
                    VerificationStatus.FAILED,
                    'Assertion failed: <unsafe>& "quoted"',
                )
            ],
        ).task
    finally:
        connection.close()
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        status, headers, body = _post(
            base + f"tasks/{task.task_id}/",
            {
                "action": "accept",
                "workspace_id": workspace_id,
                "task_id": task.task_id,
                "expected_revision": task.revision,
            },
            origin=_origin(base),
        )
        assert status == 409
        _assert_hardened(headers)
        text = body.decode()
        assert 'data-status="failed"' in text
        assert "Сообщено агентом" in text
        assert "Deployment smoke" in text
        assert "Assertion failed: &lt;unsafe&gt;&amp; &quot;quoted&quot;" in text
        assert text.index('class="panel verification-panel"') < text.index(
            'name="action" value="accept"'
        )
        accept = next(form for form in _RecoveryForms(text).forms if form.get("action") == "accept")
        assert accept["expected_revision"] == str(fresh.revision)
        connection = connect_database(database)
        try:
            assert get_task(connection, task.task_id) == fresh
        finally:
            connection.close()
    finally:
        manager.close()


@pytest.mark.parametrize("action", ["set_state", "set_deployment"])
def test_stale_state_and_deployment_preserve_recovered_draft_markers(
    tmp_path: Path, action: str
) -> None:
    _root, database, workspace_id = _database(tmp_path)
    task = _review_task(database, workspace_id)
    connection = connect_database(database)
    try:
        task_resume(connection, workspace_id, task.task_id, expected_revision=task.revision)
    finally:
        connection.close()
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url() + "tasks/" + task.task_id + "/"
        origin = f"http://{urlsplit(url).netloc}"
        fields: dict[str, str | int] = {
            "action": action,
            "workspace_id": workspace_id,
            "task_id": task.task_id,
            "expected_revision": task.revision,
        }
        if action == "set_state":
            fields.update(state="waiting", wait_reason="operator_input")
            expected = {"state", "wait_reason"}
        else:
            fields.update(deploy_prod="1")
            expected = {"deploy_test", "deploy_prod"}
        status, _headers, body = _post(url, fields, origin=origin)
        assert status == 409
        html = body.decode()
        assert expected <= _RecoveryForms(html).recovered
        if action == "set_deployment":
            assert 'name="deploy_test" value="1" data-recovered-draft="true"' in html
            assert 'name="deploy_prod" value="1" checked data-recovered-draft="true"' in html
        else:
            assert 'value="waiting" selected' in html
            assert 'value="operator_input" selected' in html
    finally:
        manager.close()
