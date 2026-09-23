from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import SimpleQueue
from threading import BoundedSemaphore, Event, Thread
from time import monotonic, sleep
from typing import ClassVar, cast
from urllib.parse import parse_qs, quote, unquote_to_bytes, urlencode, urlsplit

from harness.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS
from harness.dashboard_i18n import (
    ACCEPT,
    ACTION_REJECTED,
    ACTIONS,
    ALL_PROJECTS,
    BRANCH,
    BRAND,
    BREADCRUMB_PROJECTS,
    CANCEL,
    CANCEL_TASK,
    COMMENT_LABEL,
    COMMENT_PLACEHOLDER,
    COMMENT_SUBMIT,
    COMMENT_SUMMARY,
    CREATED,
    CURRENT_TASK,
    DELETE_PROJECT,
    DELETE_PROJECT_CONFIRM_LABEL,
    DELETE_PROJECT_CONFIRM_VALUE,
    DELETE_PROJECT_HINT,
    DELETE_PROJECT_SUMMARY,
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
    FORM_DRAFT_LABEL,
    FORM_DRAFT_SAVED,
    FORM_DRAFT_UNAVAILABLE,
    FORM_ERROR_BACK,
    FORM_ERROR_CONFLICT,
    FORM_ERROR_INVALID,
    FORM_ERROR_TITLE,
    GIT_UNAVAILABLE,
    HOME_SEARCH_LABEL,
    HOME_SEARCH_PLACEHOLDER,
    INDEX,
    INDEXED_PATHS,
    JIRA,
    JIRA_CLEAR,
    JIRA_LABEL,
    JIRA_PLACEHOLDER,
    JIRA_SAVE,
    LIVE_CONNECTING,
    LIVE_MANUAL,
    LIVE_REFRESH,
    METRIC_ACTIVE,
    METRIC_PROJECTS,
    METRIC_REVIEW,
    METRICS_LABEL,
    MODE,
    NAVIGATION,
    NAVIGATION_UNAVAILABLE,
    NEXT,
    NEXT_STEP,
    NO_ACTIONS,
    NO_SEARCH_HITS_TITLE,
    NO_TASK,
    NO_TASKS_TITLE,
    OPEN_NAVIGATION,
    OPEN_TASK,
    OPEN_WORKSPACE,
    OPERATOR_STATE_WORKING,
    OPERATOR_STATUS,
    OPERATOR_STATUS_DEPLOY_PROD,
    OPERATOR_STATUS_DEPLOY_TEST,
    PAGE_PROJECTS,
    PROJECT,
    PROJECT_OVERVIEW,
    PROJECTS_NAV,
    RECENT_TASKS,
    RECENT_TASKS_HOME,
    REOPEN_TASK,
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
    TASK_FOCUS,
    TASK_OVERVIEW,
    TIMELINE,
    UNAVAILABLE_HEADING,
    UPDATED,
    VERIFICATION_EMPTY,
    VERIFICATION_NO_REPORT,
    VERIFICATION_OLDER,
    VERIFICATION_TITLE,
    VISIBILITY,
    VISIBILITY_HINT_HIDDEN,
    VISIBILITY_HINT_NORMAL,
    VISIBILITY_SET_HIDDEN,
    VISIBILITY_SET_NORMAL,
    WAIT_REASON,
    WORKSPACE,
    WORKSPACE_FALLBACK,
    WORKSPACE_HOME,
    WORKSPACE_OVERVIEW,
    WORKSPACE_RELOCATION_HINT,
    WORKSPACE_RELOCATION_LABEL,
    WORKSPACE_RELOCATION_PLACEHOLDER,
    WORKSPACE_RELOCATION_SUBMIT,
    WORKSPACE_RELOCATION_SUMMARY,
    WORKSPACE_STATE,
    document_title,
    event_count_label,
    event_label,
    more_paths_label,
    operator_status_label,
    project_crumb,
    task_crumb,
    task_state_label,
    verification_report_label,
    verification_source_label,
    verification_status_label,
    visibility_label,
    wait_reason_label,
    workspace_count_label,
)
from harness.dashboard_skill_view import render_skill_policy
from harness.dashboard_skills import DashboardSkillsSnapshot, read_dashboard_skills
from harness.git_applicability import WorkspaceApplicability
from harness.git_workspace import (
    GitWorkspaceError,
    inspect_git_working_tree_status,
    inspect_workspace_runtime_identity,
    layout_has_git,
)
from harness.hidden_projection import HiddenProjectionCollisionError, HiddenProjectionError
from harness.host_integration_state import (
    HostIntegrationStateError,
    load_host_integration_state_for_database,
)
from harness.registry import (
    ProjectNotFoundError,
    ProjectRecord,
    RegistryError,
    VisibilityMode,
    WorkspaceRecord,
    delete_project,
    get_project,
    get_workspace,
    list_workspaces,
    relocate_workspace,
    workspace_layout_compatible,
)
from harness.retrieval import ProjectSearchHit, search_tasks
from harness.runtime_paths import DASHBOARD_HOST
from harness.search import SearchError
from harness.skill_policy import (
    MANAGED_PROJECT_SKILL_FACETS,
    ProjectSkillFacetMode,
    ProjectSkillPolicy,
    ProjectSkillPolicyError,
    get_project_skill_policy,
    set_project_skill_facet_mode,
)
from harness.skill_runtime import (
    SkillRuntimeError,
    active_skill_profiles_for_runtime,
    reconcile_workspace_skills,
)
from harness.storage import DatabaseError, connect_database
from harness.task_checkpoints import (
    TaskCheckpointError,
    TaskCheckpointRecord,
    TaskEventRecord,
    TaskEventType,
    get_latest_task_checkpoint_status,
    get_task_checkpoint,
    list_task_checkpoints,
    list_task_events,
)
from harness.task_workflow import (
    task_accept,
    task_cancel,
    task_comment,
    task_delete,
    task_feedback,
    task_reopen,
    task_set_deployment,
    task_set_jira_url,
    task_set_operator_status,
    task_set_state,
)
from harness.tasks import (
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskOperatorStatus,
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
from harness.vault_process import VaultProcess
from harness.verification import VerificationError, VerificationRecord, list_checkpoint_verification
from harness.visibility import set_project_visibility

_DASHBOARD_URL_FILENAME = "dashboard.url"
_DASHBOARD_TOKEN_FILENAME = "dashboard.token"
_DASHBOARD_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_DASHBOARD_START_TIMEOUT_SECONDS = 2.0
_DASHBOARD_STOP_TIMEOUT_SECONDS = 2.0
_DASHBOARD_FORM_MAX_BYTES = 8192
_DASHBOARD_FORM_MAX_FIELDS = 6
_DASHBOARD_SEARCH_LIMIT = 24
_DASHBOARD_RECENT_TASK_LIMIT = 24
# Pin live Tasks so review/waiting/working stay visible at the top of the bounded list.
_DASHBOARD_RECENT_TASK_ORDER_SQL = (
    "CASE WHEN tasks.state IN ('working', 'waiting') THEN 0 ELSE 1 END, "
    "tasks.updated_at DESC, tasks.id DESC"
)
_DASHBOARD_TIMELINE_EVENT_LIMIT = 60
_DASHBOARD_MAX_HISTORY_PAGE = 2**31 - 1
_DASHBOARD_CHANGED_PATH_LIMIT = 24
_DASHBOARD_SSE_POLL_SECONDS = 1.0
_DASHBOARD_SSE_HEARTBEAT_SECONDS = 10.0
_DASHBOARD_SSE_SESSION_SECONDS = 30.0
_DASHBOARD_SSE_MAX_CLIENTS = 8
_DASHBOARD_LIVE_STATUS_WORKERS = 8
_DASHBOARD_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _allows_dashboard_mutation(
    *,
    host: str | None,
    origin: str | None,
    sec_fetch_site: str | None,
    expected_host: str,
    expected_origin: str,
) -> bool:
    """Accept exact Host plus matching Origin, or Sec-Fetch-Site same-origin if Origin is absent/null."""
    if host != expected_host:
        return False
    if origin is not None and origin.strip() not in {"", "null"}:
        return origin.strip().rstrip("/") == expected_origin
    return sec_fetch_site == "same-origin"


def _action_rejected_html() -> str:
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{escape(ACTION_REJECTED)}</title></head>"
        f"<body><h1>{escape(ACTION_REJECTED)}</h1></body></html>"
    )


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
    task_jira_url: str | None
    task_operator_status: str | None
    last_activity: str | None
    next_step: str | None
    task_git_branch: DashboardGitBranch | None
    branch: str | None
    dirty_path_count: int | None
    indexed_file_count: int
    live_error: str | None
    active_task_count: int = 0
    review_task_count: int = 0


@dataclass(frozen=True, slots=True)
class DashboardTaskRow:
    """One recent Task plus the durable Git branch recorded for that Task."""

    task: TaskRecord
    git_branch: DashboardGitBranch
    project_id: str


@dataclass(frozen=True, slots=True)
class DashboardHomePage:
    """Loopback home: Project navigation, recent Tasks, and optional daemon-wide Task search."""

    workspaces: tuple[DashboardWorkspaceRow, ...]
    recent_tasks: tuple[DashboardTaskRow, ...]
    search_query: str | None
    task_search_results: tuple[ProjectSearchHit, ...]
    page: int = 1
    task_count: int = 0


@dataclass(frozen=True, slots=True)
class DashboardProjectDetail:
    """One Project plus its Workspaces and durable skill-scope policy."""

    project: ProjectRecord
    workspaces: tuple[DashboardWorkspaceRow, ...]
    skill_policy: ProjectSkillPolicy
    skills: DashboardSkillsSnapshot | None = None


@dataclass(frozen=True, slots=True)
class DashboardWorkspaceDetail:
    """One Workspace, recent durable Tasks, and optional Task-history lookup."""

    workspace: DashboardWorkspaceRow
    recent_tasks: tuple[DashboardTaskRow, ...]
    search_query: str | None
    task_search_results: tuple[ProjectSearchHit, ...]
    page: int = 1
    task_count: int = 0


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
    latest_checkpoint: TaskCheckpointRecord | None = None
    page: int = 1
    checkpoint_verification: tuple[VerificationRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class DashboardActionRequest:
    """One validated human Task mutation from the dashboard UI."""

    action: str
    workspace_id: str
    task_id: str
    expected_revision: int
    feedback: str | None = None
    comment: str | None = None
    jira_url: str | None = None
    operator_status: TaskOperatorStatus | None = None
    state: TaskState | None = None
    wait_reason: TaskWaitReason | None = None
    deploy_test: bool = False
    deploy_prod: bool = False


@dataclass(frozen=True, slots=True)
class DashboardVisibilityRequest:
    """One validated operator visibility mutation from the dashboard UI."""

    project_id: str
    visibility_mode: VisibilityMode


@dataclass(frozen=True, slots=True)
class DashboardSkillPolicyRequest:
    """One validated Project skill-surface mutation from the dashboard UI."""

    project_id: str
    facet: str
    mode: ProjectSkillFacetMode


@dataclass(frozen=True, slots=True)
class DashboardProjectDeleteRequest:
    """One explicitly confirmed Project deletion from its dashboard detail page."""

    project_id: str


@dataclass(frozen=True, slots=True)
class DashboardWorkspaceRelocationRequest:
    """One operator-requested rebind of a Workspace to a new absolute Git path."""

    workspace_id: str
    new_path: Path


@dataclass(frozen=True, slots=True)
class _DashboardPageRequest:
    kind: str
    identity: str | None
    search_query: str | None
    redirect_target: str
    page: int = 1
    settings: bool = False


def _read_dashboard_navigation_rows(database_path: Path) -> tuple[DashboardWorkspaceRow, ...]:
    """Read persisted Workspace summaries needed by global dashboard navigation."""
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
    return rows


def read_dashboard_workspace_rows(database_path: Path) -> tuple[DashboardWorkspaceRow, ...]:
    """Read a consistent persisted Projects overview plus fail-closed live Git summaries."""
    return _with_live_workspace_statuses(_read_dashboard_navigation_rows(database_path))


def _load_recent_dashboard_tasks(
    connection: sqlite3.Connection,
    *,
    workspace_id: str | None = None,
    page: int = 1,
) -> tuple[DashboardTaskRow, ...]:
    if workspace_id is None:
        rows = connection.execute(
            f"""
            SELECT tasks.id, workspaces.project_id
            FROM tasks
            INNER JOIN workspaces ON workspaces.id = tasks.workspace_id
            ORDER BY {_DASHBOARD_RECENT_TASK_ORDER_SQL}
            LIMIT ? OFFSET ?
            """,
            (_DASHBOARD_RECENT_TASK_LIMIT, (page - 1) * _DASHBOARD_RECENT_TASK_LIMIT),
        ).fetchall()
    else:
        rows = connection.execute(
            f"""
            SELECT tasks.id, workspaces.project_id
            FROM tasks
            INNER JOIN workspaces ON workspaces.id = tasks.workspace_id
            WHERE tasks.workspace_id = ?
            ORDER BY {_DASHBOARD_RECENT_TASK_ORDER_SQL}
            LIMIT ? OFFSET ?
            """,
            (
                workspace_id,
                _DASHBOARD_RECENT_TASK_LIMIT,
                (page - 1) * _DASHBOARD_RECENT_TASK_LIMIT,
            ),
        ).fetchall()
    loaded = tuple((get_task(connection, task_id), project_id) for task_id, project_id in rows)
    recorded_branches = _read_recorded_git_branches(
        connection,
        tuple(task.task_id for task, _project_id in loaded),
    )
    return tuple(
        DashboardTaskRow(
            task=task,
            git_branch=recorded_branches[task.task_id],
            project_id=project_id,
        )
        for task, project_id in loaded
    )


def _history_page(page: int, total: int, page_size: int) -> int:
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= _DASHBOARD_MAX_HISTORY_PAGE
    ):
        raise SearchError("dashboard history page must be a positive bounded integer")
    return min(page, max(1, (total + page_size - 1) // page_size))


def _dashboard_task_count(
    connection: sqlite3.Connection,
    workspace_id: str | None = None,
) -> int:
    where = "" if workspace_id is None else " WHERE workspace_id = ?"
    params = () if workspace_id is None else (workspace_id,)
    row = connection.execute("SELECT COUNT(*) FROM tasks" + where, params).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
        raise sqlite3.DatabaseError("invalid dashboard Task count")
    return row[0]


def read_dashboard_home(
    database_path: Path,
    *,
    search_query: str | None = None,
    page: int = 1,
    include_live_status: bool = True,
) -> DashboardHomePage:
    """Read the loopback home page: Projects, recent Tasks, and optional Task search."""
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN")
        try:
            workspaces = list_workspaces(connection)
            persisted = tuple(
                _read_workspace_row_persisted(connection, item) for item in workspaces
            )
            task_count = _dashboard_task_count(connection)
            page = _history_page(page, task_count, _DASHBOARD_RECENT_TASK_LIMIT)
            recent_tasks = _load_recent_dashboard_tasks(connection, page=page)
            results = (
                ()
                if search_query is None
                else search_tasks(connection, search_query, limit=_DASHBOARD_SEARCH_LIMIT)
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return DashboardHomePage(
        workspaces=(_with_live_workspace_statuses(persisted) if include_live_status else persisted),
        recent_tasks=recent_tasks,
        search_query=search_query,
        task_search_results=results,
        page=page,
        task_count=task_count,
    )


def read_dashboard_project_detail(
    database_path: Path,
    project_id: str,
    *,
    include_skills: bool = False,
) -> DashboardProjectDetail:
    """Read one Project and its Workspace summaries from the daemon-owned database."""
    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN")
        try:
            project = get_project(connection, project_id)
            skill_policy = get_project_skill_policy(connection, project_id)
            skills = (
                read_dashboard_skills(connection, project_id, database_path=database_path)
                if include_skills
                else None
            )
            workspaces = list_workspaces(connection, project_id=project_id)
            rows = []
            for item in workspaces:
                try:
                    applicability = WorkspaceApplicability(connection, item.workspace_id)
                except GitWorkspaceError:
                    row = _read_workspace_row_persisted(connection, item, tasks_available=False)
                    rows.append(
                        replace(
                            _with_live_workspace_status(row),
                            live_error="Workspace applicability unavailable",
                        )
                    )
                    continue
                row = _read_workspace_row_persisted(connection, item, applicability=applicability)
                row = _with_applicability_live_workspace_status(row, applicability)
                applicability.validate()
                rows.append(row)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return DashboardProjectDetail(
        project=project,
        workspaces=tuple(rows),
        skill_policy=skill_policy,
        skills=skills,
    )


def read_dashboard_workspace_detail(
    database_path: Path,
    workspace_id: str,
    *,
    search_query: str | None = None,
    page: int = 1,
) -> DashboardWorkspaceDetail:
    """Read one Workspace detail page with the operator Task archive and optional Task search."""
    connection = connect_database(database_path)
    try:
        try:
            applicability: WorkspaceApplicability | None = WorkspaceApplicability(
                connection, workspace_id
            )
        except GitWorkspaceError:
            applicability = None
        connection.execute("BEGIN")
        try:
            workspace = get_workspace(connection, workspace_id)
            row = _read_workspace_row_persisted(
                connection,
                workspace,
                applicability=applicability,
                tasks_available=applicability is not None,
            )
            task_count = (
                _dashboard_task_count(connection, workspace_id) if applicability is not None else 0
            )
            page = _history_page(page, task_count, _DASHBOARD_RECENT_TASK_LIMIT)
            recent_tasks = (
                ()
                if applicability is None
                else _load_recent_dashboard_tasks(connection, workspace_id=workspace_id, page=page)
            )
            task_results = (
                ()
                if search_query is None or applicability is None
                else search_tasks(
                    connection,
                    search_query,
                    limit=_DASHBOARD_SEARCH_LIMIT,
                    project_id=workspace.project_id,
                )
            )
            if applicability is not None:
                row = _with_applicability_live_workspace_status(row, applicability)
            if applicability is None:
                row = replace(row, live_error="Workspace applicability unavailable")
            if applicability is not None:
                applicability.validate()
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    return DashboardWorkspaceDetail(
        workspace=row,
        recent_tasks=recent_tasks,
        search_query=search_query,
        task_search_results=task_results,
        page=page,
        task_count=task_count,
    )


def read_dashboard_task_detail(
    database_path: Path,
    task_id: str,
    *,
    page: int = 1,
    include_live_status: bool = True,
) -> DashboardTaskDetail:
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
            count_row = connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if (
                count_row is None
                or isinstance(count_row[0], bool)
                or not isinstance(count_row[0], int)
                or count_row[0] < 0
            ):
                raise sqlite3.DatabaseError("invalid dashboard Task event count")
            event_count = count_row[0]
            page = _history_page(page, event_count, _DASHBOARD_TIMELINE_EVENT_LIMIT)
            events = list_task_events(
                connection,
                task_id,
                limit=_DASHBOARD_TIMELINE_EVENT_LIMIT,
                offset=(page - 1) * _DASHBOARD_TIMELINE_EVENT_LIMIT,
            )
            checkpoints = tuple(
                get_task_checkpoint(connection, event.checkpoint_id)
                for event in events
                if event.checkpoint_id is not None
            )
            if any(checkpoint.task_id != task_id for checkpoint in checkpoints):
                raise TaskCheckpointError("dashboard checkpoint crossed Task ownership")
            latest = list_task_checkpoints(connection, task_id, limit=1)
            latest_checkpoint = latest[0] if latest else None
            verification_checkpoint_ids = dict.fromkeys(
                checkpoint.checkpoint_id
                for checkpoint in (
                    *checkpoints,
                    *((latest_checkpoint,) if latest_checkpoint is not None else ()),
                )
            )
            try:
                checkpoint_verification = tuple(
                    record
                    for checkpoint_id in verification_checkpoint_ids
                    for record in list_checkpoint_verification(connection, checkpoint_id)
                )
            except VerificationError as exc:
                raise sqlite3.DatabaseError("invalid dashboard checkpoint verification") from exc
            git_branch = (
                DashboardGitBranch(captured=True, name=latest_checkpoint.current_branch)
                if latest_checkpoint is not None
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
        workspace=_with_live_workspace_status(row) if include_live_status else row,
        task=task,
        git_branch=git_branch,
        baseline_git_branch=baseline_git_branch,
        stack_hints=stack_hints,
        checkpoints=checkpoints,
        events=events,
        event_count=event_count,
        latest_checkpoint=latest_checkpoint,
        page=page,
        checkpoint_verification=checkpoint_verification,
    )


def _read_workspace_row_persisted(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    *,
    applicability: WorkspaceApplicability | None = None,
    tasks_available: bool = True,
) -> DashboardWorkspaceRow:
    if get_workspace(connection, workspace.workspace_id) != workspace:
        raise sqlite3.DatabaseError("workspace registry changed during dashboard read")
    project = get_project(connection, workspace.project_id)
    task = (
        get_relevant_task(connection, workspace.workspace_id, applicability=applicability)
        if tasks_available
        else None
    )
    if task is None and tasks_available:
        task = get_latest_task(connection, workspace.workspace_id, applicability=applicability)
    checkpoint = (
        get_latest_task_checkpoint_status(connection, task.task_id) if task is not None else None
    )
    if (
        checkpoint is not None
        and applicability is not None
        and not applicability.checkpoint_visible(checkpoint.checkpoint_id)
    ):
        checkpoint = None
    counts = connection.execute(
        """
        SELECT COALESCE(SUM(state IN ('working', 'waiting')), 0),
               COALESCE(SUM(state = 'waiting' AND wait_reason = 'operator_review'), 0)
        FROM tasks WHERE workspace_id = ?
        """,
        (workspace.workspace_id,),
    ).fetchone()
    if not tasks_available:
        counts = (0, 0)
    if counts is None or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts
    ):
        raise sqlite3.DatabaseError("invalid dashboard Workspace Task counts")
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
        task_jira_url=None if task is None else task.jira_url,
        task_operator_status=(
            None if task is None or task.operator_status is None else task.operator_status.value
        ),
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
        active_task_count=counts[0],
        review_task_count=counts[1],
    )


def _with_live_workspace_status(row: DashboardWorkspaceRow) -> DashboardWorkspaceRow:
    branch: str | None = None
    dirty_path_count: int | None = None
    live_error: str | None = None
    try:
        before = inspect_workspace_runtime_identity(row.workspace_root)
        if not workspace_layout_compatible(
            WorkspaceRecord(
                workspace_id=row.workspace_id,
                project_id=row.project_id,
                workspace_root=row.workspace_root,
                git_common_dir=row.git_common_dir,
            ),
            before.layout,
        ):
            raise GitWorkspaceError("registered Workspace identity changed")
        if layout_has_git(before.layout):
            status = inspect_git_working_tree_status(row.workspace_root)
            after = inspect_workspace_runtime_identity(row.workspace_root)
            if after != before:
                raise GitWorkspaceError("Workspace Git identity changed during dashboard read")
            branch = status.branch
            dirty_path_count = status.dirty_path_count
        else:
            after = inspect_workspace_runtime_identity(row.workspace_root)
            if after != before:
                raise GitWorkspaceError("Workspace identity changed during dashboard read")
            branch = None
            dirty_path_count = 0
    except GitWorkspaceError:
        live_error = "Git status unavailable"
    return replace(
        row,
        branch=branch,
        dirty_path_count=dirty_path_count,
        live_error=live_error,
    )


def _with_applicability_live_workspace_status(
    row: DashboardWorkspaceRow,
    applicability: WorkspaceApplicability,
) -> DashboardWorkspaceRow:
    """Add live status inside an applicability proof without repeating its identity checks."""
    if applicability.workspace != WorkspaceRecord(
        workspace_id=row.workspace_id,
        project_id=row.project_id,
        workspace_root=row.workspace_root,
        git_common_dir=row.git_common_dir,
    ):
        raise GitWorkspaceError("dashboard applicability belongs to another Workspace")
    try:
        if applicability.has_git:
            status = inspect_git_working_tree_status(row.workspace_root)
            return replace(
                row,
                branch=status.branch,
                dirty_path_count=status.dirty_path_count,
                live_error=None,
            )
        return replace(row, branch=None, dirty_path_count=0, live_error=None)
    except GitWorkspaceError:
        return replace(
            row,
            branch=None,
            dirty_path_count=None,
            live_error="Git status unavailable",
        )


def _with_live_workspace_statuses(
    rows: tuple[DashboardWorkspaceRow, ...],
) -> tuple[DashboardWorkspaceRow, ...]:
    if len(rows) <= 1:
        return tuple(_with_live_workspace_status(row) for row in rows)
    workers = min(_DASHBOARD_LIVE_STATUS_WORKERS, len(rows))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return tuple(pool.map(_with_live_workspace_status, rows))


def _without_live_git(row: DashboardWorkspaceRow) -> DashboardWorkspaceRow:
    if row.branch is None and row.dirty_path_count is None and row.live_error is None:
        return row
    return replace(row, branch=None, dirty_path_count=None, live_error=None)


def _fingerprint_home(home: DashboardHomePage) -> DashboardHomePage:
    return replace(home, workspaces=tuple(_without_live_git(row) for row in home.workspaces))


def _fingerprint_task_detail(detail: DashboardTaskDetail) -> DashboardTaskDetail:
    return replace(detail, workspace=_without_live_git(detail.workspace))


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


def mutate_dashboard_task(
    database_path: Path,
    request: DashboardActionRequest,
) -> None:
    """Delegate one dashboard action to the authoritative Task domain workflow."""
    connection = connect_database(database_path)
    try:
        if request.action == "delete_task":
            task_delete(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
            )
        elif request.action == "set_state":
            assert request.state is not None
            task_set_state(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                state=request.state,
                wait_reason=request.wait_reason,
            )
        elif request.action == "set_deployment":
            task_set_deployment(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                deploy_test=request.deploy_test,
                deploy_prod=request.deploy_prod,
            )
        elif request.action == "accept":
            task_accept(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
            )
        elif request.action == "feedback":
            assert request.feedback is not None
            task_feedback(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                feedback=request.feedback,
            )
        elif request.action == "cancel":
            task_cancel(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
            )
        elif request.action == "reopen":
            task_reopen(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
            )
        elif request.action == "comment":
            assert request.comment is not None
            task_comment(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                comment=request.comment,
            )
        elif request.action == "set_jira":
            task_set_jira_url(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                jira_url=request.jira_url,
            )
        elif request.action == "set_operator_status":
            task_set_operator_status(
                connection,
                request.workspace_id,
                request.task_id,
                expected_revision=request.expected_revision,
                operator_status=request.operator_status,
            )
        else:
            raise TaskValidationError("unsupported dashboard Task action")
    finally:
        connection.close()


def mutate_dashboard_visibility(database_path: Path, request: DashboardVisibilityRequest) -> None:
    """Persist hygiene-effective Hidden or restore Normal from the dashboard."""
    connection = connect_database(database_path)
    try:
        profiles = tuple(sorted(load_host_integration_state_for_database(database_path).profiles))
        set_project_visibility(
            connection,
            mode=request.visibility_mode,
            host_profiles=profiles,
            project_id=request.project_id,
        )
    finally:
        connection.close()


def mutate_dashboard_skill_policy(
    database_path: Path,
    request: DashboardSkillPolicyRequest,
) -> tuple[str, ...]:
    """Persist one Project scope override and reconcile skills without rescanning source."""
    profiles = active_skill_profiles_for_runtime(database_path)
    connection = connect_database(database_path)
    try:
        set_project_skill_facet_mode(
            connection,
            request.project_id,
            request.facet,
            request.mode,
        )
        workspaces = list_workspaces(connection, project_id=request.project_id)
        if not profiles:
            return ()
        retry: list[str] = []
        for workspace in workspaces:
            try:
                reconcile_workspace_skills(connection, workspace.workspace_id, profiles)
            except (GitWorkspaceError, SkillRuntimeError):
                retry.append(workspace.workspace_id)
        return tuple(retry)
    finally:
        connection.close()


def mutate_dashboard_registry(
    database_path: Path,
    request: DashboardProjectDeleteRequest | DashboardWorkspaceRelocationRequest,
) -> None:
    """Apply an explicit Project deletion or Workspace relocation to daemon-owned state."""
    connection = connect_database(database_path)
    try:
        if isinstance(request, DashboardProjectDeleteRequest):
            delete_project(connection, request.project_id)
            return
        relocate_workspace(
            connection,
            request.workspace_id,
            new_path=request.new_path,
        )
    finally:
        connection.close()


def _parse_dashboard_form_fields(payload: bytes) -> dict[str, str]:
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
    return {name: values[0] for name, values in parsed.items()}


def _parse_dashboard_action_form(
    payload: bytes,
) -> (
    DashboardActionRequest
    | DashboardVisibilityRequest
    | DashboardSkillPolicyRequest
    | DashboardProjectDeleteRequest
    | DashboardWorkspaceRelocationRequest
):
    fields = _parse_dashboard_form_fields(payload)
    action = fields.get("action")
    if action == "delete_project":
        expected = {"action", "project_id", "confirmation"}
        if set(fields) != expected:
            raise TaskValidationError(
                "dashboard Project deletion form does not match the expected schema"
            )
        project_id = fields["project_id"]
        if (
            not project_id
            or len(project_id) > 128
            or "\x00" in project_id
            or fields["confirmation"] != DELETE_PROJECT_CONFIRM_VALUE
        ):
            raise TaskValidationError("dashboard Project deletion fields are invalid")
        return DashboardProjectDeleteRequest(project_id=project_id)
    if action == "relocate_workspace":
        expected = {"action", "workspace_id", "new_path"}
        if set(fields) != expected:
            raise TaskValidationError(
                "dashboard Workspace relocation form does not match the expected schema"
            )
        workspace_id = fields["workspace_id"]
        new_path = fields["new_path"].strip()
        if (
            not workspace_id
            or len(workspace_id) > 128
            or "\x00" in workspace_id
            or not new_path
            or "\x00" in new_path
            or len(new_path.encode("utf-8")) > 2048
            or not Path(new_path).is_absolute()
        ):
            raise TaskValidationError("dashboard Workspace relocation fields are invalid")
        return DashboardWorkspaceRelocationRequest(
            workspace_id=workspace_id,
            new_path=Path(new_path),
        )
    if action == "set_skill_scope":
        expected = {"action", "project_id", "facet", "mode"}
        if set(fields) != expected:
            raise TaskValidationError(
                "dashboard skill-scope form does not match the expected schema"
            )
        project_id = fields["project_id"]
        facet = fields["facet"]
        mode_text = fields["mode"]
        if (
            not project_id
            or len(project_id) > 128
            or "\x00" in project_id
            or facet not in MANAGED_PROJECT_SKILL_FACETS
            or mode_text
            not in {
                ProjectSkillFacetMode.AUTO.value,
                ProjectSkillFacetMode.INCLUDED.value,
                ProjectSkillFacetMode.EXCLUDED.value,
            }
        ):
            raise TaskValidationError("dashboard skill-scope fields are invalid")
        return DashboardSkillPolicyRequest(
            project_id=project_id,
            facet=facet,
            mode=ProjectSkillFacetMode(mode_text),
        )
    if action == "set_visibility":
        expected = {"action", "project_id", "visibility_mode"}
        if set(fields) != expected:
            raise TaskValidationError(
                "dashboard visibility form does not match the expected schema"
            )
        project_id = fields["project_id"]
        mode_text = fields["visibility_mode"]
        if (
            not project_id
            or len(project_id) > 128
            or "\x00" in project_id
            or mode_text not in {VisibilityMode.NORMAL.value, VisibilityMode.HIDDEN.value}
        ):
            raise TaskValidationError("dashboard visibility fields are invalid")
        return DashboardVisibilityRequest(
            project_id=project_id,
            visibility_mode=VisibilityMode(mode_text),
        )
    expected = {"action", "workspace_id", "task_id", "expected_revision"}
    if action == "feedback":
        expected.add("feedback")
    elif action == "comment":
        expected.add("comment")
    elif action == "set_jira":
        expected.add("jira_url")
    elif action == "set_operator_status":
        expected.add("operator_status")
    elif action == "set_state":
        expected.update(("state", "wait_reason"))
    elif action == "delete_task":
        expected.add("confirmation")
    elif action == "set_deployment":
        expected.update(("deploy_test", "deploy_prod"))
        fields.setdefault("deploy_test", "0")
        fields.setdefault("deploy_prod", "0")
    if (
        action
        not in {
            "accept",
            "set_state",
            "delete_task",
            "set_deployment",
            "feedback",
            "cancel",
            "reopen",
            "comment",
            "set_jira",
            "set_operator_status",
        }
        or set(fields) != expected
    ):
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
    try:
        expected_revision = int(revision_text)
    except ValueError as exc:
        raise TaskValidationError("dashboard action expected_revision is invalid") from exc
    if expected_revision <= 0:
        raise TaskValidationError("dashboard action expected_revision must be positive")
    operator_status_text = fields.get("operator_status")
    if operator_status_text not in {
        None,
        "",
        TaskOperatorStatus.DEPLOY_TEST.value,
        TaskOperatorStatus.DEPLOY_PROD.value,
        TaskOperatorStatus.DEPLOY_BOTH.value,
    }:
        raise TaskValidationError("dashboard operator status is unsupported")
    state = None
    wait_reason = None
    if action == "set_state":
        try:
            state = TaskState(fields["state"])
            wait_reason = TaskWaitReason(fields["wait_reason"]) if fields["wait_reason"] else None
        except ValueError as exc:
            raise TaskValidationError("dashboard Task state is unsupported") from exc
        if state is TaskState.WAITING and wait_reason is None:
            raise TaskValidationError("waiting requires a reason")
        if state is not TaskState.WAITING:
            wait_reason = None
    if action == "delete_task" and fields["confirmation"] != task_id:
        raise TaskValidationError("Task deletion requires exact Task confirmation")
    if action == "set_deployment" and any(
        fields[name] not in {"0", "1"} for name in ("deploy_test", "deploy_prod")
    ):
        raise TaskValidationError("dashboard deployment flag is invalid")
    return DashboardActionRequest(
        action=action,
        workspace_id=workspace_id,
        task_id=task_id,
        expected_revision=expected_revision,
        feedback=fields.get("feedback"),
        comment=fields.get("comment"),
        jira_url=fields.get("jira_url") or None,
        operator_status=(
            None if not operator_status_text else TaskOperatorStatus(operator_status_text)
        ),
        state=state,
        wait_reason=wait_reason,
        deploy_test=fields.get("deploy_test") == "1",
        deploy_prod=fields.get("deploy_prod") == "1",
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


def _render_visibility_form(
    project_id: str,
    visibility_mode: VisibilityMode | str,
    *,
    action: str,
    compact: bool = False,
) -> str:
    mode = (
        visibility_mode
        if isinstance(visibility_mode, VisibilityMode)
        else VisibilityMode(visibility_mode)
    )
    if mode is VisibilityMode.HIDDEN:
        target = VisibilityMode.NORMAL
        label = VISIBILITY_SET_NORMAL
        hint = VISIBILITY_HINT_HIDDEN
    else:
        target = VisibilityMode.HIDDEN
        label = VISIBILITY_SET_HIDDEN
        hint = VISIBILITY_HINT_NORMAL
    hint_html = ""
    if not compact or mode is VisibilityMode.HIDDEN:
        hint_html = f'<p class="visibility-hint">{escape(hint)}</p>'
    return (
        f'<div class="visibility-box">{hint_html}'
        '<form method="post" action="'
        + escape(action, quote=True)
        + '" class="visibility-form">'
        + _hidden_input("action", "set_visibility")
        + _hidden_input("project_id", project_id)
        + _hidden_input("visibility_mode", target.value)
        + f'<button class="btn" type="submit">{escape(label)}</button></form></div>'
    )


def _render_skill_policy(
    project_id: str,
    policy: ProjectSkillPolicy,
    *,
    action: str,
    snapshot: DashboardSkillsSnapshot | None = None,
) -> str:
    return render_skill_policy(project_id, policy, action=action, snapshot=snapshot)


def _draft_marker(form_values: Mapping[str, str], name: str) -> str:
    return ' data-recovered-draft="true"' if name in form_values else ""


def _textarea_value(value: str) -> str:
    # Character references preserve an initial newline and CR across HTML parsing.
    return escape(value).replace("\r", "&#13;").replace("\n", "&#10;")


def _render_project_delete_form(
    project_id: str, *, action: str, form_values: Mapping[str, str] | None = None
) -> str:
    form_values = {} if form_values is None else form_values
    opened = " open" if "confirmation" in form_values else ""
    return (
        f'<details class="management-disclosure danger-zone"{opened}>'
        f"<summary>{escape(DELETE_PROJECT_SUMMARY)}</summary>"
        f'<p class="management-hint">{escape(DELETE_PROJECT_HINT)}</p>'
        f'<form method="post" action="{escape(action, quote=True)}" class="feedback-form">'
        + _hidden_input("action", "delete_project")
        + _hidden_input("project_id", project_id)
        + f'<label for="delete-confirm-{escape(project_id, quote=True)}">'
        + escape(DELETE_PROJECT_CONFIRM_LABEL)
        + "</label>"
        + f'<input id="delete-confirm-{escape(project_id, quote=True)}" '
        f'name="confirmation" type="text" required autocomplete="off" '
        + f'value="{escape(form_values.get("confirmation", ""), quote=True)}"'
        + _draft_marker(form_values, "confirmation")
        + " "
        f'placeholder="{escape(DELETE_PROJECT_CONFIRM_VALUE, quote=True)}">'
        + f'<button class="btn btn-danger" type="submit">{escape(DELETE_PROJECT)}</button>'
        + "</form></details>"
    )


def _render_workspace_relocation_form(
    workspace_id: str, *, action: str, form_values: Mapping[str, str] | None = None
) -> str:
    form_values = {} if form_values is None else form_values
    opened = " open" if "new_path" in form_values else ""
    return (
        f'<details class="management-disclosure"{opened}>'
        f"<summary>{escape(WORKSPACE_RELOCATION_SUMMARY)}</summary>"
        f'<p class="management-hint">{escape(WORKSPACE_RELOCATION_HINT)}</p>'
        f'<form method="post" action="{escape(action, quote=True)}" class="feedback-form">'
        + _hidden_input("action", "relocate_workspace")
        + _hidden_input("workspace_id", workspace_id)
        + f'<label for="relocate-{escape(workspace_id, quote=True)}">'
        + escape(WORKSPACE_RELOCATION_LABEL)
        + "</label>"
        + f'<input id="relocate-{escape(workspace_id, quote=True)}" name="new_path" '
        f'type="text" required maxlength="2048" autocomplete="off" '
        + f'value="{escape(form_values.get("new_path", ""), quote=True)}"'
        + _draft_marker(form_values, "new_path")
        + " "
        f'placeholder="{escape(WORKSPACE_RELOCATION_PLACEHOLDER, quote=True)}">'
        + f'<button class="btn" type="submit">{escape(WORKSPACE_RELOCATION_SUBMIT)}</button>'
        + "</form></details>"
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
    jira_url: str | None = None,
    operator_status: str | None = None,
    detailed: bool = False,
    form_values: Mapping[str, str] | None = None,
) -> str:
    form_values = {} if form_values is None else form_values
    feedback_open = " open" if "feedback" in form_values else ""
    comment_open = " open" if "comment" in form_values else ""
    jira_open = " open" if "jira_url" in form_values else ""
    forms: list[str] = []
    if state == TaskState.WAITING.value and wait_reason == TaskWaitReason.OPERATOR_REVIEW.value:
        forms.append(
            '<div class="action-row"><form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "accept")
            + f'<button class="btn btn-primary" type="submit">{escape(ACCEPT)}</button></form>'
            '<form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "cancel")
            + f'<button class="btn btn-danger" type="submit">{escape(CANCEL)}</button></form></div>'
            f'<details class="feedback-disclosure"{feedback_open}><summary>{escape(FEEDBACK_SUMMARY)}</summary>'
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "feedback")
            + f'<label for="feedback-{escape(task_id, quote=True)}">{escape(FEEDBACK_LABEL)}</label>'
            f'<textarea id="feedback-{escape(task_id, quote=True)}" name="feedback" rows="4" maxlength="1024" '
            f'required placeholder="{escape(FEEDBACK_PLACEHOLDER, quote=True)}"'
            + _draft_marker(form_values, "feedback")
            + f">{_textarea_value(form_values.get('feedback', ''))}</textarea>"
            f'<button class="btn" type="submit">{escape(FEEDBACK_SUBMIT)}</button></form></details>'
        )
    elif state in {TaskState.WORKING.value, TaskState.WAITING.value}:
        forms.append(
            '<div class="action-row"><form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "cancel")
            + f'<button class="btn btn-danger" type="submit">{escape(CANCEL_TASK)}</button></form></div>'
        )
    elif state in {TaskState.COMPLETED.value, TaskState.CANCELLED.value}:
        forms.append(
            '<div class="action-row"><form method="post" action="">'
            + _task_action_fields(workspace_id, task_id, revision, "reopen")
            + f'<button class="btn btn-primary" type="submit">{escape(REOPEN_TASK)}</button>'
            "</form></div>"
        )
    if detailed:
        selected_state = form_values.get("state", state)
        selected_reason = form_values.get("wait_reason", wait_reason or "operator_input")
        options = "".join(
            f'<option value="{value}"'
            + (" selected" if selected_state == value else "")
            + f">{escape(label)}</option>"
            for value, label in (
                ("working", OPERATOR_STATE_WORKING),
                ("waiting", "Отложить"),
                ("completed", "Принять и завершить"),
                ("cancelled", "Отменить"),
            )
        )
        reasons = "".join(
            f'<option value="{value}"'
            + (" selected" if selected_reason == value else "")
            + f">{escape(label)}</option>"
            for value, label in (
                ("operator_input", "Решение оператора"),
                ("operator_review", "Приёмка оператором"),
                ("external", "Внешняя зависимость"),
            )
        )
        forms.append(
            '<details class="feedback-disclosure"'
            + (" open" if "state" in form_values or "wait_reason" in form_values else "")
            + "><summary>Изменить состояние</summary>"
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "set_state")
            + '<label>Состояние задачи<select name="state"'
            + _draft_marker(form_values, "state")
            + ">"
            + options
            + "</select></label>"
            + '<label>Причина ожидания (для отложенной задачи)<select name="wait_reason"'
            + _draft_marker(form_values, "wait_reason")
            + ">"
            + reasons
            + "</select></label>"
            + '<button class="btn" type="submit">Сохранить состояние</button></form></details>'
        )
        forms.append(
            f'<details class="feedback-disclosure"{comment_open}><summary>{escape(COMMENT_SUMMARY)}</summary>'
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "comment")
            + f'<label for="comment-{escape(task_id, quote=True)}">{escape(COMMENT_LABEL)}</label>'
            f'<textarea id="comment-{escape(task_id, quote=True)}" name="comment" rows="4" '
            f'maxlength="2048" required placeholder="{escape(COMMENT_PLACEHOLDER, quote=True)}"'
            + _draft_marker(form_values, "comment")
            + f">{_textarea_value(form_values.get('comment', ''))}</textarea>"
            f'<button class="btn" type="submit">{escape(COMMENT_SUBMIT)}</button></form></details>'
        )
        forms.append(
            f'<details class="feedback-disclosure"{jira_open}><summary>{escape(JIRA)}</summary>'
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "set_jira")
            + f'<label for="jira-{escape(task_id, quote=True)}">{escape(JIRA_LABEL)}</label>'
            f'<input id="jira-{escape(task_id, quote=True)}" name="jira_url" type="url" '
            f'maxlength="2048" value="{escape(form_values.get("jira_url", jira_url or ""), quote=True)}" '
            + _draft_marker(form_values, "jira_url")
            + " "
            f'placeholder="{escape(JIRA_PLACEHOLDER, quote=True)}">'
            f'<button class="btn" type="submit">{escape(JIRA_SAVE)}</button></form>'
            + (
                '<form method="post" action="" class="action-row">'
                + _task_action_fields(workspace_id, task_id, revision, "set_jira")
                + '<input type="hidden" name="jira_url" value="">'
                + f'<button class="btn" type="submit">{escape(JIRA_CLEAR)}</button></form>'
                if jira_url is not None
                else ""
            )
            + "</details>"
        )
        checkboxes = []
        for name, marker, label in (
            ("deploy_test", TaskOperatorStatus.DEPLOY_TEST.value, OPERATOR_STATUS_DEPLOY_TEST),
            ("deploy_prod", TaskOperatorStatus.DEPLOY_PROD.value, OPERATOR_STATUS_DEPLOY_PROD),
        ):
            checked = (
                form_values[name] == "1"
                if name in form_values
                else operator_status in {marker, TaskOperatorStatus.DEPLOY_BOTH.value}
            )
            checkboxes.append(
                f'<label class="checkbox-row"><input type="checkbox" name="{name}" value="1"'
                + (" checked" if checked else "")
                + _draft_marker(form_values, name)
                + f"> {escape(label)}</label>"
            )
        forms.append(
            '<details class="feedback-disclosure"'
            + (" open" if "deploy_test" in form_values or "deploy_prod" in form_values else "")
            + "><summary>Отметки деплоя</summary>"
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "set_deployment")
            + "".join(checkboxes)
            + '<button class="btn" type="submit">Сохранить отметки</button></form></details>'
        )
        forms.append(
            '<details class="feedback-disclosure"><summary>Удалить задачу</summary>'
            "<p>Задача, её история и созданные в ней знания будут удалены навсегда. "
            "Файлы репозитория сохранятся.</p>"
            '<form method="post" action="" class="feedback-form">'
            + _task_action_fields(workspace_id, task_id, revision, "delete_task")
            + '<label class="checkbox-row"><input type="checkbox" name="confirmation" value="'
            + escape(task_id, quote=True)
            + '" required> Подтверждаю удаление задачи и её знаний</label>'
            + '<button class="btn btn-danger" type="submit">Удалить навсегда</button></form></details>'
        )
    return '<div class="action-panel">' + "".join(forms) + "</div>" if forms else ""


_RECOVERABLE_TASK_FIELDS = {
    "accept": None,
    "feedback": "feedback",
    "cancel": None,
    "reopen": None,
    "comment": "comment",
    "set_jira": "jira_url",
    "set_operator_status": "operator_status",
    "set_state": "state",
    "set_deployment": "deploy_test",
    "delete_task": "confirmation",
}


def _recoverable_form_fields(payload: bytes) -> dict[str, str]:
    """Recover only singular, known form schemas, never arbitrary failed request fields."""
    try:
        fields = _parse_dashboard_form_fields(payload)
    except TaskValidationError:
        return {}
    action = fields.get("action", "")
    if action in _RECOVERABLE_TASK_FIELDS:
        expected = {"action", "workspace_id", "task_id", "expected_revision"}
        editable = _RECOVERABLE_TASK_FIELDS[action]
        if editable is not None:
            expected.add(editable)
        if action == "set_state":
            expected.add("wait_reason")
        if action == "set_deployment":
            expected.add("deploy_prod")
            fields.setdefault("deploy_test", "0")
            fields.setdefault("deploy_prod", "0")
        revision = fields.get("expected_revision", "")
        if not revision.isascii() or not revision.isdigit() or not revision.strip("0"):
            return {}
    else:
        expected = {
            "relocate_workspace": {"action", "workspace_id", "new_path"},
            "delete_project": {"action", "project_id", "confirmation"},
            "set_visibility": {"action", "project_id", "visibility_mode"},
            "set_skill_scope": {"action", "project_id", "facet", "mode"},
        }.get(action, set())
    if set(fields) != expected:
        return {}
    if any(
        not value or len(value) > 128 or "\x00" in value
        for name, value in fields.items()
        if name in {"task_id", "workspace_id", "project_id"}
    ):
        return {}
    return fields


def _recovery_target_on_page(
    page: _DashboardPageRequest, *, workspace_id: str, project_id: str, task_id: str | None = None
) -> bool:
    return (
        page.kind == "projects"
        or (page.kind == "task" and page.identity == task_id)
        or (page.kind == "workspace" and page.identity == workspace_id)
        or (page.kind == "project" and page.identity == project_id)
    )


def _render_recovery_controls(
    database_path: Path, page: _DashboardPageRequest, fields: dict[str, str]
) -> str:
    action = fields.get("action", "")
    if action in _RECOVERABLE_TASK_FIELDS:
        if page.kind == "task" and page.identity != fields["task_id"]:
            return ""
        detail = read_dashboard_task_detail(database_path, fields["task_id"])
        task = detail.task
        if fields["workspace_id"] != task.workspace_id or not _recovery_target_on_page(
            page,
            workspace_id=task.workspace_id,
            project_id=detail.workspace.project_id,
            task_id=task.task_id,
        ):
            return ""
        reviewing = (
            task.state is TaskState.WAITING and task.wait_reason is TaskWaitReason.OPERATOR_REVIEW
        )
        terminal = task.state in {TaskState.COMPLETED, TaskState.CANCELLED}
        if (
            (action == "feedback" and not reviewing)
            or (action == "cancel" and terminal)
            or (action == "reopen" and not terminal)
            or (
                action == "set_operator_status"
                and fields["operator_status"]
                not in {
                    "",
                    TaskOperatorStatus.DEPLOY_TEST.value,
                    TaskOperatorStatus.DEPLOY_PROD.value,
                }
            )
        ):
            return ""
        last_summary = "" if detail.latest_checkpoint is None else detail.latest_checkpoint.summary
        return (
            f"<h2>{escape(task.title)}</h2><p>{_state_pill(task.state.value, None if task.wait_reason is None else task.wait_reason.value)} "
            f"{escape(REVISION)} {task.revision}</p><p>{escape(last_summary)}</p>"
            + _render_latest_verification(detail)
            + _render_task_actions(
                workspace_id=task.workspace_id,
                task_id=task.task_id,
                state=task.state.value,
                wait_reason=None if task.wait_reason is None else task.wait_reason.value,
                revision=task.revision,
                jira_url=task.jira_url,
                operator_status=None
                if task.operator_status is None
                else task.operator_status.value,
                detailed=True,
                form_values=fields,
            )
        )
    if action == "relocate_workspace":
        if page.kind != "workspace" or page.identity != fields["workspace_id"]:
            return ""
        connection = connect_database(database_path)
        try:
            workspace = get_workspace(connection, fields["workspace_id"])
        finally:
            connection.close()
        return _render_workspace_relocation_form(
            workspace.workspace_id, action=page.redirect_target, form_values=fields
        )
    if action in {"delete_project", "set_skill_scope", "set_visibility"}:
        connection = connect_database(database_path)
        try:
            project = get_project(connection, fields["project_id"])
            if page.kind == "workspace" and action == "set_visibility":
                if (
                    page.identity is None
                    or get_workspace(connection, page.identity).project_id != project.project_id
                ):
                    return ""
            elif page.kind != "project" or page.identity != project.project_id:
                return ""
        finally:
            connection.close()
        if action == "delete_project":
            return _render_project_delete_form(
                project.project_id, action=page.redirect_target, form_values=fields
            )
        if action == "set_visibility":
            return _render_visibility_form(
                project.project_id, project.visibility_mode, action=page.redirect_target
            )
        project_detail = read_dashboard_project_detail(
            database_path, project.project_id, include_skills=True
        )
        return _render_skill_policy(
            project.project_id,
            project_detail.skill_policy,
            action=page.redirect_target,
            snapshot=project_detail.skills,
        )
    return ""


def _render_dashboard_form_error(
    database_path: Path,
    base_path: str,
    page: _DashboardPageRequest,
    *,
    status: int,
    payload: bytes = b"",
) -> str:
    fields = _recoverable_form_fields(payload) if payload else {}
    controls = ""
    if fields:
        try:
            controls = _render_recovery_controls(database_path, page, fields)
        except (
            OSError,
            sqlite3.DatabaseError,
            DatabaseError,
            TaskError,
            RegistryError,
            ProjectSkillPolicyError,
            DashboardError,
            TaskCheckpointError,
            VerificationError,
        ):
            # The original status and a copyable draft remain useful even if the target disappeared.
            pass
    values = [
        fields[name]
        for name in (
            "feedback",
            "comment",
            "jira_url",
            "operator_status",
            "state",
            "wait_reason",
            "deploy_test",
            "deploy_prod",
            "new_path",
            "confirmation",
        )
        if name in fields
    ]
    message = FORM_ERROR_CONFLICT if status == 409 else FORM_ERROR_INVALID
    content = (
        '<section class="panel form-recovery"><div class="panel-body">'
        f'<div class="form-error" role="alert"><h1>{escape(FORM_ERROR_TITLE)}</h1>'
        f"<p>{escape(message)}</p></div>"
    )
    if values:
        content += f"<p>{escape(FORM_DRAFT_SAVED if controls else FORM_DRAFT_UNAVAILABLE)}</p>"
    content += controls
    if values and not controls:
        content += (
            f'<label for="recovery-draft">{escape(FORM_DRAFT_LABEL)}</label>'
            '<textarea id="recovery-draft" class="recovery-draft" rows="6" readonly '
            'data-recovered-draft="true">' + _textarea_value("\n".join(values)) + "</textarea>"
        )
    content += (
        f'<p><a class="btn" href="{escape(page.redirect_target, quote=True)}">'
        f"{escape(FORM_ERROR_BACK)}</a></p></div></section>"
    )
    navigation_rows = None
    try:
        navigation_rows = _read_dashboard_navigation_rows(database_path)
    except (
        OSError,
        sqlite3.DatabaseError,
        DatabaseError,
        TaskError,
        RegistryError,
        DashboardError,
    ):
        pass
    project_id = page.identity if page.kind == "project" else None
    workspace_id = page.identity if page.kind == "workspace" else None
    task_id = page.identity if page.kind == "task" else None
    if task_id is not None:
        try:
            connection = connect_database(database_path)
            try:
                workspace_id = get_task(connection, task_id).workspace_id
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError, DatabaseError, TaskError):
            pass
    for row in navigation_rows or ():
        if row.workspace_id == workspace_id:
            project_id = row.project_id
            break
    return _render_shell(
        base_path=base_path,
        page_title=document_title(FORM_ERROR_TITLE),
        breadcrumbs=((BREADCRUMB_PROJECTS, base_path), (FORM_ERROR_TITLE, None)),
        events_url="",
        content=content,
        navigation_rows=navigation_rows,
        current_project_id=project_id,
        current_workspace_id=workspace_id,
        current_task_id=task_id,
        current_section="settings" if page.settings else "tasks" if workspace_id else "overview",
    )


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
    page: int = 1,
) -> str:
    params: list[tuple[str, str]] = [("view", view), ("snapshot", snapshot)]
    if identity is not None:
        params.append(("project_id" if view == "project_settings" else f"{view}_id", identity))
    if search_query is not None:
        params.append(("q", search_query))
    if page != 1:
        params.append(("page", str(page)))
    return f"{base_path}events?{urlencode(params)}"


def _project_display_name(rows: tuple[DashboardWorkspaceRow, ...], project_id: str) -> str:
    project_rows = tuple(row for row in rows if row.project_id == project_id)
    if not project_rows:
        return project_crumb(project_id)
    common_dir = project_rows[0].git_common_dir
    candidate = common_dir.parent.name if common_dir.name == ".git" else common_dir.name
    return candidate.removesuffix(".git") or project_crumb(project_id)


def _attention_workspace(rows: tuple[DashboardWorkspaceRow, ...]) -> DashboardWorkspaceRow:
    def rank(row: DashboardWorkspaceRow) -> tuple[int, str]:
        if (
            row.task_state == TaskState.WAITING.value
            and row.task_wait_reason == TaskWaitReason.OPERATOR_REVIEW.value
        ):
            priority = 0
        elif row.task_state == TaskState.WORKING.value:
            priority = 1
        elif row.task_state == TaskState.WAITING.value:
            priority = 2
        elif row.task_id is not None:
            priority = 3
        else:
            priority = 4
        return (priority, row.workspace_id)

    return min(rows, key=rank)


def _group_navigation_rows(
    rows: tuple[DashboardWorkspaceRow, ...],
) -> tuple[tuple[str, tuple[DashboardWorkspaceRow, ...]], ...]:
    grouped: dict[str, list[DashboardWorkspaceRow]] = {}
    for row in rows:
        grouped.setdefault(row.project_id, []).append(row)
    return tuple((project_id, tuple(project_rows)) for project_id, project_rows in grouped.items())


def _render_project_navigation(
    rows: tuple[DashboardWorkspaceRow, ...] | None,
    *,
    base_path: str,
    current_project_id: str | None,
    current_workspace_id: str | None,
    current_task_id: str | None,
    current_section: str,
) -> str:
    overview_current = (
        current_project_id is None
        and current_workspace_id is None
        and current_section == "overview"
    )
    parts = [
        f'<nav class="project-navigation" aria-label="{escape(PROJECTS_NAV, quote=True)}">',
        f'<a class="overview-link{" is-current" if overview_current else ""}" '
        f'href="{escape(base_path, quote=True)}"'
        + (' aria-current="page"' if overview_current else "")
        + f'><span class="nav-overview-icon" aria-hidden="true">⌂</span><span>{escape(ALL_PROJECTS)}</span></a>',
        f'<a class="overview-link{" is-current" if current_project_id == "all" else ""}" '
        f'href="{base_path}vault/all/"'
        + (' aria-current="page"' if current_project_id == "all" else "")
        + ">Личное хранилище</a>",
        f'<p class="nav-label">{escape(PROJECTS_NAV)}</p>',
    ]
    if rows is None:
        parts.append(f'<p class="nav-empty">{escape(NAVIGATION_UNAVAILABLE)}</p></nav>')
        return "".join(parts)
    for project_id, project_rows in _group_navigation_rows(rows):
        project_current = project_id == current_project_id or (
            current_workspace_id is not None
            and any(item.workspace_id == current_workspace_id for item in project_rows)
        )
        workspace_url = _url(base_path, "projects", project_id)
        project_name = _project_display_name(rows, project_id)
        link_current = project_current and current_section == "overview"
        review_count = sum(item.review_task_count for item in project_rows)
        parts.append(
            f'<section class="nav-project{" is-context" if project_current else ""}">'
            f'<a class="nav-project-link" href="{escape(workspace_url, quote=True)}"'
            + (' aria-current="page"' if link_current else "")
            + f'><span class="nav-project-name">{escape(project_name)}</span>'
            + (
                f'<span class="nav-project-id">{review_count} на проверке</span>'
                if review_count
                else ""
            )
            + "</a>"
            "</section>"
        )
    if not rows:
        parts.append(f'<p class="nav-empty">{escape(EMPTY_WORKSPACES_TITLE)}</p>')
    parts.append("</nav>")
    return "".join(parts)


def _render_shell(
    *,
    base_path: str,
    page_title: str,
    breadcrumbs: tuple[tuple[str, str | None], ...],
    events_url: str,
    content: str,
    navigation_rows: tuple[DashboardWorkspaceRow, ...] | None = (),
    current_project_id: str | None = None,
    current_workspace_id: str | None = None,
    current_task_id: str | None = None,
    interactive: bool = True,
    current_section: str = "overview",
) -> str:
    breadcrumb_html: list[str] = []
    for label, href in breadcrumbs:
        if href is None:
            breadcrumb_html.append(f'<li aria-current="page"><span>{escape(label)}</span></li>')
        else:
            breadcrumb_html.append(
                f'<li><a href="{escape(href, quote=True)}">{escape(label)}</a></li>'
            )
    navigation = _render_project_navigation(
        navigation_rows,
        base_path=base_path,
        current_project_id=current_project_id,
        current_workspace_id=current_workspace_id,
        current_task_id=current_task_id,
        current_section=current_section,
    )
    project_navigation = ""
    if current_project_id is not None and current_project_id != "all":
        project_navigation = _render_project_tabs(
            current_project_id,
            tuple(row for row in navigation_rows or () if row.project_id == current_project_id),
            base_path,
            section=current_section,
            workspace_id=current_workspace_id,
            task_id=current_task_id,
        )
    css_url = f"{base_path}assets/dashboard.css"
    js_url = f"{base_path}assets/dashboard.js"
    live_state = "reconnecting" if events_url else "manual"
    live_copy = LIVE_CONNECTING if events_url else LIVE_MANUAL
    refresh_control = (
        f'<button class="update-link" type="button" data-refresh-now="true">{escape(LIVE_REFRESH)}</button>'
        if interactive
        else ""
    )
    if not interactive:
        live_copy = "Личное хранилище"
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(page_title)}</title>"
        f'<link rel="stylesheet" href="{escape(css_url, quote=True)}">'
        "</head>"
        f'<body data-events-url="{escape(events_url, quote=True)}">'
        f'<a class="skip-link" href="#main">{escape(SKIP_TO_CONTENT)}</a>'
        '<div class="app-layout"><aside class="app-sidebar">'
        f'<a class="brand" href="{escape(base_path, quote=True)}">'
        f'<span class="brand-mark" aria-hidden="true">H</span><span class="brand-copy">'
        f"<strong>{escape(BRAND)}</strong><small>{escape(WORKSPACE_HOME)}</small></span></a>"
        f"{navigation}"
        '<div class="sidebar-footer">'
        f'<span class="live-indicator" data-live-indicator data-state="{live_state}">'
        '<span class="live-dot" aria-hidden="true"></span>'
        f'<span class="live-copy" data-live-copy>{escape(live_copy)}</span>'
        f"{refresh_control}"
        '</span></div></aside><div class="app-stage"><header class="context-header">'
        f'<details class="mobile-navigation"><summary aria-label="{escape(OPEN_NAVIGATION, quote=True)}">'
        f'<span class="brand-mark" aria-hidden="true">H</span><span>{escape(OPEN_NAVIGATION)}</span>'
        '<span class="mobile-chevron" aria-hidden="true">⌄</span></summary>'
        f'<div class="mobile-navigation-panel">{navigation}</div></details>'
        f'<nav class="breadcrumbs" aria-label="{escape(NAVIGATION, quote=True)}"><ol>'
        f"{''.join(breadcrumb_html)}</ol></nav>"
        '<span class="header-live-indicator live-indicator" data-header-live-indicator '
        f'data-state="{live_state}"><span class="live-dot" aria-hidden="true"></span>'
        f'<span class="live-copy">{escape(live_copy)}</span>'
        f"{refresh_control}</span>"
        '</header><main id="main"><div class="content-frame">'
        f"{project_navigation}{content}</div></main></div></div>"
        + (f'<script defer src="{escape(js_url, quote=True)}"></script>' if interactive else "")
        + "</body></html>"
    )


def _render_navigation_error(title: str, message: str) -> str:
    return _render_shell(
        base_path="/",
        page_title=document_title(title),
        breadcrumbs=((BREADCRUMB_PROJECTS, "/"), (title, None)),
        events_url="",
        content=(
            '<section class="panel"><div class="panel-body">'
            f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
            f'<a class="btn btn-primary" href="/">{escape(ALL_PROJECTS)}</a>'
            '<a class="btn" href="/vault/all/">Личное хранилище</a></div></section>'
        ),
        navigation_rows=None,
        current_section="error",
    )


def _render_metrics(rows: tuple[DashboardWorkspaceRow, ...]) -> str:
    project_count = len({row.project_id for row in rows})
    active_count = sum(row.active_task_count for row in rows)
    review_count = sum(row.review_task_count for row in rows)
    metrics = (
        (METRIC_PROJECTS, project_count),
        (METRIC_ACTIVE, active_count),
        (METRIC_REVIEW, review_count),
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
    operator_marker = (
        ""
        if row.task_operator_status is None
        else f'<span class="task-marker">{escape(operator_status_label(row.task_operator_status))}</span>'
    )
    jira_link = (
        ""
        if row.task_jira_url is None
        else f'<a class="task-jira" href="{escape(row.task_jira_url, quote=True)}" '
        f'target="_blank" rel="noreferrer noopener">{escape(JIRA)}</a>'
    )
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
            jira_url=row.task_jira_url,
            operator_status=row.task_operator_status,
        )
    return (
        f'<article class="workspace-card" data-state="{escape(row.task_state or "idle", quote=True)}">'
        '<div class="workspace-card-main"><header class="workspace-card-head"><div>'
        f'<p class="workspace-card-label">{escape(WORKSPACE_OVERVIEW)}</p>'
        f'<h3 class="workspace-name"><a href="{escape(workspace_url, quote=True)}">'
        f"{escape(row.workspace_root.name or str(row.workspace_root))}</a></h3>"
        f'<p class="workspace-path">{escape(str(row.workspace_root))}</p></div>'
        f"{_state_pill(row.task_state, row.task_wait_reason)}</header>"
        f'<div class="task-focus"><div class="task-focus-head"><span class="task-focus-label">{escape(TASK_FOCUS)}</span>'
        f'<span class="task-focus-links">{operator_marker}{jira_link}</span></div>'
        f'<p class="task-focus-title">{focus}</p>{task_branch}'
        + (
            f'<div class="next-step"><span>{escape(NEXT_STEP)}</span><p>{escape(row.next_step)}</p></div>'
            if row.next_step is not None
            else ""
        )
        + '</div><a class="text-link" href="'
        + escape(workspace_url, quote=True)
        + f'">{escape(OPEN_WORKSPACE)} <span aria-hidden="true">→</span></a></div>'
        '<aside class="workspace-card-side"><div class="mini-stats">'
        f'<div class="mini-stat"><span>{escape(BRANCH)}</span><strong>{escape(live_branch)}</strong></div>'
        f'<div class="mini-stat"><span>{escape(DIRTY)}</span><strong>{escape(live_dirty)}</strong></div>'
        f'<div class="mini-stat"><span>{escape(INDEX)}</span><strong>{row.indexed_file_count}</strong></div>'
        f'<div class="mini-stat"><span>{escape(MODE)}</span>'
        f"<strong>{escape(visibility_label(row.visibility_mode))}</strong></div>"
        f"</div>{actions}</aside></article>"
    )


def _render_project_hub_cards(rows: tuple[DashboardWorkspaceRow, ...], base_path: str) -> str:
    cards = []
    for project_id, project_rows in _group_navigation_rows(rows):
        name = _project_display_name(rows, project_id)
        workspace = _attention_workspace(project_rows)
        cards.append(
            '<article class="hub-card"><div class="hub-card-heading">'
            f'<span class="hub-monogram" aria-hidden="true">{escape(name[:1].upper())}</span>'
            f"{_state_pill(workspace.task_state, workspace.task_wait_reason)}</div>"
            f'<h2><a href="{_url(base_path, "projects", project_id)}">{escape(name)}</a></h2>'
            + (
                '<p class="hub-focus"><a href="'
                + _url(base_path, "tasks", workspace.task_id)
                + '">'
                + escape(workspace.task_title or NO_TASK)
                + "</a></p>"
                if workspace.task_id
                else f'<p class="hub-focus">{escape(NO_TASK)}</p>'
            )
            + (
                f'<p class="hub-next">{escape(workspace.next_step)}</p>'
                if workspace.next_step
                else ""
            )
            + '<div class="hub-counts">'
            f"<span>Активных: {sum(row.active_task_count for row in project_rows)}</span>"
            f"<span>{sum(row.review_task_count for row in project_rows)} на проверке</span></div>"
            '<footer class="hub-links">'
            f'<a href="{_url(base_path, "workspaces", workspace.workspace_id)}">Задачи →</a>'
            f'<a href="{_url(base_path, "vault", project_id)}">Заметки и доступы →</a>'
            "</footer></article>"
        )
    return '<section class="hub-grid" aria-label="Проекты">' + "".join(cards) + "</section>"


def _render_project_tabs(
    project_id: str,
    rows: tuple[DashboardWorkspaceRow, ...],
    base_path: str,
    *,
    section: str,
    workspace_id: str | None = None,
    task_id: str | None = None,
) -> str:
    if workspace_id is None and rows:
        workspace_id = _attention_workspace(rows).workspace_id
    project_url = _url(base_path, "projects", project_id)
    vault_url = _url(base_path, "vault", project_id)
    context = {}
    if workspace_id is not None:
        context["workspace"] = workspace_id
    if task_id is not None:
        context["task"] = task_id
    if context:
        vault_url += "?" + urlencode(context)
    links = [("overview", "Обзор", project_url)]
    if workspace_id is not None:
        links.append(("tasks", "Задачи", _url(base_path, "workspaces", workspace_id)))
    links.extend(
        (
            ("vault", "Заметки и доступы", vault_url),
            ("settings", "Настройки", project_url + "settings/"),
        )
    )
    tabs = "".join(
        f'<a href="{escape(href, quote=True)}"'
        + (
            ' aria-current="page"'
            if key == section and not (key == "tasks" and task_id)
            else ' aria-current="true"'
            if key == section
            else ""
        )
        + f">{label}</a>"
        for key, label, href in links
    )
    return_task = (
        f'<a class="return-task" href="{_url(base_path, "tasks", task_id)}">Вернуться к задаче</a>'
        if section == "vault" and task_id
        else ""
    )
    return '<nav class="project-tabs" aria-label="Разделы проекта">' + tabs + return_task + "</nav>"


def render_projects_page(home: DashboardHomePage, *, base_path: str = "/") -> str:
    """Project hub with direct navigation and the existing global Task search/history."""
    rows = home.workspaces
    content = (
        '<section class="page-intro"><div>'
        f'<p class="eyebrow">{escape(PAGE_PROJECTS)}</p>'
        "<h1>Мои проекты</h1>"
        '<p class="hero-copy">Задачи, заметки и доступы — всё под рукой.</p></div></section>'
        + _render_metrics(rows)
        + _render_project_hub_cards(rows, base_path)
        + '<section class="panel search-panel"><div class="panel-head"><div>'
        f'<p class="panel-kicker">{escape(SEARCH_SECTION)}</p><h2>{escape(HOME_SEARCH_LABEL)}</h2>'
        '</div><span class="search-shortcut" aria-hidden="true">/</span></div><div class="panel-body">'
        + _render_search(
            query=home.search_query or "",
            submitted=home.search_query is not None,
            task_results=home.task_search_results,
            placeholder=HOME_SEARCH_PLACEHOLDER,
            label=HOME_SEARCH_LABEL,
            action=base_path,
            base_path=base_path,
        )
        + '</div></section><section class="panel" id="history"><div class="panel-head"><div>'
        f"<h2>{escape(RECENT_TASKS_HOME)}</h2></div></div>"
        '<div class="panel-body">'
        + (
            f'<div class="empty-state"><strong>{escape(EMPTY_WORKSPACES_TITLE)}</strong>'
            f"<span>{escape(EMPTY_WORKSPACES_HINT)}</span></div>"
            if not rows
            else _render_recent_tasks(
                home.recent_tasks,
                base_path,
                navigation_rows=rows,
                show_project=True,
            )
        )
        + _render_history_pagination(
            base_path,
            page=home.page,
            total=home.task_count,
            page_size=_DASHBOARD_RECENT_TASK_LIMIT,
            search_query=home.search_query,
        )
        + "</div></section>"
    )
    if not rows:
        content = (
            '<section class="page-intro"><h1>Мои проекты</h1></section>'
            '<section class="panel"><div class="panel-body empty-state">'
            f"<h2>{escape(EMPTY_WORKSPACES_TITLE)}</h2><p>{escape(EMPTY_WORKSPACES_HINT)}</p>"
            f'<a class="btn" href="{base_path}vault/all/">Открыть личное хранилище</a>'
            "</div></section>"
        )
    return _render_shell(
        base_path=base_path,
        page_title=document_title(PAGE_PROJECTS),
        breadcrumbs=((BREADCRUMB_PROJECTS, None),),
        events_url=_events_url(
            base_path,
            view="projects",
            search_query=home.search_query,
            page=home.page,
            snapshot=_snapshot_fingerprint(_fingerprint_home(home)),
        ),
        content=content,
        navigation_rows=rows,
    )


def render_project_page(
    detail: DashboardProjectDetail,
    *,
    base_path: str,
    navigation_rows: tuple[DashboardWorkspaceRow, ...] | None = None,
    settings: bool = False,
) -> str:
    rows = detail.workspaces
    project_id = detail.project.project_id
    nav_rows = rows if navigation_rows is None else navigation_rows
    project_name = _project_display_name(nav_rows, project_id)
    project_url = _url(base_path, "projects", project_id)
    visibility_action = project_url + "settings/"
    workspace_html = (
        '<section class="project-section"><header class="project-section-head">'
        f'<div><p class="project-kicker">{escape(SECTION_WORKSPACES)}</p>'
        f'<h2 class="project-title">{escape(workspace_count_label(len(rows)))}</h2></div></header>'
        '<div class="workspace-list">'
        + "".join(_render_workspace_card(row, base_path) for row in rows)
        + "</div></section>"
        if rows
        else (
            f'<div class="empty-state"><strong>{escape(EMPTY_PROJECT_WORKSPACES_TITLE)}</strong>'
            f"<span>{escape(EMPTY_PROJECT_WORKSPACES_HINT)}</span></div>"
        )
    )
    content = (
        '<section class="page-intro compact"><div>'
        f'<p class="eyebrow">{escape(PROJECT_OVERVIEW)}</p>'
        f"<h1>{escape(project_name)}</h1></div>"
        '<div class="project-counts">'
        f"<span>Активные задачи: {sum(row.active_task_count for row in rows)}</span>"
        f"<span>Ожидают проверки: {sum(row.review_task_count for row in rows)}</span>"
        "</div></section>"
        + workspace_html
        + f'<p class="legacy-settings-link" id="skill-scope"><a href="{visibility_action}#skill-scope">'
        "Области разработки и настройки проекта</a></p>"
    )
    if settings:
        content = (
            '<section class="page-intro compact"><div><p class="eyebrow">'
            f"{escape(project_name)}</p><h1>Настройки проекта</h1></div></section>"
            '<section class="panel"><div class="panel-head"><h2>Режим проекта</h2></div>'
            '<div class="panel-body">'
            + _render_visibility_form(
                project_id, detail.project.visibility_mode, action=visibility_action
            )
            + "</div></section>"
            + _render_skill_policy(
                project_id, detail.skill_policy, action=visibility_action, snapshot=detail.skills
            )
            + '<section class="panel"><div class="panel-head"><h2>Папки проекта</h2></div><div class="panel-body">'
            + "".join(
                '<section class="workspace-setting">'
                f'<h3><a href="{_url(base_path, "workspaces", row.workspace_id)}">'
                f"{escape(str(row.workspace_root))}</a></h3>"
                + _render_workspace_relocation_form(
                    row.workspace_id, action=_url(base_path, "workspaces", row.workspace_id)
                )
                + "</section>"
                for row in rows
            )
            + '</div></section><section class="panel"><div class="panel-body">'
            f'<p class="section-note">ID проекта: {escape(project_id)}</p>'
            + _render_project_delete_form(project_id, action=visibility_action)
            + "</div></section>"
        )
    return _render_shell(
        base_path=base_path,
        page_title=document_title(f"Настройки · {project_name}" if settings else project_name),
        breadcrumbs=(
            ((BREADCRUMB_PROJECTS, base_path), (project_name, project_url), ("Настройки", None))
            if settings
            else ((BREADCRUMB_PROJECTS, base_path), (project_name, None))
        ),
        events_url=_events_url(
            base_path,
            view="project_settings" if settings else "project",
            identity=project_id,
            snapshot=_snapshot_fingerprint(
                detail if navigation_rows is None else (detail, navigation_rows)
            ),
        ),
        content=content,
        navigation_rows=nav_rows,
        current_project_id=project_id,
        current_section="settings" if settings else "overview",
    )


def _render_recent_tasks(
    tasks: tuple[DashboardTaskRow, ...],
    base_path: str,
    *,
    navigation_rows: tuple[DashboardWorkspaceRow, ...] = (),
    show_project: bool = False,
) -> str:
    if not tasks:
        return f'<div class="empty-state"><strong>{escape(NO_TASKS_TITLE)}</strong></div>'
    parts = ['<div class="task-list">']
    for row in tasks:
        task = row.task
        task_url = _url(base_path, "tasks", task.task_id)
        wait_reason = None if task.wait_reason is None else task.wait_reason.value
        operator_status = (
            ""
            if task.operator_status is None
            else f'<span class="task-marker">{escape(operator_status_label(task.operator_status.value))}</span>'
        )
        jira = (
            ""
            if task.jira_url is None
            else f'<a class="task-jira" href="{escape(task.jira_url, quote=True)}" '
            f'target="_blank" rel="noreferrer noopener">{escape(JIRA)}</a>'
        )
        project_html = ""
        if show_project:
            project_url = _url(base_path, "projects", row.project_id)
            project_name = _project_display_name(navigation_rows, row.project_id)
            project_html = (
                f'<span><a href="{escape(project_url, quote=True)}">'
                f"{escape(project_name)}</a></span>"
            )
        parts.append(
            f'<article class="task-row" data-state="{escape(task.state.value, quote=True)}"><div>'
            f'<p class="task-row-title"><a href="{escape(task_url, quote=True)}">{escape(task.title)}</a></p>'
            '<div class="task-row-meta">'
            f"{project_html}"
            f'<span class="mono">{escape(task.task_id[:10])}</span>'
            f"<span>{escape(REVISION)} {task.revision}</span>"
            f'<span class="task-git-branch">{escape(BRANCH)} '
            f'<strong class="mono">{escape(_display_recorded_branch(row.git_branch))}</strong></span>'
            f"<span>{escape(task.updated_at)}</span>{operator_status}{jira}</div></div>"
            f'<div class="task-row-aside">{_state_pill(task.state.value, wait_reason)}'
            f'<span class="row-arrow" aria-hidden="true">→</span></div></article>'
        )
    parts.append("</div>")
    return "".join(parts)


def _render_history_pagination(
    url: str,
    *,
    page: int,
    total: int,
    page_size: int,
    search_query: str | None = None,
    anchor: str = "history",
) -> str:
    pages = max(1, (total + page_size - 1) // page_size)
    if pages == 1:
        return ""

    def link(target: int, label: str, relation: str) -> str:
        params = [] if search_query is None else [("q", search_query)]
        params.append(("page", str(target)))
        href = f"{url}?{urlencode(params)}#{anchor}"
        return (
            f'<a class="btn" rel="{relation}" href="{escape(href, quote=True)}">{escape(label)}</a>'
        )

    previous = link(page - 1, "Предыдущая", "prev") if page > 1 else ""
    following = link(page + 1, "Следующая", "next") if page < pages else ""
    return (
        '<nav class="action-row" aria-label="Страницы истории">'
        + previous
        + f'<span class="section-note">Страница {page} из {pages} · Записей: {total}</span>'
        + following
        + "</nav>"
    )


def _render_search(
    *,
    query: str,
    submitted: bool,
    task_results: tuple[ProjectSearchHit, ...],
    placeholder: str,
    label: str,
    action: str | None = None,
    base_path: str,
) -> str:
    result_html = ""
    if submitted:
        if task_results:
            hits = []
            for task_hit in task_results:
                task_id = task_hit.ref.removeprefix("task:").partition("#")[0]
                task_url = _url(base_path, "tasks", task_id)
                summary = "" if task_hit.short_summary is None else f" · {task_hit.short_summary}"
                hits.append(
                    '<div class="search-hit"><div class="search-hit-path">'
                    f'<a href="{escape(task_url, quote=True)}">{escape(task_hit.title)}</a>'
                    f'<div class="section-note">{escape(task_hit.location)}{escape(summary)}</div>'
                    '</div><div class="search-hit-meta">задача · '
                    + escape(task_hit.match_reason)
                    + "</div></div>"
                )
            result_html = (
                '<div class="search-results" aria-live="polite">' + "".join(hits) + "</div>"
            )
        else:
            result_html = (
                f'<div class="empty-state"><strong>{escape(NO_SEARCH_HITS_TITLE)}</strong></div>'
            )
    action_attr = "" if action is None else f' action="{escape(action, quote=True)}"'
    return (
        f'<form method="get" class="search-box" role="search"{action_attr}>'
        f'<input class="search-input" type="search" name="q" value="{escape(query, quote=True)}" '
        f'maxlength="256" placeholder="{escape(placeholder, quote=True)}" '
        f'aria-label="{escape(label, quote=True)}">'
        f'<button class="btn btn-primary" type="submit">{escape(SEARCH)}</button></form>'
        + result_html
    )


def _render_workspace_current_task(
    row: DashboardWorkspaceRow,
    *,
    base_path: str,
    actions: str,
) -> str:
    if row.live_error is not None:
        return (
            '<section class="panel focus-panel"><div class="panel-body" role="status">'
            "<h2>Папка проекта недоступна</h2>"
            "<p>Проверьте доступ к папке. Если проект перемещён, укажите новый путь.</p>"
            + _render_workspace_relocation_form(
                row.workspace_id, action=_url(base_path, "workspaces", row.workspace_id)
            )
            + "</div></section>"
        )
    if row.task_id is None:
        return (
            '<section class="panel focus-panel"><div class="panel-head">'
            f'<div><p class="panel-kicker">{escape(CURRENT_TASK)}</p>'
            f"<h2>{escape(NO_TASK)}</h2></div>{_state_pill(None)}</div></section>"
        )
    task_url = _url(base_path, "tasks", row.task_id)
    branch = (
        EM_DASH if row.task_git_branch is None else _display_recorded_branch(row.task_git_branch)
    )
    next_step = (
        ""
        if row.next_step is None
        else f'<div class="next-step"><span>{escape(NEXT_STEP)}</span><p>{escape(row.next_step)}</p></div>'
    )
    markers = ""
    if row.task_operator_status is not None:
        markers += f'<span class="task-marker">{escape(operator_status_label(row.task_operator_status))}</span>'
    if row.task_jira_url is not None:
        markers += (
            f'<a class="task-jira" href="{escape(row.task_jira_url, quote=True)}" '
            f'target="_blank" rel="noreferrer noopener">{escape(JIRA)}</a>'
        )
    return (
        '<section class="panel focus-panel"><div class="panel-head"><div>'
        f'<p class="panel-kicker">{escape(CURRENT_TASK)}</p>'
        f'<h2><a href="{escape(task_url, quote=True)}">{escape(row.task_title or row.task_id)}</a></h2>'
        f"</div>{_state_pill(row.task_state, row.task_wait_reason)}</div>"
        '<div class="panel-body"><div class="task-primary-meta">'
        f'<span class="mono">{escape(row.task_id[:10])}</span>'
        f'<span>{escape(BRANCH)} <strong class="mono">{escape(branch)}</strong></span>{markers}</div>'
        f'<div class="focus-content">{next_step}<a class="text-link" href="{escape(task_url, quote=True)}">{escape(OPEN_TASK)} '
        f'<span aria-hidden="true">→</span></a></div>{actions}</div></section>'
    )


def _render_workspace_switcher(
    rows: tuple[DashboardWorkspaceRow, ...], current: DashboardWorkspaceRow, base_path: str
) -> str:
    workspaces = tuple(row for row in rows if row.project_id == current.project_id)
    if len(workspaces) < 2:
        return ""
    return (
        '<nav class="workspace-switcher" aria-label="Папки проекта"><span>Папка:</span>'
        + "".join(
            f'<a href="{_url(base_path, "workspaces", row.workspace_id)}"'
            + (' aria-current="page"' if row.workspace_id == current.workspace_id else "")
            + f' title="{escape(str(row.workspace_root), quote=True)}">'
            + escape(row.workspace_root.name)
            + "</a>"
            for row in workspaces
        )
        + "</nav>"
    )


def render_workspace_page(
    detail: DashboardWorkspaceDetail,
    *,
    base_path: str,
    navigation_rows: tuple[DashboardWorkspaceRow, ...] | None = None,
) -> str:
    row = detail.workspace
    workspace_url = _url(base_path, "workspaces", row.workspace_id)
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
            jira_url=row.task_jira_url,
            operator_status=row.task_operator_status,
        )
    workspace_name = row.workspace_root.name or WORKSPACE_FALLBACK
    project_url = _url(base_path, "projects", row.project_id)
    navigation = (row,) if navigation_rows is None else navigation_rows
    project_name = _project_display_name(navigation, row.project_id)
    content = (
        '<section class="page-intro compact"><div>'
        f'<p class="eyebrow">{escape(project_name)}</p>'
        "<h1>Задачи</h1>"
        f'<p class="hero-copy">{escape(str(row.workspace_root))}</p></div>'
        "</section>"
        + _render_workspace_switcher(navigation, row, base_path)
        + _render_workspace_current_task(row, base_path=base_path, actions=actions)
        + '<section class="panel search-panel"><div class="panel-head"><div>'
        f'<p class="panel-kicker">{escape(SEARCH_SECTION)}</p><h2>{escape(SEARCH_LABEL)}</h2>'
        '</div><span class="search-shortcut" aria-hidden="true">/</span></div><div class="panel-body">'
        + _render_search(
            query=detail.search_query or "",
            submitted=detail.search_query is not None,
            task_results=detail.task_search_results,
            placeholder=SEARCH_PLACEHOLDER,
            label=SEARCH_LABEL,
            base_path=base_path,
        )
        + '</div></section><section class="workspace-layout"><div class="workspace-main">'
        + '<section class="panel" id="history"><div class="panel-head"><div>'
        f"<h2>{escape(RECENT_TASKS)}</h2></div></div>"
        '<div class="panel-body">'
        + _render_recent_tasks(detail.recent_tasks, base_path)
        + _render_history_pagination(
            workspace_url,
            page=detail.page,
            total=detail.task_count,
            page_size=_DASHBOARD_RECENT_TASK_LIMIT,
            search_query=detail.search_query,
        )
        + '</div></section></div><aside class="workspace-aside"><section class="panel sticky-panel">'
        f'<div class="panel-head"><div><p class="panel-kicker">{escape(WORKSPACE_STATE)}</p>'
        f'<h2>{escape(WORKSPACE_OVERVIEW)}</h2></div></div><div class="panel-body">'
        '<dl class="fact-list">'
        f'<div class="fact"><dt>{escape(PROJECT)}</dt><dd><a href="{escape(project_url, quote=True)}">{escape(project_name)}</a></dd></div>'
        f'<div class="fact"><dt>{escape(BRANCH)}</dt><dd class="mono">{escape(live_branch)}</dd></div>'
        f'<div class="fact"><dt>{escape(DIRTY_PATHS)}</dt><dd>{escape(live_dirty)}</dd></div>'
        f'<div class="fact"><dt>{escape(INDEXED_PATHS)}</dt><dd>{row.indexed_file_count}</dd></div>'
        f'<div class="fact"><dt>{escape(VISIBILITY)}</dt><dd>{escape(visibility_label(row.visibility_mode))}</dd></div>'
        f'<div class="fact"><dt>{escape(TASK)}</dt><dd class="mono">{escape(_display_task(row))}</dd></div>'
        '</dl><div class="settings-divider"></div>'
        + f'<a class="text-link" href="{project_url}settings/">Настройки проекта и папок</a>'
        + "</div></section></aside></section>"
    )
    return _render_shell(
        base_path=base_path,
        page_title=document_title(f"Задачи · {workspace_name}"),
        breadcrumbs=(
            (BREADCRUMB_PROJECTS, base_path),
            (project_name, project_url),
            ("Задачи", None),
        ),
        events_url=_events_url(
            base_path,
            view="workspace",
            identity=row.workspace_id,
            search_query=detail.search_query,
            page=detail.page,
            snapshot=_snapshot_fingerprint(
                detail if navigation_rows is None else (detail, navigation_rows)
            ),
        ),
        content=content,
        navigation_rows=navigation,
        current_project_id=row.project_id,
        current_workspace_id=row.workspace_id,
        current_section="tasks",
    )


def _timeline_event_label(event: TaskEventRecord) -> str:
    return event_label(event.event_type)


def _render_verification(records: tuple[VerificationRecord, ...]) -> str:
    items = []
    for record in records:
        items.append(
            '<li class="verification-item"><div class="verification-head">'
            f"<strong>{escape(record.name)}</strong>"
            f'<span class="verification-status" data-status="{record.status.value}">'
            f"{escape(verification_status_label(record.status))}</span></div>"
            f'<p class="section-note">{escape(verification_source_label(record.source))}</p>'
            f'<pre class="verification-evidence">{escape(record.evidence)}</pre></li>'
        )
    return '<ul class="verification-list">' + "".join(items) + "</ul>" if items else ""


def _render_latest_verification(detail: DashboardTaskDetail) -> str:
    checkpoint = detail.latest_checkpoint
    if checkpoint is None:
        body = f'<p class="section-note">{escape(VERIFICATION_NO_REPORT)}</p>'
    else:
        body = (
            '<p class="section-note">'
            + escape(verification_report_label(checkpoint.task_revision, checkpoint.created_at))
            + "</p>"
        )
        if checkpoint.task_revision < detail.task.revision:
            body += f'<p class="section-note">{escape(VERIFICATION_OLDER)}</p>'
        records = tuple(
            record
            for record in detail.checkpoint_verification
            if record.checkpoint_id == checkpoint.checkpoint_id
        )
        body += _render_verification(records) or (
            f'<p class="section-note">{escape(VERIFICATION_EMPTY)}</p>'
        )
    return (
        '<section class="panel verification-panel"><div class="panel-head">'
        f"<h2>{escape(VERIFICATION_TITLE)}</h2></div>"
        f'<div class="panel-body">{body}</div></section>'
    )


def _render_timeline(detail: DashboardTaskDetail, *, base_path: str = "/") -> str:
    checkpoints = {item.checkpoint_id: item for item in detail.checkpoints}
    visible_events = detail.events
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
            content.append(
                _render_verification(
                    tuple(
                        record
                        for record in detail.checkpoint_verification
                        if record.checkpoint_id == checkpoint.checkpoint_id
                    )
                )
            )
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
        if event.target_state is not None:
            content.append(
                _state_pill(
                    event.target_state.value,
                    None if event.target_wait_reason is None else event.target_wait_reason.value,
                )
            )
        if event.operator_feedback is not None:
            content.append(
                f'<blockquote class="feedback-quote">{escape(event.operator_feedback)}</blockquote>'
            )
        if event.operator_comment is not None:
            content.append(
                f'<blockquote class="feedback-quote">{escape(event.operator_comment)}</blockquote>'
            )
        if event.event_type is TaskEventType.JIRA_LINK_UPDATED:
            content.append(
                escape(JIRA_CLEAR)
                if event.jira_url is None
                else f'<a href="{escape(event.jira_url, quote=True)}" target="_blank" '
                f'rel="noreferrer noopener">{escape(event.jira_url)}</a>'
            )
        if event.event_type is TaskEventType.OPERATOR_STATUS_UPDATED:
            content.append(
                escape(
                    operator_status_label(
                        None if event.operator_status is None else event.operator_status.value
                    )
                )
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
    items.append("</div>")
    items.append(
        _render_history_pagination(
            _url(base_path, "tasks", detail.task.task_id),
            page=detail.page,
            total=detail.event_count,
            page_size=_DASHBOARD_TIMELINE_EVENT_LIMIT,
            anchor="timeline",
        )
    )
    return "".join(items)


def render_task_page(
    detail: DashboardTaskDetail,
    *,
    base_path: str,
    navigation_rows: tuple[DashboardWorkspaceRow, ...] | None = None,
) -> str:
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
        jira_url=task.jira_url,
        operator_status=(None if task.operator_status is None else task.operator_status.value),
        detailed=True,
    )
    workspace_name = row.workspace_root.name or WORKSPACE_FALLBACK
    navigation = (row,) if navigation_rows is None else navigation_rows
    project_name = _project_display_name(navigation, row.project_id)
    no_actions = f'<p class="section-note">{escape(NO_ACTIONS)}</p>'
    stack = (
        EM_DASH
        if not detail.stack_hints
        else " · ".join(escape(item) for item in detail.stack_hints)
    )
    latest_checkpoint = detail.latest_checkpoint
    latest_update = ""
    if latest_checkpoint is not None:
        next_step = (
            ""
            if latest_checkpoint.next_step is None
            else f'<div class="next-step"><span>{escape(NEXT_STEP)}</span><p>{escape(latest_checkpoint.next_step)}</p></div>'
        )
        latest_update = (
            '<section class="task-update"><p class="panel-kicker">'
            + escape(UPDATED)
            + f'</p><p class="task-update-summary">{escape(latest_checkpoint.summary)}</p>'
            + next_step
            + "</section>"
        )
    content = (
        '<section class="page-intro task-intro"><div>'
        f'<p class="eyebrow">{escape(TASK_OVERVIEW)}</p>'
        f'<h1 class="task-title">{escape(task.title)}</h1>'
        '<div class="task-intro-meta">'
        f"{_state_pill(task.state.value, wait_reason)}"
        f'<span class="mono" title="{escape(task.task_id, quote=True)}">{escape(task.task_id[:10])}</span>'
        f"<span>{escape(REVISION)} {task.revision}</span>"
        f'<span>{escape(BRANCH)} <strong class="mono">{escape(_display_recorded_branch(detail.git_branch))}</strong></span>'
        '</div></div></section><nav class="task-sections" aria-label="Разделы задачи">'
        '<a href="#task-result">Результат и проверки</a><a href="#task-actions">Действия</a>'
        '<a href="#timeline">История</a></nav>'
        '<section class="task-layout"><div class="task-summary" id="task-result">'
        + latest_update
        + _render_latest_verification(detail)
        + "</div>"
        f'<section class="panel action-card" id="task-actions"><div class="panel-head">'
        f'<h2>{escape(ACTIONS)}</h2></div><div class="panel-body">{actions if actions else no_actions}</div></section>'
        + '<section class="panel timeline-panel" id="timeline"><div class="panel-head"><div>'
        f"<h2>{escape(TIMELINE)}</h2></div>"
        f'<p class="section-note">{escape(event_count_label(detail.event_count))}</p></div>'
        '<div class="panel-body">'
        + _render_timeline(detail, base_path=base_path)
        + "</div></section>"
        '<details class="panel facts-card"><summary>Данные задачи</summary>'
        '<div class="panel-body"><dl class="fact-list">'
        f'<div class="fact"><dt>{escape(WORKSPACE)}</dt>'
        f'<dd><a href="{escape(workspace_url, quote=True)}">{escape(workspace_name)}</a></dd></div>'
        f'<div class="fact"><dt>{escape(PROJECT)}</dt>'
        f'<dd><a href="{escape(project_url, quote=True)}">{escape(project_name)}</a></dd></div>'
        f'<div class="fact"><dt>{escape(BRANCH)}</dt>'
        f'<dd class="mono">{escape(_display_recorded_branch(detail.git_branch))}</dd></div>'
        f'<div class="fact"><dt>{escape(STATE)}</dt>'
        f"<dd>{escape(task_state_label(task.state.value, wait_reason))}</dd></div>"
        f'<div class="fact"><dt>{escape(OPERATOR_STATUS)}</dt>'
        f"<dd>{escape(operator_status_label(None if task.operator_status is None else task.operator_status.value))}</dd></div>"
        f'<div class="fact"><dt>{escape(JIRA)}</dt><dd>'
        + (
            escape(EM_DASH)
            if task.jira_url is None
            else f'<a href="{escape(task.jira_url, quote=True)}" target="_blank" '
            f'rel="noreferrer noopener">{escape(task.jira_url)}</a>'
        )
        + "</dd></div>"
        f'<div class="fact"><dt>{escape(WAIT_REASON)}</dt><dd>{escape(wait_reason_label(wait_reason))}</dd></div>'
        f'<div class="fact"><dt>{escape(STACK_HINTS)}</dt><dd class="mono">{stack}</dd></div>'
        f'<div class="fact"><dt>{escape(CREATED)}</dt><dd>{escape(task.created_at)}</dd></div>'
        f'<div class="fact"><dt>{escape(UPDATED)}</dt><dd>{escape(task.updated_at)}</dd></div>'
        f'<div class="fact"><dt>ID</dt><dd class="mono">{escape(task.task_id)}</dd></div>'
        "</dl></div></details></section>"
    )
    task_breadcrumbs: list[tuple[str, str | None]] = [
        (BREADCRUMB_PROJECTS, base_path),
        (project_name, project_url),
        ("Задачи", workspace_url),
        (task_crumb(task.task_id), None),
    ]
    fingerprinted = _fingerprint_task_detail(detail)
    return _render_shell(
        base_path=base_path,
        page_title=document_title(task.title),
        breadcrumbs=tuple(task_breadcrumbs),
        events_url=_events_url(
            base_path,
            view="task",
            identity=task.task_id,
            page=detail.page,
            snapshot=_snapshot_fingerprint(
                fingerprinted if navigation_rows is None else (fingerprinted, navigation_rows)
            ),
        ),
        content=content,
        navigation_rows=navigation,
        current_project_id=row.project_id,
        current_workspace_id=row.workspace_id,
        current_task_id=task.task_id,
        current_section="tasks",
    )


def _normalize_legacy_capability_path(path: str, access_token: str) -> str:
    prefix = f"/{access_token}"
    if path == prefix:
        return "/"
    if path.startswith(f"{prefix}/"):
        remainder = path[len(prefix) :]
        return remainder if remainder else "/"
    return path


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


def _parse_history_query(query: str, *, allow_search: bool) -> tuple[str | None, int]:
    if not query:
        return None, 1
    try:
        parsed = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=2,
        )
    except (UnicodeError, ValueError) as exc:
        raise SearchError("dashboard history query is malformed") from exc
    allowed = {"q", "page"} if allow_search else {"page"}
    if set(parsed) - allowed:
        if not allow_search:
            raise DashboardError("dashboard Task history accepts only a page field")
        raise SearchError("dashboard history query has unexpected fields")
    if any(len(values) != 1 for values in parsed.values()):
        raise SearchError("dashboard history query fields must be singular")
    page = 1
    if "page" in parsed:
        raw_page = parsed["page"][0]
        if not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 10:
            raise SearchError("dashboard history page is invalid")
        page = _history_page(int(raw_page), _DASHBOARD_MAX_HISTORY_PAGE, 1)
    search_query = _parse_search_query(urlencode({"q": parsed["q"][0]})) if "q" in parsed else None
    return search_query, page


def _parse_page_request(base_path: str, path: str, query: str) -> _DashboardPageRequest:
    if path == base_path:
        search_query, page = _parse_history_query(query, allow_search=True)
        redirect = path + (f"?{query}" if query else "")
        return _DashboardPageRequest("projects", None, search_query, redirect, page)
    if not path.startswith(base_path):
        raise DashboardError("dashboard path is outside the dashboard route")
    relative = path[len(base_path) :]
    parts = relative.split("/")
    if len(parts) == 4 and parts[0] == "projects" and parts[2:] == ["settings", ""]:
        if query:
            raise DashboardError("dashboard settings route does not accept query fields")
        return _DashboardPageRequest(
            "project", _decode_identity_component(parts[1]), None, path, settings=True
        )
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
        search_query, page = _parse_history_query(query, allow_search=True)
    elif parts[0] == "tasks":
        search_query, page = _parse_history_query(query, allow_search=False)
    else:
        if query:
            raise DashboardError("dashboard detail route does not accept query fields")
        search_query = None
        page = 1
    redirect = path + (f"?{query}" if query else "")
    kind = {"projects": "project", "workspaces": "workspace", "tasks": "task"}[parts[0]]
    return _DashboardPageRequest(kind, identity, search_query, redirect, page)


def _render_page(database_path: Path, base_path: str, request: _DashboardPageRequest) -> str:
    if request.kind == "projects":
        return render_projects_page(
            read_dashboard_home(
                database_path, search_query=request.search_query, page=request.page
            ),
            base_path=base_path,
        )
    navigation_rows = _read_dashboard_navigation_rows(database_path)
    assert request.identity is not None
    if request.kind == "project":
        return render_project_page(
            read_dashboard_project_detail(
                database_path, request.identity, include_skills=request.settings
            ),
            base_path=base_path,
            navigation_rows=navigation_rows,
            settings=request.settings,
        )
    if request.kind == "workspace":
        return render_workspace_page(
            read_dashboard_workspace_detail(
                database_path,
                request.identity,
                search_query=request.search_query,
                page=request.page,
            ),
            base_path=base_path,
            navigation_rows=navigation_rows,
        )
    if request.kind == "task":
        return render_task_page(
            read_dashboard_task_detail(database_path, request.identity, page=request.page),
            base_path=base_path,
            navigation_rows=navigation_rows,
        )
    raise DashboardError("dashboard page kind is unsupported")


def _parse_sse_view(query: str) -> tuple[str, str | None, str | None, str, int]:
    try:
        parsed = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=5,
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
    page = 1
    if "page" in parsed:
        if len(parsed["page"]) != 1:
            raise DashboardError("dashboard event history page must be singular")
        try:
            _search_query, page = _parse_history_query(
                urlencode({"page": parsed["page"][0]}),
                allow_search=False,
            )
        except SearchError as exc:
            raise DashboardError("dashboard event history page is invalid") from exc
    if view == "projects":
        if set(parsed) - {"view", "snapshot", "q", "page"}:
            raise DashboardError("Projects event query has unexpected fields")
        search_query: str | None = None
        if "q" in parsed:
            if len(parsed["q"]) != 1:
                raise DashboardError("dashboard event search query must be singular")
            search_query = parsed["q"][0].strip() or None
            if search_query is not None and (
                "\x00" in search_query or len(search_query.encode("utf-8")) > 256
            ):
                raise DashboardError("dashboard event search query is invalid")
        return view, None, search_query, snapshot, page
    if view not in {"project", "project_settings", "workspace", "task"}:
        raise DashboardError("dashboard event view is unsupported")
    identity_key = "project_id" if view == "project_settings" else f"{view}_id"
    allowed = {"view", "snapshot", identity_key}
    if view == "workspace":
        allowed.add("q")
    if view in {"workspace", "task"}:
        allowed.add("page")
    if set(parsed) - allowed or identity_key not in parsed or len(parsed[identity_key]) != 1:
        raise DashboardError("dashboard event query does not match the expected schema")
    identity = parsed[identity_key][0]
    if not identity or len(identity.encode("utf-8")) > 128 or "\x00" in identity:
        raise DashboardError("dashboard event identity is invalid")
    search_query = None
    if "q" in parsed:
        if len(parsed["q"]) != 1:
            raise DashboardError("dashboard event search query must be singular")
        search_query = parsed["q"][0].strip() or None
        if search_query is not None and (
            "\x00" in search_query or len(search_query.encode("utf-8")) > 256
        ):
            raise DashboardError("dashboard event search query is invalid")
    return view, identity, search_query, snapshot, page


def _view_fingerprint(
    database_path: Path,
    view: str,
    identity: str | None,
    search_query: str | None,
    page: int = 1,
) -> str:
    if view == "projects":
        value: object = _fingerprint_home(
            read_dashboard_home(
                database_path,
                search_query=search_query,
                page=page,
                include_live_status=False,
            )
        )
    elif view in {"project", "project_settings"}:
        assert identity is not None
        value = read_dashboard_project_detail(
            database_path, identity, include_skills=view == "project_settings"
        )
    elif view == "workspace":
        assert identity is not None
        value = read_dashboard_workspace_detail(
            database_path,
            identity,
            search_query=search_query,
            page=page,
        )
    elif view == "task":
        assert identity is not None
        value = _fingerprint_task_detail(
            read_dashboard_task_detail(
                database_path,
                identity,
                page=page,
                include_live_status=False,
            )
        )
    else:
        raise DashboardError("unsupported dashboard fingerprint view")
    if view != "projects":
        value = (value, _read_dashboard_navigation_rows(database_path))
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
    access_token: ClassVar[str]
    expected_host: ClassVar[str]
    expected_origin: ClassVar[str]
    stop_event: ClassVar[Event]
    sse_slots: ClassVar[BoundedSemaphore]
    workspace_invalidations: ClassVar[SimpleQueue[str] | None]
    vault_process: ClassVar[VaultProcess]
    vault_frame_origin = ""

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if self.headers.get("Host") != self.expected_host:
            self._send_html(404, "")
            return
        path = _normalize_legacy_capability_path(parsed.path, self.access_token)
        if path.startswith("/vault/"):
            self._serve_vault(path, parsed.query)
            return
        if path == f"{self.route_path}assets/dashboard.css" and not parsed.query:
            self._send_bytes(200, "text/css; charset=utf-8", DASHBOARD_CSS.encode("utf-8"))
            return
        if path == f"{self.route_path}assets/dashboard.js" and not parsed.query:
            self._send_bytes(
                200,
                "application/javascript; charset=utf-8",
                DASHBOARD_JS.encode("utf-8"),
            )
            return
        if path == f"{self.route_path}events":
            try:
                view, identity, search_query, snapshot, history_page = _parse_sse_view(parsed.query)
            except DashboardError:
                self._send_html(400, "")
                return
            self._serve_events(view, identity, search_query, snapshot, history_page)
            return
        try:
            page = _parse_page_request(self.route_path, path, parsed.query)
            html = _render_page(self.database_path, self.route_path, page)
        except SearchError:
            self._send_html(
                400,
                _render_navigation_error(
                    "Поиск недоступен", "Сократите запрос и попробуйте ещё раз."
                ),
            )
            return
        except (TaskNotFoundError, RegistryError):
            self._send_html(
                404,
                _render_navigation_error(
                    "Страница не найдена",
                    "Проект или задача больше недоступны. Выберите проект из списка.",
                ),
            )
            return
        except DashboardError:
            self._send_html(
                404,
                _render_navigation_error(
                    "Страница не найдена", "Проверьте ссылку или выберите проект из списка."
                ),
            )
            return
        except (
            OSError,
            sqlite3.DatabaseError,
            DatabaseError,
            GitWorkspaceError,
            ProjectSkillPolicyError,
            TaskError,
            TaskCheckpointError,
        ):
            self._send_html(
                503,
                _render_navigation_error(
                    UNAVAILABLE_HEADING, "Обновите страницу или вернитесь к списку проектов."
                ),
            )
            return
        self._send_html(200, html)

    def _serve_vault(self, path: str, query: str = "") -> None:
        parts = path.split("/")
        if len(parts) != 4 or parts[-1]:
            self._send_html(
                404,
                _render_navigation_error("Страница не найдена", "Проверьте ссылку на хранилище."),
            )
            return
        try:
            project_id = _decode_identity_component(parts[2])
            try:
                context = parse_qs(
                    query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    errors="strict",
                    max_num_fields=2,
                )
            except (ValueError, UnicodeError) as exc:
                raise DashboardError("invalid vault navigation context") from exc
            if set(context) - {"workspace", "task"} or any(
                len(values) != 1 or not values[0] or len(values[0]) > 128
                for values in context.values()
            ):
                raise DashboardError("invalid vault navigation context")
            workspace_id = context.get("workspace", [None])[0]
            task_id = context.get("task", [None])[0]
            rows = _read_dashboard_navigation_rows(self.database_path)
            workspaces: tuple[DashboardWorkspaceRow, ...]
            if project_id == "all":
                if context:
                    raise DashboardError("global vault has no project context")
                name = ALL_PROJECTS
                workspaces = ()
            else:
                detail = read_dashboard_project_detail(self.database_path, project_id)
                workspaces = detail.workspaces
                name = _project_display_name(rows, project_id)
            if workspace_id is not None and not any(
                row.workspace_id == workspace_id for row in workspaces
            ):
                raise DashboardError("vault workspace is outside the project")
            if task_id is not None:
                connection = connect_database(self.database_path)
                try:
                    task = get_task(connection, task_id)
                finally:
                    connection.close()
                if task.workspace_id != workspace_id:
                    raise DashboardError("vault task is outside the workspace")
            origin = self.vault_process.origin(self.expected_origin)
            self.vault_frame_origin = origin
            fragment = urlencode({"project": project_id, "name": name})
            content = (
                f'<iframe class="vault-frame" title="Личное хранилище проекта" '
                f'src="{escape(origin + "/#" + fragment, quote=True)}" '
                'allow="clipboard-write" referrerpolicy="no-referrer"></iframe>'
            )
            html = _render_shell(
                base_path="/",
                page_title=document_title(name),
                breadcrumbs=(
                    ((BREADCRUMB_PROJECTS, "/"), ("Личное хранилище", None))
                    if project_id == "all"
                    else (
                        (BREADCRUMB_PROJECTS, "/"),
                        (name, _url("/", "projects", project_id)),
                        ("Заметки и доступы", None),
                    )
                ),
                events_url="",
                content=content,
                navigation_rows=rows,
                current_project_id=project_id,
                current_workspace_id=workspace_id,
                current_task_id=task_id,
                current_section="vault",
                interactive=False,
            )
            self._send_html(200, html)
        except (RegistryError, DashboardError, TaskNotFoundError):
            self._send_html(
                404,
                _render_navigation_error(
                    "Переход недоступен",
                    "Откройте заметки заново из проекта. Записи доступны в личном хранилище.",
                ),
            )
        except Exception:
            self._send_html(
                503,
                _render_navigation_error(
                    "Хранилище недоступно", "Обновите страницу или вернитесь к списку проектов."
                ),
            )

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = _normalize_legacy_capability_path(parsed.path, self.access_token)
        try:
            page = _parse_page_request(self.route_path, path, parsed.query)
        except (DashboardError, SearchError):
            self._send_html(404, "")
            return
        if not _allows_dashboard_mutation(
            host=self.headers.get("Host"),
            origin=self.headers.get("Origin"),
            sec_fetch_site=self.headers.get("Sec-Fetch-Site"),
            expected_host=self.expected_host,
            expected_origin=self.expected_origin,
        ):
            self._send_html(403, _action_rejected_html())
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_form_error(400, page)
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
            self._send_form_error(400, page)
            return
        try:
            request = _parse_dashboard_action_form(payload)
            if isinstance(request, DashboardVisibilityRequest):
                mutate_dashboard_visibility(self.database_path, request)
            elif isinstance(request, DashboardSkillPolicyRequest):
                if page.kind != "project" or page.identity != request.project_id:
                    raise TaskValidationError(
                        "dashboard skill-scope target does not match its project page"
                    )
                retry_workspace_ids = mutate_dashboard_skill_policy(self.database_path, request)
                if self.workspace_invalidations is not None:
                    for workspace_id in retry_workspace_ids:
                        self.workspace_invalidations.put(workspace_id)
            elif isinstance(request, DashboardProjectDeleteRequest):
                if page.kind != "project" or page.identity != request.project_id:
                    raise TaskValidationError(
                        "dashboard Project deletion target does not match its page"
                    )
                mutate_dashboard_registry(self.database_path, request)
                redirect_target = self.route_path
            elif isinstance(request, DashboardWorkspaceRelocationRequest):
                if page.kind != "workspace" or page.identity != request.workspace_id:
                    raise TaskValidationError(
                        "dashboard Workspace relocation target does not match its page"
                    )
                mutate_dashboard_registry(self.database_path, request)
                if self.workspace_invalidations is not None:
                    self.workspace_invalidations.put(request.workspace_id)
                redirect_target = page.redirect_target
            else:
                if request.action == "delete_task" and (
                    page.kind != "task" or page.identity != request.task_id
                ):
                    raise TaskValidationError("Task deletion target does not match its page")
                mutate_dashboard_task(
                    self.database_path,
                    request,
                )
            if not isinstance(
                request,
                (DashboardProjectDeleteRequest, DashboardWorkspaceRelocationRequest),
            ):
                redirect_target = (
                    _url(self.route_path, "workspaces", request.workspace_id)
                    if isinstance(request, DashboardActionRequest)
                    and request.action == "delete_task"
                    else page.redirect_target
                )
        except TaskValidationError:
            self._send_form_error(400, page, payload)
            return
        except (
            TaskNotFoundError,
            TaskConflictError,
            TaskRevisionConflictError,
            TaskWorkspaceConflictError,
            TaskTransitionError,
            ProjectNotFoundError,
            HiddenProjectionError,
            HiddenProjectionCollisionError,
            RegistryError,
            GitWorkspaceError,
            HostIntegrationStateError,
            ProjectSkillPolicyError,
            SkillRuntimeError,
        ):
            self._send_form_error(409, page, payload)
            return
        except (OSError, sqlite3.DatabaseError, DatabaseError):
            self._send_html(503, "")
            return
        self._send_redirect(redirect_target)

    def _send_form_error(
        self, status: int, page: _DashboardPageRequest, payload: bytes = b""
    ) -> None:
        self._send_html(
            status,
            _render_dashboard_form_error(
                self.database_path, self.route_path, page, status=status, payload=payload
            ),
        )

    def _serve_events(
        self,
        view: str,
        identity: str | None,
        search_query: str | None,
        expected_snapshot: str,
        page: int = 1,
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
                    page,
                )
            except (
                OSError,
                sqlite3.DatabaseError,
                DatabaseError,
                GitWorkspaceError,
                ProjectSkillPolicyError,
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
                    if view in {"workspace", "project", "project_settings"}:
                        try:
                            refreshed_snapshot = _view_fingerprint(
                                self.database_path,
                                view,
                                identity,
                                search_query,
                                page,
                            )
                        except (
                            GitWorkspaceError,
                            RegistryError,
                            TaskError,
                            SearchError,
                            OSError,
                            sqlite3.DatabaseError,
                            DatabaseError,
                        ):
                            self._write_sse("event: refresh\ndata: changed\n\n")
                            return
                        if refreshed_snapshot != current_snapshot:
                            current_snapshot = refreshed_snapshot
                            self._write_sse("event: refresh\ndata: changed\n\n")
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
            if name == "Content-Security-Policy" and self.vault_frame_origin:
                value += f"; frame-src {self.vault_frame_origin}"
            self.send_header(name, value)

    def log_message(self, _format: str, *args: object) -> None:
        del args


def _request_handler(
    database_path: Path,
    access_token: str,
    stop_event: Event,
    workspace_invalidations: SimpleQueue[str] | None,
) -> type[_DashboardRequestHandler]:
    class Handler(_DashboardRequestHandler):
        pass

    Handler.database_path = database_path
    Handler.route_path = "/"
    Handler.access_token = access_token
    Handler.stop_event = stop_event
    Handler.sse_slots = BoundedSemaphore(_DASHBOARD_SSE_MAX_CLIENTS)
    Handler.workspace_invalidations = workspace_invalidations
    return Handler


def dashboard_url_path(socket_path: Path) -> Path:
    """Return the runtime-directory file that publishes the current dashboard URL."""
    return socket_path.parent / _DASHBOARD_URL_FILENAME


def dashboard_token_path(database_path: Path) -> Path:
    """Return the durable capability-token file next to the selected database."""
    return database_path.parent / _DASHBOARD_TOKEN_FILENAME


def load_or_create_dashboard_access_token(database_path: Path) -> str:
    """Return the stable private capability shared by daemon-owned loopback services."""
    return _load_or_create_access_token(dashboard_token_path(database_path))


def read_dashboard_access_token(database_path: Path) -> str | None:
    """Read the existing private loopback capability without creating state."""
    token = _read_private_ascii_line(dashboard_token_path(database_path))
    return token if token is not None and _DASHBOARD_TOKEN_PATTERN.fullmatch(token) else None


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
        workspace_invalidations: SimpleQueue[str] | None = None,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise DashboardError("dashboard loopback port is invalid")
        self._database_path = database_path
        self._url_file = url_file
        self._token_file = dashboard_token_path(database_path)
        self._port = port
        self._workspace_invalidations = workspace_invalidations
        self._server: _DashboardHttpServer | None = None
        self._thread: Thread | None = None
        self._stop_event: Event | None = None
        self._started_event: Event | None = None
        self._url: str | None = None
        self._failure: BaseException | None = None
        self._vault_process = VaultProcess(database_path)

    def is_running(self) -> bool:
        """Return whether the daemon-owned dashboard listener is currently healthy and running."""
        return (
            self._server is not None
            and self._thread is not None
            and self._thread.is_alive()
            and self._failure is None
        )

    def get_url(self) -> str:
        """Return the loopback dashboard URL, starting the listener if needed."""
        if self._server is not None and self._thread is not None and self._thread.is_alive():
            assert self._url is not None
            self._publish_url_file(self._url)
            return self._url
        self.close()

        self._vault_process = VaultProcess(self._database_path)
        access_token = load_or_create_dashboard_access_token(self._database_path)
        stop_event = Event()
        started_event = Event()
        self._failure = None
        try:
            try:
                server = _DashboardHttpServer(
                    (DASHBOARD_HOST, self._port),
                    _request_handler(
                        self._database_path,
                        access_token,
                        stop_event,
                        self._workspace_invalidations,
                    ),
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
            handler.vault_process = self._vault_process
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
            self._url = f"http://{DASHBOARD_HOST}:{port}/"
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
        self._vault_process.close()
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
