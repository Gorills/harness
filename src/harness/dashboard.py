from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass, replace
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread
from time import monotonic, sleep
from typing import ClassVar, cast
from urllib.parse import parse_qs, quote, unquote_to_bytes, urlencode, urlsplit

from harness.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS
from harness.dashboard_i18n import (
    ACCEPT,
    ACTIONS,
    BRANCH,
    BRAND,
    BREADCRUMB_PROJECTS,
    CANCEL,
    CANCEL_TASK,
    CREATED,
    DETACHED_HEAD,
    DIRTY,
    DIRTY_PATHS,
    EM_DASH,
    EMPTY_PROJECT_WORKSPACES_HINT,
    EMPTY_PROJECT_WORKSPACES_TITLE,
    EMPTY_WORKSPACES_HINT,
    EMPTY_WORKSPACES_TITLE,
    FEEDBACK_LABEL,
    FEEDBACK_PLACEHOLDER,
    FEEDBACK_SUBMIT,
    FEEDBACK_SUMMARY,
    GIT_UNAVAILABLE,
    INDEX,
    INDEXED_PATHS,
    LIVE_CONNECTING,
    LIVE_REFRESH,
    METRIC_ACTIVE,
    METRIC_INDEX,
    METRIC_PROJECTS,
    METRIC_REVIEW,
    METRICS_LABEL,
    MODE,
    NAVIGATION,
    NEXT,
    NO_ACTIONS,
    NO_SEARCH_HITS_TITLE,
    NO_TASK,
    NO_TASKS_TITLE,
    PAGE_PROJECTS,
    PAGE_PROJECTS_LEAD,
    PROJECT,
    PROJECT_PREFIX,
    RECENT_TASKS,
    REVISION,
    SEARCH,
    SEARCH_LABEL,
    SEARCH_PLACEHOLDER,
    SEARCH_SECTION,
    SECTION_WORKSPACES,
    SKIP_TO_CONTENT,
    STACK_HINTS,
    STATE,
    TASK,
    TASK_FACTS,
    TASK_FOCUS,
    TIMELINE,
    UNAVAILABLE_HEADING,
    UNAVAILABLE_TITLE,
    UPDATED,
    VISIBILITY,
    WAIT_REASON,
    WORKSPACE,
    WORKSPACE_FALLBACK,
    WORKSPACE_STATE,
    document_title,
    event_count_label,
    event_label,
    match_kind_label,
    more_paths_label,
    omitted_events_label,
    project_crumb,
    task_state_label,
    visibility_label,
    wait_reason_label,
    workspace_count_label,
)
from harness.git_workspace import (
    GitWorkspaceError,
    inspect_git_working_tree_status,
    inspect_git_workspace_runtime_identity,
)
from harness.registry import (
    ProjectRecord,
    RegistryError,
    WorkspaceRecord,
    get_project,
    get_workspace,
    list_workspaces,
)
from harness.runtime_paths import DASHBOARD_HOST
from harness.search import IndexedPathSearchResult, SearchError, search_indexed_paths
from harness.storage import DatabaseError, connect_database
from harness.task_checkpoints import (
    TaskCheckpointError,
    TaskCheckpointRecord,
    TaskEventRecord,
    TaskEventType,
    get_latest_task_checkpoint_status,
    list_task_checkpoints,
    list_task_events,
)
from harness.task_workflow import task_accept, task_cancel, task_feedback
from harness.tasks import (
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    TaskWorkspaceConflictError,
    get_latest_task,
    get_relevant_task,
    get_task,
    get_task_stack_hints,
)

_DASHBOARD_URL_FILENAME = "dashboard.url"
_DASHBOARD_TOKEN_FILENAME = "dashboard.token"
_DASHBOARD_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_DASHBOARD_START_TIMEOUT_SECONDS = 2.0
_DASHBOARD_STOP_TIMEOUT_SECONDS = 2.0
_DASHBOARD_FORM_MAX_BYTES = 4096
_DASHBOARD_FORM_MAX_FIELDS = 5
_DASHBOARD_SEARCH_LIMIT = 24
_DASHBOARD_RECENT_TASK_LIMIT = 24
_DASHBOARD_TIMELINE_EVENT_LIMIT = 60
_DASHBOARD_CHANGED_PATH_LIMIT = 24
_DASHBOARD_SSE_POLL_SECONDS = 1.0
_DASHBOARD_SSE_HEARTBEAT_SECONDS = 10.0
_DASHBOARD_SSE_SESSION_SECONDS = 30.0
_DASHBOARD_SSE_MAX_CLIENTS = 8
_DASHBOARD_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class DashboardError(RuntimeError):
    """Raised when the local dashboard cannot be started or rendered safely."""


@dataclass(frozen=True, slots=True)
class DashboardGitBranch:
    """Durable Git branch recorded for one Task, not the live Workspace checkout."""

    captured: bool
    name: str | None


@dataclass(frozen=True, slots=True)
class DashboardWorkspaceRow:
    """One bounded Workspace summary rendered by the local Projects dashboard."""

    project_id: str
    workspace_id: str
    workspace_root: Path
    git_common_dir: Path
    visibility_mode: str
    task_id: str | None
    task_title: str | None
    task_state: str | None
    task_wait_reason: str | None
    task_revision: int | None
    last_activity: str | None
    next_step: str | None
    task_git_branch: DashboardGitBranch | None
    branch: str | None
    dirty_path_count: int | None
    indexed_file_count: int
    live_error: str | None


@dataclass(frozen=True, slots=True)
class DashboardTaskRow:
    """One recent Task plus the durable Git branch recorded for that Task."""

    task: TaskRecord
    git_branch: DashboardGitBranch


@dataclass(frozen=True, slots=True)
class DashboardProjectDetail:
    """One Project plus all of its registered Workspace summaries."""

    project: ProjectRecord
    workspaces: tuple[DashboardWorkspaceRow, ...]


@dataclass(frozen=True, slots=True)
class DashboardWorkspaceDetail:
    """One Workspace, recent durable Tasks, and optional bounded indexed-path search."""

    workspace: DashboardWorkspaceRow
    recent_tasks: tuple[DashboardTaskRow, ...]
    search_query: str | None
    search_results: tuple[IndexedPathSearchResult, ...]


@dataclass(frozen=True, slots=True)
class DashboardTaskDetail:
    """One durable Task timeline together with its Workspace live summary."""

    workspace: DashboardWorkspaceRow
    task: TaskRecord
    git_branch: DashboardGitBranch
    baseline_git_branch: DashboardGitBranch
    stack_hints: tuple[str, ...]
    checkpoints: tuple[TaskCheckpointRecord, ...]
    events: tuple[TaskEventRecord, ...]
    event_count: int


@dataclass(frozen=True, slots=True)
class DashboardActionRequest:
    """One validated capability-scoped human Task mutation from the dashboard UI."""

    action: str
    workspace_id: str
    task_id: str
    expected_revision: int
    feedback: str | None = None


@dataclass(frozen=True, slots=True)
class _DashboardPageRequest:
    kind: str
    identity: str | None
    search_query: str | None
    redirect_target: str


def read_dashboard_workspace_rows(database_path: Path) -> tuple[DashboardWorkspaceRow, ...]:
    """Read a consistent persisted Projects overview plus fail-closed live Git summaries."""
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN")
        try:
            workspaces = list_workspaces(connection)
            rows = tuple(_read_workspace_row_persisted(connection, item) for item in workspaces)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return tuple(_with_live_workspace_status(row) for row in rows)


def read_dashboard_project_detail(
    database_path: Path,
    project_id: str,
) -> DashboardProjectDetail:
    """Read one Project and its Workspace summaries from the daemon-owned database."""
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN")
        try:
            project = get_project(connection, project_id)
            workspaces = list_workspaces(connection, project_id=project_id)
            rows = tuple(_read_workspace_row_persisted(connection, item) for item in workspaces)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return DashboardProjectDetail(
        project=project,
        workspaces=tuple(_with_live_workspace_status(row) for row in rows),
    )


def read_dashboard_workspace_detail(
    database_path: Path,
    workspace_id: str,
    *,
    search_query: str | None = None,
) -> DashboardWorkspaceDetail:
    """Read one Workspace detail page with bounded Task history and optional path search."""
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN")
        try:
            workspace = get_workspace(connection, workspace_id)
            row = _read_workspace_row_persisted(connection, workspace)
            task_ids = connection.execute(
                """
                SELECT id
                FROM tasks
                WHERE workspace_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (workspace_id, _DASHBOARD_RECENT_TASK_LIMIT),
            ).fetchall()
            loaded_tasks = tuple(get_task(connection, task_id[0]) for task_id in task_ids)
            recorded_branches = _read_recorded_git_branches(
                connection,
                tuple(task.task_id for task in loaded_tasks),
            )
            recent_tasks = tuple(
                DashboardTaskRow(task=task, git_branch=recorded_branches[task.task_id])
                for task in loaded_tasks
            )
            results = (
                ()
                if search_query is None
                else search_indexed_paths(
                    connection,
                    workspace_id,
                    search_query,
                    limit=_DASHBOARD_SEARCH_LIMIT,
                )
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return DashboardWorkspaceDetail(
        workspace=_with_live_workspace_status(row),
        recent_tasks=recent_tasks,
        search_query=search_query,
        search_results=results,
    )


def read_dashboard_task_detail(database_path: Path, task_id: str) -> DashboardTaskDetail:
    """Read one Task's immutable timeline and the live summary for its owning Workspace."""
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN")
        try:
            task = get_task(connection, task_id)
            workspace = get_workspace(connection, task.workspace_id)
            row = _read_workspace_row_persisted(connection, workspace)
            stack_hints = get_task_stack_hints(connection, task_id)
            baseline_git_branch = _read_task_baseline_branch(connection, task_id)
            checkpoints = list_task_checkpoints(
                connection,
                task_id,
                limit=_DASHBOARD_TIMELINE_EVENT_LIMIT,
            )
            events = list_task_events(
                connection,
                task_id,
                limit=_DASHBOARD_TIMELINE_EVENT_LIMIT,
            )
            count_row = connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if (
                count_row is None
                or isinstance(count_row[0], bool)
                or not isinstance(count_row[0], int)
                or count_row[0] < len(events)
            ):
                raise sqlite3.DatabaseError("invalid dashboard Task event count")
            event_count = count_row[0]
            git_branch = (
                DashboardGitBranch(captured=True, name=checkpoints[-1].current_branch)
                if checkpoints
                else baseline_git_branch
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return DashboardTaskDetail(
        workspace=_with_live_workspace_status(row),
        task=task,
        git_branch=git_branch,
        baseline_git_branch=baseline_git_branch,
        stack_hints=stack_hints,
        checkpoints=checkpoints,
        events=events,
        event_count=event_count,
    )


def _read_workspace_row_persisted(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
) -> DashboardWorkspaceRow:
    if get_workspace(connection, workspace.workspace_id) != workspace:
        raise sqlite3.DatabaseError("workspace registry changed during dashboard read")
    project = get_project(connection, workspace.project_id)
    task = get_relevant_task(connection, workspace.workspace_id)
    if task is None:
        task = get_latest_task(connection, workspace.workspace_id)
    checkpoint = (
        get_latest_task_checkpoint_status(connection, task.task_id) if task is not None else None
    )
    return DashboardWorkspaceRow(
        project_id=workspace.project_id,
        workspace_id=workspace.workspace_id,
        workspace_root=workspace.workspace_root,
        git_common_dir=workspace.git_common_dir,
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
        task_git_branch=(
            None
            if task is None
            else _read_recorded_git_branches(connection, (task.task_id,))[task.task_id]
        ),
        branch=None,
        dirty_path_count=None,
        indexed_file_count=_indexed_file_count(connection, workspace.workspace_id),
        live_error=None,
    )


def _with_live_workspace_status(row: DashboardWorkspaceRow) -> DashboardWorkspaceRow:
    branch: str | None = None
    dirty_path_count: int | None = None
    live_error: str | None = None
    try:
        before = inspect_git_workspace_runtime_identity(row.workspace_root)
        workspace = before.layout
        if (
            workspace.workspace_root != row.workspace_root
            or workspace.git_common_dir != row.git_common_dir
        ):
            raise GitWorkspaceError("registered Workspace Git identity changed")
        status = inspect_git_working_tree_status(row.workspace_root)
        after = inspect_git_workspace_runtime_identity(row.workspace_root)
        if after != before:
            raise GitWorkspaceError("Workspace Git identity changed during dashboard read")
        branch = status.branch
        dirty_path_count = status.dirty_path_count
    except GitWorkspaceError:
        live_error = "Git status unavailable"
    return replace(
        row,
        branch=branch,
        dirty_path_count=dirty_path_count,
        live_error=live_error,
    )


def _read_task_baseline_branch(
    connection: sqlite3.Connection,
    task_id: str,
) -> DashboardGitBranch:
    row = connection.execute(
        "SELECT branch FROM task_baselines WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return DashboardGitBranch(captured=False, name=None)
    return _validated_persisted_branch(row[0], captured=True)


def _read_recorded_git_branches(
    connection: sqlite3.Connection,
    task_ids: tuple[str, ...],
) -> dict[str, DashboardGitBranch]:
    if not task_ids:
        return {}
    placeholders = ",".join("?" * len(task_ids))
    rows = connection.execute(
        f"""
        SELECT
            tasks.id,
            CASE WHEN latest.id IS NULL THEN 0 ELSE 1 END,
            latest.current_branch,
            CASE WHEN baseline.task_id IS NULL THEN 0 ELSE 1 END,
            baseline.branch
        FROM tasks
        LEFT JOIN task_baselines AS baseline
            ON baseline.task_id = tasks.id
        LEFT JOIN (
            SELECT checkpoints.task_id, checkpoints.id, checkpoints.current_branch
            FROM task_checkpoints AS checkpoints
            INNER JOIN (
                SELECT task_id, MAX(task_revision) AS task_revision
                FROM task_checkpoints
                WHERE task_id IN ({placeholders})
                GROUP BY task_id
            ) AS newest
                ON newest.task_id = checkpoints.task_id
               AND newest.task_revision = checkpoints.task_revision
        ) AS latest
            ON latest.task_id = tasks.id
        WHERE tasks.id IN ({placeholders})
        """,
        (*task_ids, *task_ids),
    ).fetchall()
    found = {row[0] for row in rows}
    if found != set(task_ids):
        raise sqlite3.DatabaseError("dashboard Task git-branch query missed a Task")
    recorded: dict[str, DashboardGitBranch] = {}
    for task_id, has_checkpoint, checkpoint_branch, has_baseline, baseline_branch in rows:
        if not isinstance(task_id, str) or not task_id:
            raise sqlite3.DatabaseError("dashboard Task git-branch query returned an invalid id")
        if isinstance(has_checkpoint, bool) or has_checkpoint not in {0, 1}:
            raise sqlite3.DatabaseError("dashboard Task checkpoint capture flag is invalid")
        if isinstance(has_baseline, bool) or has_baseline not in {0, 1}:
            raise sqlite3.DatabaseError("dashboard Task baseline capture flag is invalid")
        if has_checkpoint == 1:
            recorded[task_id] = _validated_persisted_branch(checkpoint_branch, captured=True)
        elif has_baseline == 1:
            recorded[task_id] = _validated_persisted_branch(baseline_branch, captured=True)
        else:
            recorded[task_id] = DashboardGitBranch(captured=False, name=None)
    return recorded


def _validated_persisted_branch(value: object, *, captured: bool) -> DashboardGitBranch:
    if value is None:
        return DashboardGitBranch(captured=captured, name=None)
    if not isinstance(value, str) or not value:
        raise sqlite3.DatabaseError("dashboard persisted Git branch is invalid")
    return DashboardGitBranch(captured=captured, name=value)


def _indexed_file_count(connection: sqlite3.Connection, workspace_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM indexed_files WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError("invalid indexed file count")
    return row[0]


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
    return DashboardActionRequest(
        action=action,
        workspace_id=workspace_id,
        task_id=task_id,
        expected_revision=expected_revision,
        feedback=fields.get("feedback"),
    )


def _display_task(row: DashboardWorkspaceRow) -> str:
    if row.task_id is None:
        return EM_DASH
    assert row.task_revision is not None
    return f"{row.task_id} · r{row.task_revision}"


def _display_live_status(value: str | int | None, row: DashboardWorkspaceRow) -> str:
    if row.live_error is not None:
        return GIT_UNAVAILABLE
    return EM_DASH if value is None else str(value)


def _display_recorded_branch(branch: DashboardGitBranch) -> str:
    if not branch.captured:
        return EM_DASH
    if branch.name is None:
        return DETACHED_HEAD
    return branch.name


def _render_task_git_branch(branch: DashboardGitBranch) -> str:
    return (
        f'<p class="task-git-branch"><span>{escape(BRANCH)}</span> '
        f'<strong class="mono">{escape(_display_recorded_branch(branch))}</strong></p>'
    )


def _render_timeline_branch(branch: DashboardGitBranch) -> str:
    return (
        f'<div class="timeline-branch"><strong>{escape(BRANCH)}</strong> '
        f'<span class="mono">{escape(_display_recorded_branch(branch))}</span></div>'
    )


def _hidden_input(name: str, value: str | int) -> str:
    return (
        f'<input type="hidden" name="{escape(name, quote=True)}" '
        f'value="{escape(str(value), quote=True)}">'
    )


def _task_action_fields(
    workspace_id: str,
    task_id: str,
    revision: int,
    action: str,
) -> str:
    return (
        _hidden_input("action", action)
        + _hidden_input("workspace_id", workspace_id)
        + _hidden_input("task_id", task_id)
        + _hidden_input("expected_revision", revision)
    )


def _render_task_actions(
    *,
    workspace_id: str,
    task_id: str,
    state: str,
    wait_reason: str | None,
    revision: int,
) -> str:
    forms: list[str] = []
    if state == TaskState.WAITING.value and wait_reason == TaskWaitReason.OPERATOR_REVIEW.value:
        forms.append(
            '<div class="action-row"><form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "accept")
            + f'<button class="btn btn-primary" type="submit">{escape(ACCEPT)}</button></form>'
            '<form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "cancel")
            + f'<button class="btn btn-danger" type="submit">{escape(CANCEL)}</button></form></div>'
            f'<details class="feedback-disclosure"><summary>{escape(FEEDBACK_SUMMARY)}</summary>'
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "feedback")
            + f'<label for="feedback-{escape(task_id, quote=True)}">{escape(FEEDBACK_LABEL)}</label>'
            f'<textarea id="feedback-{escape(task_id, quote=True)}" name="feedback" rows="4" maxlength="1024" '
            f'required placeholder="{escape(FEEDBACK_PLACEHOLDER, quote=True)}"></textarea>'
            f'<button class="btn" type="submit">{escape(FEEDBACK_SUBMIT)}</button></form></details>'
        )
    elif state in {TaskState.WORKING.value, TaskState.WAITING.value}:
        forms.append(
            '<div class="action-row"><form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "cancel")
            + f'<button class="btn btn-danger" type="submit">{escape(CANCEL_TASK)}</button></form></div>'
        )
    return '<div class="action-panel">' + "".join(forms) + "</div>" if forms else ""


def _state_pill(state: str | None, wait_reason: str | None = None) -> str:
    label = task_state_label(state, wait_reason)
    if state is None:
        return f'<span class="pill pill-idle">{escape(label)}</span>'
    if state == TaskState.WAITING.value and wait_reason == TaskWaitReason.OPERATOR_REVIEW.value:
        return f'<span class="pill pill-review">{escape(label)}</span>'
    css = {
        TaskState.WORKING.value: "pill-working",
        TaskState.WAITING.value: "pill-waiting",
        TaskState.COMPLETED.value: "pill-completed",
        TaskState.CANCELLED.value: "pill-cancelled",
    }.get(state, "pill-idle")
    return f'<span class="pill {css}">{escape(label)}</span>'


def _url(base_path: str, kind: str, identity: str | None = None) -> str:
    if kind == "projects":
        return base_path
    assert identity is not None
    return f"{base_path}{kind}/{quote(identity, safe='')}/"


def _snapshot_fingerprint(value: object) -> str:
    return sha256(repr(value).encode("utf-8")).hexdigest()


def _events_url(
    base_path: str,
    *,
    view: str,
    snapshot: str,
    identity: str | None = None,
    search_query: str | None = None,
) -> str:
    params: list[tuple[str, str]] = [("view", view), ("snapshot", snapshot)]
    if identity is not None:
        params.append((f"{view}_id", identity))
    if search_query is not None:
        params.append(("q", search_query))
    return f"{base_path}events?{urlencode(params)}"


def _render_shell(
    *,
    base_path: str,
    page_title: str,
    breadcrumbs: tuple[tuple[str, str | None], ...],
    events_url: str,
    content: str,
) -> str:
    breadcrumb_html: list[str] = []
    for index, (label, href) in enumerate(breadcrumbs):
        if index:
            breadcrumb_html.append('<span class="sep">/</span>')
        if href is None:
            breadcrumb_html.append(f"<span>{escape(label)}</span>")
        else:
            breadcrumb_html.append(f'<a href="{escape(href, quote=True)}">{escape(label)}</a>')
    css_url = f"{base_path}assets/dashboard.css"
    js_url = f"{base_path}assets/dashboard.js"
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(page_title)}</title>"
        f'<link rel="stylesheet" href="{escape(css_url, quote=True)}">'
        "</head>"
        f'<body data-events-url="{escape(events_url, quote=True)}">'
        f'<a class="skip-link" href="#main">{escape(SKIP_TO_CONTENT)}</a>'
        '<div class="shell"><header class="topbar">'
        f'<a class="brand" href="{escape(base_path, quote=True)}">'
        f'<span class="brand-mark" aria-hidden="true">H</span><span>{escape(BRAND)}</span></a>'
        '<div class="topbar-meta">'
        '<span class="live-indicator" data-live-indicator data-state="reconnecting">'
        '<span class="live-dot" aria-hidden="true"></span>'
        f'<span class="live-copy" data-live-copy>{escape(LIVE_CONNECTING)}</span>'
        f'<button class="update-link" type="button" data-refresh-now="true">{escape(LIVE_REFRESH)}</button>'
        "</span></div></header>"
        f'<nav class="breadcrumbs" aria-label="{escape(NAVIGATION, quote=True)}">'
        f"{''.join(breadcrumb_html)}</nav>"
        f'<main id="main">{content}</main></div>'
        f'<script defer src="{escape(js_url, quote=True)}"></script>'
        "</body></html>"
    )


def _render_metrics(rows: tuple[DashboardWorkspaceRow, ...]) -> str:
    project_count = len({row.project_id for row in rows})
    active_count = sum(
        row.task_state in {TaskState.WORKING.value, TaskState.WAITING.value} for row in rows
    )
    review_count = sum(
        row.task_state == TaskState.WAITING.value
        and row.task_wait_reason == TaskWaitReason.OPERATOR_REVIEW.value
        for row in rows
    )
    indexed_count = sum(row.indexed_file_count for row in rows)
    metrics = (
        (METRIC_PROJECTS, project_count),
        (METRIC_ACTIVE, active_count),
        (METRIC_REVIEW, review_count),
        (METRIC_INDEX, indexed_count),
    )
    return (
        f'<section class="metrics" aria-label="{escape(METRICS_LABEL, quote=True)}">'
        + "".join(
            '<div class="metric"><span class="metric-label">'
            + escape(label)
            + '</span><strong class="metric-value">'
            + escape(str(value))
            + "</strong></div>"
            for label, value in metrics
        )
        + "</section>"
    )


def _render_workspace_card(row: DashboardWorkspaceRow, base_path: str) -> str:
    workspace_url = _url(base_path, "workspaces", row.workspace_id)
    project_url = _url(base_path, "projects", row.project_id)
    task_link = ""
    if row.task_id is None:
        task_title = NO_TASK
    else:
        task_url = _url(base_path, "tasks", row.task_id)
        task_link = (
            f'<a href="{escape(task_url, quote=True)}">{escape(row.task_title or row.task_id)}</a>'
        )
        task_title = row.task_title or row.task_id
    focus = escape(task_title) if row.task_id is None else task_link
    task_branch = (
        "" if row.task_git_branch is None else _render_task_git_branch(row.task_git_branch)
    )
    next_step = "" if row.next_step is None else f'<p class="next-step">{escape(row.next_step)}</p>'
    live_branch = _display_live_status(row.branch, row)
    live_dirty = _display_live_status(row.dirty_path_count, row)
    actions = ""
    if row.task_id is not None and row.task_revision is not None and row.task_state is not None:
        actions = _render_task_actions(
            workspace_id=row.workspace_id,
            task_id=row.task_id,
            state=row.task_state,
            wait_reason=row.task_wait_reason,
            revision=row.task_revision,
        )
    return (
        '<article class="workspace-card"><div class="card-main">'
        '<div class="card-kicker">'
        f'<a class="project-link" href="{escape(project_url, quote=True)}">'
        f"{escape(PROJECT_PREFIX)} {escape(row.project_id[:8])}</a>"
        f"{_state_pill(row.task_state, row.task_wait_reason)}</div>"
        f'<h2 class="workspace-name"><a href="{escape(workspace_url, quote=True)}">'
        f"{escape(row.workspace_root.name or str(row.workspace_root))}</a></h2>"
        f'<p class="workspace-path">{escape(str(row.workspace_root))}</p>'
        f'<div class="task-focus"><div class="task-focus-label">{escape(TASK_FOCUS)}</div>'
        f'<p class="task-focus-title">{focus}</p>{task_branch}{next_step}</div></div>'
        '<aside class="card-side"><div class="mini-stats">'
        f'<div class="mini-stat"><span>{escape(BRANCH)}</span><strong>{escape(live_branch)}</strong></div>'
        f'<div class="mini-stat"><span>{escape(DIRTY)}</span><strong>{escape(live_dirty)}</strong></div>'
        f'<div class="mini-stat"><span>{escape(INDEX)}</span><strong>{row.indexed_file_count}</strong></div>'
        f'<div class="mini-stat"><span>{escape(MODE)}</span>'
        f"<strong>{escape(visibility_label(row.visibility_mode))}</strong></div>"
        f"</div>{actions}</aside></article>"
    )


def render_projects_page(rows: tuple[DashboardWorkspaceRow, ...], *, base_path: str = "/") -> str:
    """Render the capability-scoped Projects overview with navigation and live refresh hints."""
    if rows:
        workspace_html = (
            '<div class="workspace-grid">'
            + "".join(_render_workspace_card(row, base_path) for row in rows)
            + "</div>"
        )
    else:
        workspace_html = (
            f'<div class="empty-state"><strong>{escape(EMPTY_WORKSPACES_TITLE)}</strong>'
            f"<span>{escape(EMPTY_WORKSPACES_HINT)}</span></div>"
        )
    content = (
        '<section class="hero compact"><div>'
        f"<h1>{escape(PAGE_PROJECTS)}</h1>"
        f'<p class="hero-copy">{escape(PAGE_PROJECTS_LEAD)}</p></div></section>'
        + _render_metrics(rows)
        + '<section class="section"><div class="section-head"><div>'
        f'<h2 class="section-title">{escape(SECTION_WORKSPACES)}</h2></div></div>'
        + workspace_html
        + "</section>"
    )
    return _render_shell(
        base_path=base_path,
        page_title=document_title(PAGE_PROJECTS),
        breadcrumbs=((BREADCRUMB_PROJECTS, None),),
        events_url=_events_url(
            base_path,
            view="projects",
            snapshot=_snapshot_fingerprint(rows),
        ),
        content=content,
    )


def render_project_page(detail: DashboardProjectDetail, *, base_path: str) -> str:
    rows = detail.workspaces
    workspace_html = (
        '<div class="workspace-grid">'
        + "".join(_render_workspace_card(row, base_path) for row in rows)
        + "</div>"
        if rows
        else (
            f'<div class="empty-state"><strong>{escape(EMPTY_PROJECT_WORKSPACES_TITLE)}</strong>'
            f"<span>{escape(EMPTY_PROJECT_WORKSPACES_HINT)}</span></div>"
        )
    )
    content = (
        '<section class="hero compact"><div>'
        f"<h1>{escape(detail.project.project_id[:12])}</h1></div>"
        '<div class="hero-aside">'
        f'<div class="identity-line">{escape(detail.project.project_id)}</div>'
        f'<div class="identity-line">{escape(visibility_label(detail.project.visibility_mode.value))}</div>'
        "</div></section>"
        + _render_metrics(rows)
        + '<section class="section"><div class="section-head"><div>'
        f'<h2 class="section-title">{escape(workspace_count_label(len(rows)))}</h2>'
        "</div></div>" + workspace_html + "</section>"
    )
    project_id = detail.project.project_id
    return _render_shell(
        base_path=base_path,
        page_title=document_title(project_crumb(project_id)),
        breadcrumbs=((BREADCRUMB_PROJECTS, base_path), (project_crumb(project_id), None)),
        events_url=_events_url(
            base_path,
            view="project",
            identity=project_id,
            snapshot=_snapshot_fingerprint(detail),
        ),
        content=content,
    )


def _render_recent_tasks(tasks: tuple[DashboardTaskRow, ...], base_path: str) -> str:
    if not tasks:
        return f'<div class="empty-state"><strong>{escape(NO_TASKS_TITLE)}</strong></div>'
    parts = ['<div class="task-list">']
    for row in tasks:
        task = row.task
        task_url = _url(base_path, "tasks", task.task_id)
        wait_reason = None if task.wait_reason is None else task.wait_reason.value
        parts.append(
            '<article class="task-row"><div>'
            f'<p class="task-row-title"><a href="{escape(task_url, quote=True)}">{escape(task.title)}</a></p>'
            '<div class="task-row-meta">'
            f'<span class="mono">{escape(task.task_id[:10])}</span>'
            f"<span>{escape(REVISION)} {task.revision}</span>"
            f'<span>{escape(BRANCH)} <span class="mono">'
            f"{escape(_display_recorded_branch(row.git_branch))}</span></span>"
            f"<span>{escape(task.updated_at)}</span></div></div>"
            f"{_state_pill(task.state.value, wait_reason)}</article>"
        )
    parts.append("</div>")
    return "".join(parts)


def _render_search(detail: DashboardWorkspaceDetail) -> str:
    query = detail.search_query or ""
    result_html = ""
    if detail.search_query is not None:
        if detail.search_results:
            hits = []
            for hit in detail.search_results:
                hits.append(
                    '<div class="search-hit"><div class="search-hit-path">'
                    + escape(hit.relative_path)
                    + '</div><div class="search-hit-meta">'
                    + escape(match_kind_label(hit.match_kind.value))
                    + f" · {hit.size_bytes} B</div></div>"
                )
            result_html = (
                '<div class="search-results" aria-live="polite">' + "".join(hits) + "</div>"
            )
        else:
            result_html = (
                f'<div class="empty-state"><strong>{escape(NO_SEARCH_HITS_TITLE)}</strong></div>'
            )
    return (
        '<form method="get" class="search-box" role="search">'
        f'<input class="search-input" type="search" name="q" value="{escape(query, quote=True)}" '
        f'maxlength="256" placeholder="{escape(SEARCH_PLACEHOLDER, quote=True)}" '
        f'aria-label="{escape(SEARCH_LABEL, quote=True)}">'
        f'<button class="btn btn-primary" type="submit">{escape(SEARCH)}</button></form>'
        + result_html
    )


def render_workspace_page(detail: DashboardWorkspaceDetail, *, base_path: str) -> str:
    row = detail.workspace
    project_url = _url(base_path, "projects", row.project_id)
    live_branch = _display_live_status(row.branch, row)
    live_dirty = _display_live_status(row.dirty_path_count, row)
    actions = ""
    if row.task_id is not None and row.task_revision is not None and row.task_state is not None:
        actions = _render_task_actions(
            workspace_id=row.workspace_id,
            task_id=row.task_id,
            state=row.task_state,
            wait_reason=row.task_wait_reason,
            revision=row.task_revision,
        )
    workspace_name = row.workspace_root.name or WORKSPACE_FALLBACK
    no_actions = f'<p class="section-note">{escape(NO_ACTIONS)}</p>'
    content = (
        '<section class="hero compact"><div>'
        f"<h1>{escape(workspace_name)}</h1>"
        f'<p class="hero-copy">{escape(str(row.workspace_root))}</p></div>'
        '<div class="hero-aside">'
        f'<div class="identity-line">{escape(row.workspace_id)}</div>'
        f'<div class="identity-line">{escape(row.project_id)}</div></div></section>'
        '<section class="detail-grid"><div class="panel">'
        f'<div class="panel-head"><h2>{escape(WORKSPACE_STATE)}</h2>'
        f'{_state_pill(row.task_state, row.task_wait_reason)}</div><div class="panel-body">'
        '<dl class="fact-list">'
        f'<div class="fact"><dt>{escape(PROJECT)}</dt>'
        f'<dd><a href="{escape(project_url, quote=True)}" class="mono">{escape(row.project_id)}</a></dd></div>'
        f'<div class="fact"><dt>{escape(BRANCH)}</dt><dd class="mono">{escape(live_branch)}</dd></div>'
        f'<div class="fact"><dt>{escape(DIRTY_PATHS)}</dt><dd>{escape(live_dirty)}</dd></div>'
        f'<div class="fact"><dt>{escape(INDEXED_PATHS)}</dt><dd>{row.indexed_file_count}</dd></div>'
        f'<div class="fact"><dt>{escape(VISIBILITY)}</dt>'
        f"<dd>{escape(visibility_label(row.visibility_mode))}</dd></div>"
        f'<div class="fact"><dt>{escape(TASK)}</dt><dd class="mono">{escape(_display_task(row))}</dd></div>'
        "</dl></div></div>"
        f'<aside class="panel"><div class="panel-head"><h2>{escape(ACTIONS)}</h2></div>'
        f'<div class="panel-body">{actions if actions else no_actions}</div></aside></section>'
        '<section class="section"><div class="section-head"><div>'
        f'<h2 class="section-title">{escape(SEARCH_SECTION)}</h2></div></div>'
        '<div class="panel"><div class="panel-body">'
        + _render_search(detail)
        + "</div></div></section>"
        '<section class="section"><div class="section-head"><div>'
        f'<h2 class="section-title">{escape(RECENT_TASKS)}</h2></div></div>'
        '<div class="panel"><div class="panel-body">'
        + _render_recent_tasks(detail.recent_tasks, base_path)
        + "</div></div></section>"
    )
    return _render_shell(
        base_path=base_path,
        page_title=document_title(workspace_name),
        breadcrumbs=(
            (BREADCRUMB_PROJECTS, base_path),
            (project_crumb(row.project_id), project_url),
            (workspace_name, None),
        ),
        events_url=_events_url(
            base_path,
            view="workspace",
            identity=row.workspace_id,
            search_query=detail.search_query,
            snapshot=_snapshot_fingerprint(detail),
        ),
        content=content,
    )


def _timeline_event_label(event: TaskEventRecord) -> str:
    return event_label(event.event_type)


def _render_timeline(detail: DashboardTaskDetail) -> str:
    checkpoints = {item.checkpoint_id: item for item in detail.checkpoints}
    visible_events = detail.events
    truncated_count = detail.event_count - len(visible_events)
    items: list[str] = ['<div class="timeline">']
    for event in reversed(visible_events):
        content: list[str] = []
        if event.event_type is TaskEventType.CREATED:
            content.append(_render_timeline_branch(detail.baseline_git_branch))
        checkpoint = (
            checkpoints.get(event.checkpoint_id) if event.checkpoint_id is not None else None
        )
        if checkpoint is not None:
            content.append(
                _render_timeline_branch(
                    DashboardGitBranch(captured=True, name=checkpoint.current_branch)
                )
            )
            content.append(f'<div class="timeline-summary">{escape(checkpoint.summary)}</div>')
            if checkpoint.next_step is not None:
                content.append(
                    f"<div><strong>{escape(NEXT)}:</strong> {escape(checkpoint.next_step)}</div>"
                )
            if checkpoint.changed_paths:
                visible_paths = checkpoint.changed_paths[:_DASHBOARD_CHANGED_PATH_LIMIT]
                chips = "".join(
                    f'<span class="path-chip">{escape(path)}</span>' for path in visible_paths
                )
                remaining = len(checkpoint.changed_paths) - len(visible_paths)
                if remaining:
                    chips += f'<span class="path-chip">{escape(more_paths_label(remaining))}</span>'
                content.append(f'<div class="path-chips">{chips}</div>')
        if event.operator_feedback is not None:
            content.append(
                f'<blockquote class="feedback-quote">{escape(event.operator_feedback)}</blockquote>'
            )
        content_html = (
            '<div class="timeline-content">' + "".join(content) + "</div>" if content else ""
        )
        items.append(
            f'<article class="timeline-item" data-kind="{escape(event.event_type.value, quote=True)}">'
            '<div class="timeline-head">'
            f'<h3 class="timeline-title">{escape(_timeline_event_label(event))}</h3>'
            f'<span class="timeline-time">r{event.task_revision} · {escape(event.created_at)}</span>'
            f"</div>{content_html}</article>"
        )
    if truncated_count:
        items.append(
            '<div class="timeline-item"><div class="timeline-content">'
            f"{escape(omitted_events_label(truncated_count))}"
            "</div></div>"
        )
    items.append("</div>")
    return "".join(items)


def render_task_page(detail: DashboardTaskDetail, *, base_path: str) -> str:
    row = detail.workspace
    task = detail.task
    workspace_url = _url(base_path, "workspaces", row.workspace_id)
    project_url = _url(base_path, "projects", row.project_id)
    wait_reason = None if task.wait_reason is None else task.wait_reason.value
    actions = _render_task_actions(
        workspace_id=task.workspace_id,
        task_id=task.task_id,
        state=task.state.value,
        wait_reason=wait_reason,
        revision=task.revision,
    )
    workspace_name = row.workspace_root.name or WORKSPACE_FALLBACK
    no_actions = f'<p class="section-note">{escape(NO_ACTIONS)}</p>'
    stack = (
        EM_DASH
        if not detail.stack_hints
        else " · ".join(escape(item) for item in detail.stack_hints)
    )
    content = (
        '<section class="hero compact"><div>'
        f'<h1 class="task-title">{escape(task.title)}</h1></div>'
        '<div class="hero-aside">'
        f"{_state_pill(task.state.value, wait_reason)}"
        f'<div class="identity-line">{escape(task.task_id)}</div>'
        f'<div class="identity-line">{escape(REVISION)} {task.revision}</div></div></section>'
        f'<section class="detail-grid"><div class="panel"><div class="panel-head"><h2>{escape(TASK_FACTS)}</h2></div>'
        '<div class="panel-body"><dl class="fact-list">'
        f'<div class="fact"><dt>{escape(WORKSPACE)}</dt>'
        f'<dd><a href="{escape(workspace_url, quote=True)}" class="mono">{escape(task.workspace_id)}</a></dd></div>'
        f'<div class="fact"><dt>{escape(PROJECT)}</dt>'
        f'<dd><a href="{escape(project_url, quote=True)}" class="mono">{escape(row.project_id)}</a></dd></div>'
        f'<div class="fact"><dt>{escape(BRANCH)}</dt>'
        f'<dd class="mono">{escape(_display_recorded_branch(detail.git_branch))}</dd></div>'
        f'<div class="fact"><dt>{escape(STATE)}</dt>'
        f"<dd>{escape(task_state_label(task.state.value, wait_reason))}</dd></div>"
        f'<div class="fact"><dt>{escape(WAIT_REASON)}</dt><dd>{escape(wait_reason_label(wait_reason))}</dd></div>'
        f'<div class="fact"><dt>{escape(STACK_HINTS)}</dt><dd class="mono">{stack}</dd></div>'
        f'<div class="fact"><dt>{escape(CREATED)}</dt><dd>{escape(task.created_at)}</dd></div>'
        f'<div class="fact"><dt>{escape(UPDATED)}</dt><dd>{escape(task.updated_at)}</dd></div>'
        "</dl></div></div>"
        f'<aside class="panel"><div class="panel-head"><h2>{escape(ACTIONS)}</h2></div>'
        f'<div class="panel-body">{actions if actions else no_actions}</div></aside></section>'
        '<section class="section"><div class="section-head"><div>'
        f'<h2 class="section-title">{escape(TIMELINE)}</h2></div>'
        f'<p class="section-note">{escape(event_count_label(detail.event_count))}</p></div>'
        '<div class="panel"><div class="panel-body">'
        + _render_timeline(detail)
        + "</div></div></section>"
    )
    return _render_shell(
        base_path=base_path,
        page_title=document_title(task.title),
        breadcrumbs=(
            (BREADCRUMB_PROJECTS, base_path),
            (project_crumb(row.project_id), project_url),
            (workspace_name, workspace_url),
            (task.title, None),
        ),
        events_url=_events_url(
            base_path,
            view="task",
            identity=task.task_id,
            snapshot=_snapshot_fingerprint(detail),
        ),
        content=content,
    )


def _decode_identity_component(component: str) -> str:
    try:
        identity = unquote_to_bytes(component).decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise DashboardError("dashboard route identity is not valid UTF-8") from exc
    if not identity or len(identity.encode("utf-8")) > 128 or "/" in identity or "\x00" in identity:
        raise DashboardError("dashboard route identity is invalid")
    return identity


def _parse_search_query(query: str) -> str | None:
    if not query:
        return None
    try:
        parsed = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=1,
        )
    except (UnicodeError, ValueError) as exc:
        raise SearchError("dashboard search query is malformed") from exc
    if set(parsed) != {"q"} or len(parsed["q"]) != 1:
        raise SearchError("dashboard search accepts exactly one q field")
    value = parsed["q"][0].strip()
    if not value:
        return None
    if "\x00" in value or len(value.encode("utf-8")) > 256:
        raise SearchError("dashboard search query is invalid")
    return value


def _parse_page_request(base_path: str, path: str, query: str) -> _DashboardPageRequest:
    if path == base_path:
        if query:
            raise DashboardError("Projects route does not accept query fields")
        return _DashboardPageRequest("projects", None, None, base_path)
    if not path.startswith(base_path):
        raise DashboardError("dashboard path is outside the capability route")
    relative = path[len(base_path) :]
    parts = relative.split("/")
    if (
        len(parts) != 3
        or parts[2] != ""
        or parts[0]
        not in {
            "projects",
            "workspaces",
            "tasks",
        }
    ):
        raise DashboardError("dashboard page route is not recognized")
    identity = _decode_identity_component(parts[1])
    if parts[0] == "workspaces":
        search_query = _parse_search_query(query)
    else:
        if query:
            raise DashboardError("dashboard detail route does not accept query fields")
        search_query = None
    redirect = path + (f"?{query}" if query else "")
    kind = {"projects": "project", "workspaces": "workspace", "tasks": "task"}[parts[0]]
    return _DashboardPageRequest(kind, identity, search_query, redirect)


def _render_page(database_path: Path, base_path: str, request: _DashboardPageRequest) -> str:
    if request.kind == "projects":
        return render_projects_page(
            read_dashboard_workspace_rows(database_path),
            base_path=base_path,
        )
    assert request.identity is not None
    if request.kind == "project":
        return render_project_page(
            read_dashboard_project_detail(database_path, request.identity),
            base_path=base_path,
        )
    if request.kind == "workspace":
        return render_workspace_page(
            read_dashboard_workspace_detail(
                database_path,
                request.identity,
                search_query=request.search_query,
            ),
            base_path=base_path,
        )
    if request.kind == "task":
        return render_task_page(
            read_dashboard_task_detail(database_path, request.identity),
            base_path=base_path,
        )
    raise DashboardError("dashboard page kind is unsupported")


def _parse_sse_view(query: str) -> tuple[str, str | None, str | None, str]:
    try:
        parsed = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=4,
        )
    except (UnicodeError, ValueError) as exc:
        raise DashboardError("dashboard event query is malformed") from exc
    if "view" not in parsed or len(parsed["view"]) != 1:
        raise DashboardError("dashboard event query requires one view")
    if "snapshot" not in parsed or len(parsed["snapshot"]) != 1:
        raise DashboardError("dashboard event query requires one snapshot")
    snapshot = parsed["snapshot"][0]
    if len(snapshot) != 64 or any(character not in "0123456789abcdef" for character in snapshot):
        raise DashboardError("dashboard event snapshot is invalid")
    view = parsed["view"][0]
    if view == "projects":
        if set(parsed) != {"view", "snapshot"}:
            raise DashboardError("Projects event query has unexpected fields")
        return view, None, None, snapshot
    if view not in {"project", "workspace", "task"}:
        raise DashboardError("dashboard event view is unsupported")
    identity_key = f"{view}_id"
    allowed = {"view", "snapshot", identity_key}
    if view == "workspace":
        allowed.add("q")
    if set(parsed) - allowed or identity_key not in parsed or len(parsed[identity_key]) != 1:
        raise DashboardError("dashboard event query does not match the expected schema")
    identity = parsed[identity_key][0]
    if not identity or len(identity.encode("utf-8")) > 128 or "\x00" in identity:
        raise DashboardError("dashboard event identity is invalid")
    search_query: str | None = None
    if "q" in parsed:
        if len(parsed["q"]) != 1:
            raise DashboardError("dashboard event search query must be singular")
        search_query = parsed["q"][0].strip() or None
        if search_query is not None and (
            "\x00" in search_query or len(search_query.encode("utf-8")) > 256
        ):
            raise DashboardError("dashboard event search query is invalid")
    return view, identity, search_query, snapshot


def _view_fingerprint(
    database_path: Path,
    view: str,
    identity: str | None,
    search_query: str | None,
) -> str:
    if view == "projects":
        value: object = read_dashboard_workspace_rows(database_path)
    elif view == "project":
        assert identity is not None
        value = read_dashboard_project_detail(database_path, identity)
    elif view == "workspace":
        assert identity is not None
        value = read_dashboard_workspace_detail(
            database_path,
            identity,
            search_query=search_query,
        )
    elif view == "task":
        assert identity is not None
        value = read_dashboard_task_detail(database_path, identity)
    else:
        raise DashboardError("unsupported dashboard fingerprint view")
    return _snapshot_fingerprint(value)


def _sqlite_data_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA data_version").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError("dashboard data_version is invalid")
    return row[0]


class _DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    database_path: ClassVar[Path]
    route_path: ClassVar[str]
    expected_host: ClassVar[str]
    expected_origin: ClassVar[str]
    stop_event: ClassVar[Event]
    sse_slots: ClassVar[BoundedSemaphore]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if self.headers.get("Host") != self.expected_host:
            self._send_html(404, "")
            return
        if parsed.path == f"{self.route_path}assets/dashboard.css" and not parsed.query:
            self._send_bytes(200, "text/css; charset=utf-8", DASHBOARD_CSS.encode("utf-8"))
            return
        if parsed.path == f"{self.route_path}assets/dashboard.js" and not parsed.query:
            self._send_bytes(
                200,
                "application/javascript; charset=utf-8",
                DASHBOARD_JS.encode("utf-8"),
            )
            return
        if parsed.path == f"{self.route_path}events":
            try:
                view, identity, search_query, snapshot = _parse_sse_view(parsed.query)
            except DashboardError:
                self._send_html(400, "")
                return
            self._serve_events(view, identity, search_query, snapshot)
            return
        try:
            page = _parse_page_request(self.route_path, parsed.path, parsed.query)
            html = _render_page(self.database_path, self.route_path, page)
        except SearchError:
            self._send_html(400, "")
            return
        except (TaskNotFoundError, RegistryError):
            self._send_html(404, "")
            return
        except DashboardError:
            self._send_html(404, "")
            return
        except (
            OSError,
            sqlite3.DatabaseError,
            DatabaseError,
            TaskError,
            TaskCheckpointError,
        ):
            self._send_html(
                503,
                f"<!doctype html><title>{escape(UNAVAILABLE_TITLE)}</title>"
                f"<h1>{escape(UNAVAILABLE_HEADING)}</h1>",
            )
            return
        self._send_html(200, html)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            page = _parse_page_request(self.route_path, parsed.path, parsed.query)
        except (DashboardError, SearchError):
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
        self._send_redirect(page.redirect_target)

    def _serve_events(
        self,
        view: str,
        identity: str | None,
        search_query: str | None,
        expected_snapshot: str,
    ) -> None:
        if not self.sse_slots.acquire(blocking=False):
            self._send_html(503, "")
            return
        watch_connection: sqlite3.Connection | None = None
        try:
            try:
                watch_connection = connect_database(self.database_path)
                data_version = _sqlite_data_version(watch_connection)
                current_snapshot = _view_fingerprint(
                    self.database_path,
                    view,
                    identity,
                    search_query,
                )
            except (
                OSError,
                sqlite3.DatabaseError,
                DatabaseError,
                RegistryError,
                TaskError,
                SearchError,
            ):
                self._send_html(404, "")
                return
            self.send_response_only(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self._send_security_headers()
            self.end_headers()
            self._write_sse("retry: 1500\nevent: ready\ndata: live\n\n")
            if current_snapshot != expected_snapshot:
                self._write_sse("event: refresh\ndata: changed\n\n")
            deadline = monotonic() + _DASHBOARD_SSE_SESSION_SECONDS
            heartbeat_at = monotonic() + _DASHBOARD_SSE_HEARTBEAT_SECONDS
            while not self.stop_event.is_set() and monotonic() < deadline:
                sleep(_DASHBOARD_SSE_POLL_SECONDS)
                try:
                    current_data_version = _sqlite_data_version(watch_connection)
                except sqlite3.DatabaseError:
                    return
                if current_data_version != data_version:
                    data_version = current_data_version
                    self._write_sse("event: refresh\ndata: changed\n\n")
                if monotonic() >= heartbeat_at:
                    self._write_sse(": heartbeat\n\n")
                    heartbeat_at = monotonic() + _DASHBOARD_SSE_HEARTBEAT_SECONDS
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        finally:
            if watch_connection is not None:
                watch_connection.close()
            self.sse_slots.release()

    def _write_sse(self, value: str) -> None:
        self.wfile.write(value.encode("utf-8"))
        self.wfile.flush()

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        self._send_html(code, "")

    def _send_html(self, status: int, body: str) -> None:
        self._send_bytes(status, "text/html; charset=utf-8", body.encode("utf-8"))

    def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
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


def _request_handler(
    database_path: Path,
    access_token: str,
    stop_event: Event,
) -> type[_DashboardRequestHandler]:
    class Handler(_DashboardRequestHandler):
        pass

    Handler.database_path = database_path
    Handler.route_path = f"/{access_token}/"
    Handler.stop_event = stop_event
    Handler.sse_slots = BoundedSemaphore(_DASHBOARD_SSE_MAX_CLIENTS)
    return Handler


def dashboard_url_path(socket_path: Path) -> Path:
    """Return the runtime-directory file that publishes the current dashboard URL."""
    return socket_path.parent / _DASHBOARD_URL_FILENAME


def dashboard_token_path(database_path: Path) -> Path:
    """Return the durable capability-token file next to the selected database."""
    return database_path.parent / _DASHBOARD_TOKEN_FILENAME


def _write_private_url_file(path: Path, url: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = f"{url}\n".encode("ascii")
    fd: int | None = None
    try:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(temporary, flags, 0o600)
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fchmod(fd, 0o600)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise OSError("dashboard URL file could not be secured")
        os.close(fd)
        fd = None
        os.replace(temporary, path)
        published = path.lstat()
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_uid != os.geteuid()
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise OSError("dashboard URL file is not a private regular file")
    except OSError:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _unlink_private_url_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _read_private_ascii_line(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 128
    ):
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if b"\n" not in payload:
        return None
    line, remainder = payload.split(b"\n", 1)
    if remainder:
        return None
    try:
        return line.decode("ascii")
    except UnicodeDecodeError:
        return None


def _load_or_create_access_token(path: Path) -> str:
    existing = _read_private_ascii_line(path)
    if existing is not None and _DASHBOARD_TOKEN_PATTERN.fullmatch(existing):
        return existing
    token = secrets.token_urlsafe(32)
    try:
        _write_private_url_file(path, token)
    except OSError:
        pass
    return token


class DashboardServerManager:
    """Own one daemon-lifetime loopback dashboard HTTP server."""

    def __init__(
        self,
        database_path: Path,
        *,
        url_file: Path | None = None,
        port: int = 0,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise DashboardError("dashboard loopback port is invalid")
        self._database_path = database_path
        self._url_file = url_file
        self._token_file = dashboard_token_path(database_path)
        self._port = port
        self._server: _DashboardHttpServer | None = None
        self._thread: Thread | None = None
        self._stop_event: Event | None = None
        self._started_event: Event | None = None
        self._url: str | None = None
        self._failure: BaseException | None = None

    def is_running(self) -> bool:
        """Return whether the daemon-owned dashboard listener is currently healthy and running."""
        return (
            self._server is not None
            and self._thread is not None
            and self._thread.is_alive()
            and self._failure is None
        )

    def get_url(self) -> str:
        """Return the capability-bearing loopback URL, starting the listener if needed."""
        if self._server is not None and self._thread is not None and self._thread.is_alive():
            assert self._url is not None
            self._publish_url_file(self._url)
            return self._url
        self.close()

        access_token = _load_or_create_access_token(self._token_file)
        stop_event = Event()
        started_event = Event()
        self._failure = None
        try:
            try:
                server = _DashboardHttpServer(
                    (DASHBOARD_HOST, self._port),
                    _request_handler(self._database_path, access_token, stop_event),
                )
            except OSError as exc:
                requested = self._port if self._port else "ephemeral"
                raise DashboardError(
                    f"dashboard loopback listener could not bind {DASHBOARD_HOST}:{requested}"
                ) from exc
            server.timeout = 0.05
            port = server.server_address[1]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                server.server_close()
                raise DashboardError("dashboard loopback listener returned an invalid port")
            handler = cast(type[_DashboardRequestHandler], server.RequestHandlerClass)
            handler.expected_host = f"{DASHBOARD_HOST}:{port}"
            handler.expected_origin = f"http://{DASHBOARD_HOST}:{port}"
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
            self._url = f"http://{DASHBOARD_HOST}:{port}/{access_token}/"
            thread.start()
            self._wait_until_started(thread, started_event)
            self._publish_url_file(self._url)
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

    def _publish_url_file(self, url: str) -> None:
        if self._url_file is None:
            return
        try:
            _write_private_url_file(self._url_file, url)
        except OSError:
            return

    def close(self) -> None:
        """Stop only the dashboard server owned by this manager."""
        server = self._server
        thread = self._thread
        stop_event = self._stop_event
        url_file = self._url_file
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
        if url_file is not None:
            _unlink_private_url_file(url_file)
        if thread is not None and thread.is_alive():
            raise DashboardError("dashboard server did not stop cleanly")
