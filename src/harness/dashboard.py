from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import ClassVar, cast
from urllib.parse import parse_qs

from harness.git_workspace import (
    GitWorkspaceError,
    inspect_git_working_tree_status,
    inspect_git_workspace_runtime_identity,
)
from harness.registry import (
    RegistryError,
    WorkspaceRecord,
    get_project,
    get_workspace,
    list_workspaces,
)
from harness.storage import DatabaseError, connect_database
from harness.task_checkpoints import TaskCheckpointError, get_latest_task_checkpoint_status
from harness.task_workflow import task_accept, task_cancel, task_feedback
from harness.tasks import (
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    TaskWorkspaceConflictError,
    get_latest_task,
    get_relevant_task,
)

_DASHBOARD_HOST = "127.0.0.1"
_DASHBOARD_START_TIMEOUT_SECONDS = 2.0
_DASHBOARD_STOP_TIMEOUT_SECONDS = 2.0
_DASHBOARD_FORM_MAX_BYTES = 4096
_DASHBOARD_FORM_MAX_FIELDS = 5
_DASHBOARD_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DashboardError(RuntimeError):
    """Raised when the local dashboard cannot be started or rendered safely."""


@dataclass(frozen=True, slots=True)
class DashboardWorkspaceRow:
    """One bounded read-only Workspace row shown on the initial Projects overview."""

    project_id: str
    workspace_id: str
    workspace_root: Path
    visibility_mode: str
    task_id: str | None
    task_title: str | None
    task_state: str | None
    task_wait_reason: str | None
    task_revision: int | None
    last_activity: str | None
    next_step: str | None
    branch: str | None
    dirty_path_count: int | None
    indexed_file_count: int
    live_error: str | None


def read_dashboard_workspace_rows(database_path: Path) -> tuple[DashboardWorkspaceRow, ...]:
    """Read the bounded Projects overview from the same durable state owned by harnessd."""
    connection = connect_database(database_path)
    try:
        workspaces = list_workspaces(connection)
        return tuple(_read_workspace_row(connection, workspace) for workspace in workspaces)
    finally:
        connection.close()


def _read_workspace_row(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
) -> DashboardWorkspaceRow:
    connection.execute("BEGIN")
    try:
        if get_workspace(connection, workspace.workspace_id) != workspace:
            raise sqlite3.DatabaseError("workspace registry changed during dashboard read")
        project = get_project(connection, workspace.project_id)
        task = get_relevant_task(connection, workspace.workspace_id)
        if task is None:
            task = get_latest_task(connection, workspace.workspace_id)
        checkpoint = (
            get_latest_task_checkpoint_status(connection, task.task_id)
            if task is not None
            else None
        )
        indexed_file_count = _indexed_file_count(connection, workspace.workspace_id)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    branch: str | None = None
    dirty_path_count: int | None = None
    live_error: str | None = None
    try:
        before = inspect_git_workspace_runtime_identity(workspace.workspace_root)
        if (
            before.layout.workspace_root != workspace.workspace_root
            or before.layout.git_common_dir != workspace.git_common_dir
        ):
            raise GitWorkspaceError("registered Workspace Git identity changed")
        status = inspect_git_working_tree_status(workspace.workspace_root)
        after = inspect_git_workspace_runtime_identity(workspace.workspace_root)
        if after != before:
            raise GitWorkspaceError("Workspace Git identity changed during dashboard read")
        branch = status.branch
        dirty_path_count = status.dirty_path_count
    except GitWorkspaceError:
        live_error = "Git status unavailable"

    return DashboardWorkspaceRow(
        project_id=workspace.project_id,
        workspace_id=workspace.workspace_id,
        workspace_root=workspace.workspace_root,
        visibility_mode=project.visibility_mode.value,
        task_id=None if task is None else task.task_id,
        task_title=None if task is None else task.title,
        task_state=None if task is None else task.state.value,
        task_wait_reason=(
            None if task is None or task.wait_reason is None else task.wait_reason.value
        ),
        task_revision=None if task is None else task.revision,
        last_activity=None if task is None else task.updated_at,
        next_step=None if checkpoint is None else checkpoint.next_step,
        branch=branch,
        dirty_path_count=dirty_path_count,
        indexed_file_count=indexed_file_count,
        live_error=live_error,
    )


def _indexed_file_count(connection: sqlite3.Connection, workspace_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM indexed_files WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError("invalid indexed file count")
    return row[0]


def _display_task(row: DashboardWorkspaceRow) -> str:
    if row.task_id is None:
        return "—"
    assert row.task_revision is not None
    return f"{row.task_id} · r{row.task_revision}"


def _display_live_status(value: str | int | None, row: DashboardWorkspaceRow) -> str:
    if row.live_error is not None:
        return row.live_error
    if value is None:
        return "—"
    return str(value)


def _cell(value: str | int | None, *, css_class: str | None = None) -> str:
    text = "—" if value is None or value == "" else str(value)
    class_attribute = "" if css_class is None else f' class="{css_class}"'
    return f"<td{class_attribute}>{escape(text)}</td>"


def _hidden_input(name: str, value: str | int) -> str:
    return (
        f'<input type="hidden" name="{escape(name, quote=True)}" '
        f'value="{escape(str(value), quote=True)}">'
    )


def _task_action_fields(row: DashboardWorkspaceRow, action: str) -> str:
    assert row.task_id is not None
    assert row.task_revision is not None
    return (
        _hidden_input("action", action)
        + _hidden_input("workspace_id", row.workspace_id)
        + _hidden_input("task_id", row.task_id)
        + _hidden_input("expected_revision", row.task_revision)
    )


def _render_task_actions(row: DashboardWorkspaceRow) -> str:
    if row.task_id is None or row.task_revision is None:
        return "—"
    forms: list[str] = []
    if (
        row.task_state == TaskState.WAITING.value
        and row.task_wait_reason == TaskWaitReason.OPERATOR_REVIEW.value
    ):
        forms.append(
            '<div class="review-state">Ready for review</div>'
            '<form method="post" action="">'
            + _task_action_fields(row, "accept")
            + '<button type="submit">Accept</button></form>'
        )
        forms.append(
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(row, "feedback")
            + '<label>Feedback <textarea name="feedback" rows="3" maxlength="1024" '
            "required></textarea></label>" + '<button type="submit">Send feedback</button></form>'
        )
    if row.task_state in {TaskState.WORKING.value, TaskState.WAITING.value}:
        forms.append(
            '<form method="post" action="">'
            + _task_action_fields(row, "cancel")
            + '<button type="submit">Cancel</button></form>'
        )
    return "".join(forms) if forms else "—"


def render_projects_page(rows: tuple[DashboardWorkspaceRow, ...]) -> str:
    """Render the first bounded dashboard Projects overview with escaped persisted text."""
    if rows:
        body_rows = []
        for row in rows:
            live_class = "error" if row.live_error is not None else None
            body_rows.append(
                "<tr>"
                + _cell(row.project_id)
                + _cell(str(row.workspace_root))
                + _cell(row.task_title)
                + _cell(_display_task(row))
                + _cell(row.task_state if row.task_state is not None else "idle")
                + _cell(row.last_activity)
                + _cell(_display_live_status(row.branch, row), css_class=live_class)
                + _cell(_display_live_status(row.dirty_path_count, row), css_class=live_class)
                + _cell(row.indexed_file_count)
                + _cell(row.next_step)
                + f"<td>{_render_task_actions(row)}</td>"
                + "</tr>"
            )
        table_body = "".join(body_rows)
    else:
        table_body = '<tr><td colspan="11" class="empty">No registered Workspaces.</td></tr>'

    return (
        """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Harness · Projects</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 2rem; }
h1 { margin-bottom: .25rem; }
p { margin-top: 0; opacity: .75; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { border-bottom: 1px solid currentColor; padding: .55rem; text-align: left; vertical-align: top; }
th { white-space: nowrap; }
td { overflow-wrap: anywhere; }
.error { font-weight: 650; }
.empty { text-align: center; padding: 2rem; }
form { margin: 0 0 .5rem; }
.feedback-form label { display: grid; gap: .25rem; }
textarea { box-sizing: border-box; max-width: 28rem; width: 100%; }
.review-state { font-weight: 650; margin-bottom: .5rem; }
</style>
</head>
<body>
<h1>Harness Projects</h1>
<p>Workspace, Task, and human-review overview from the local Harness daemon.</p>
<table>
<thead><tr><th>Project</th><th>Workspace</th><th>Focus</th><th>Task</th><th>State</th><th>Last activity</th><th>Branch</th><th>Dirty</th><th>Index</th><th>Next</th><th>Actions</th></tr></thead>
<tbody>"""
        + table_body
        + """</tbody>
</table>
</body>
</html>
"""
    )


@dataclass(frozen=True, slots=True)
class DashboardActionRequest:
    """One validated capability-scoped human Task mutation from the dashboard UI."""

    action: str
    workspace_id: str
    task_id: str
    expected_revision: int
    feedback: str | None = None


def mutate_dashboard_task(database_path: Path, request: DashboardActionRequest) -> None:
    """Delegate one dashboard action to the authoritative Task domain workflow."""
    connection = connect_database(database_path)
    try:
        if request.action == "accept":
            task_accept(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
            )
            return
        if request.action == "feedback":
            assert request.feedback is not None
            task_feedback(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                feedback=request.feedback,
            )
            return
        if request.action == "cancel":
            task_cancel(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
            )
            return
        raise TaskValidationError("unsupported dashboard Task action")
    finally:
        connection.close()


def _parse_dashboard_action_form(payload: bytes) -> DashboardActionRequest:
    try:
        encoded = payload.decode("ascii")
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=_DASHBOARD_FORM_MAX_FIELDS,
        )
    except (UnicodeError, ValueError) as exc:
        raise TaskValidationError("dashboard action form is malformed") from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise TaskValidationError("dashboard action form fields must be singular")
    fields = {name: values[0] for name, values in parsed.items()}
    action = fields.get("action")
    expected = {"action", "workspace_id", "task_id", "expected_revision"}
    if action == "feedback":
        expected.add("feedback")
    if action not in {"accept", "feedback", "cancel"} or set(fields) != expected:
        raise TaskValidationError("dashboard action form does not match the expected schema")
    workspace_id = fields["workspace_id"]
    task_id = fields["task_id"]
    revision_text = fields["expected_revision"]
    if (
        not workspace_id
        or len(workspace_id) > 128
        or "\x00" in workspace_id
        or not task_id
        or len(task_id) > 128
        or "\x00" in task_id
        or not revision_text.isascii()
        or not revision_text.isdigit()
    ):
        raise TaskValidationError("dashboard action identity fields are invalid")
    expected_revision = int(revision_text)
    if expected_revision <= 0:
        raise TaskValidationError("dashboard action expected_revision must be positive")
    feedback = fields.get("feedback")
    return DashboardActionRequest(
        action=action,
        workspace_id=workspace_id,
        task_id=task_id,
        expected_revision=expected_revision,
        feedback=feedback,
    )


class _DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    database_path: ClassVar[Path]
    route_path: ClassVar[str]
    expected_host: ClassVar[str]
    expected_origin: ClassVar[str]

    def do_GET(self) -> None:
        if self.path != self.route_path or self.headers.get("Host") != self.expected_host:
            self._send_html(404, "")
            return
        try:
            rows = read_dashboard_workspace_rows(self.database_path)
        except (
            OSError,
            sqlite3.DatabaseError,
            DatabaseError,
            RegistryError,
            TaskError,
            TaskCheckpointError,
        ):
            self._send_html(
                503,
                "<!doctype html><title>Harness dashboard unavailable</title>"
                "<h1>Harness dashboard unavailable</h1>",
            )
            return
        self._send_html(200, render_projects_page(rows))

    def do_POST(self) -> None:
        if self.path != self.route_path:
            self._send_html(404, "")
            return
        if (
            self.headers.get("Host") != self.expected_host
            or self.headers.get("Origin") != self.expected_origin
        ):
            self._send_html(403, "")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_html(400, "")
            return
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self._send_html(415, "")
            return
        length_text = self.headers.get("Content-Length")
        if length_text is None or not length_text.isascii() or not length_text.isdigit():
            self._send_html(411, "")
            return
        content_length = int(length_text)
        if content_length <= 0 or content_length > _DASHBOARD_FORM_MAX_BYTES:
            self._send_html(413, "")
            return
        payload = self.rfile.read(content_length)
        if len(payload) != content_length:
            self._send_html(400, "")
            return
        try:
            request = _parse_dashboard_action_form(payload)
            mutate_dashboard_task(self.database_path, request)
        except TaskValidationError:
            self._send_html(400, "")
            return
        except (
            TaskNotFoundError,
            TaskConflictError,
            TaskRevisionConflictError,
            TaskWorkspaceConflictError,
            TaskTransitionError,
        ):
            self._send_html(409, "")
            return
        except (OSError, sqlite3.DatabaseError, DatabaseError):
            self._send_html(503, "")
            return
        self._send_redirect(self.route_path)

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        self._send_html(code, "")

    def _send_html(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._send_security_headers()
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _send_redirect(self, location: str) -> None:
        self.send_response_only(303)
        self.send_header("Content-Length", "0")
        self.send_header("Location", location)
        self._send_security_headers()
        self.end_headers()

    def _send_security_headers(self) -> None:
        for name, value in _DASHBOARD_RESPONSE_HEADERS.items():
            self.send_header(name, value)

    def log_message(self, _format: str, *args: object) -> None:
        del args


def _request_handler(database_path: Path, access_token: str) -> type[_DashboardRequestHandler]:
    class Handler(_DashboardRequestHandler):
        pass

    Handler.database_path = database_path
    Handler.route_path = f"/{access_token}/"
    return Handler


class DashboardServerManager:
    """Lazily own one daemon-lifetime loopback dashboard HTTP server."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._server: _DashboardHttpServer | None = None
        self._thread: Thread | None = None
        self._stop_event: Event | None = None
        self._started_event: Event | None = None
        self._url: str | None = None
        self._failure: BaseException | None = None

    def get_url(self) -> str:
        """Start the dashboard on first use and return its capability-bearing loopback URL."""
        if self._server is not None and self._thread is not None and self._thread.is_alive():
            assert self._url is not None
            return self._url
        self.close()

        access_token = secrets.token_urlsafe(32)
        stop_event = Event()
        started_event = Event()
        self._failure = None
        try:
            server = _DashboardHttpServer(
                (_DASHBOARD_HOST, 0),
                _request_handler(self._database_path, access_token),
            )
            server.timeout = 0.05
            port = server.server_address[1]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                server.server_close()
                raise DashboardError("dashboard loopback listener returned an invalid port")
            handler = cast(type[_DashboardRequestHandler], server.RequestHandlerClass)
            handler.expected_host = f"{_DASHBOARD_HOST}:{port}"
            handler.expected_origin = f"http://{_DASHBOARD_HOST}:{port}"
            thread = Thread(
                target=self._run_server,
                args=(server, stop_event, started_event),
                name="harness-dashboard",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._stop_event = stop_event
            self._started_event = started_event
            self._url = f"http://{_DASHBOARD_HOST}:{port}/{access_token}/"
            thread.start()
            self._wait_until_started(thread, started_event)
            return self._url
        except Exception as exc:
            try:
                self.close()
            except DashboardError as cleanup_error:
                raise DashboardError(
                    "dashboard startup failed and its local server could not be cleaned up"
                ) from cleanup_error
            if isinstance(exc, DashboardError):
                raise
            raise DashboardError("dashboard server could not be started") from exc

    def _run_server(
        self,
        server: _DashboardHttpServer,
        stop_event: Event,
        started_event: Event,
    ) -> None:
        started_event.set()
        try:
            while not stop_event.is_set():
                server.handle_request()
        except BaseException as exc:
            if not stop_event.is_set():
                self._failure = exc

    def _wait_until_started(self, thread: Thread, started_event: Event) -> None:
        deadline = monotonic() + _DASHBOARD_START_TIMEOUT_SECONDS
        while not started_event.is_set():
            if not thread.is_alive():
                raise DashboardError("dashboard server stopped during startup") from self._failure
            if monotonic() >= deadline:
                raise DashboardError("dashboard server did not become ready in time")
            sleep(0.01)
        if not thread.is_alive():
            raise DashboardError("dashboard server stopped during startup") from self._failure

    def close(self) -> None:
        """Stop only the dashboard server owned by this manager."""
        server = self._server
        thread = self._thread
        stop_event = self._stop_event
        self._server = None
        self._thread = None
        self._stop_event = None
        self._started_event = None
        self._url = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=_DASHBOARD_STOP_TIMEOUT_SECONDS)
        if server is not None:
            server.server_close()
        if thread is not None and thread.is_alive():
            raise DashboardError("dashboard server did not stop cleanly")
