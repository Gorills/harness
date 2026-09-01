from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from queue import SimpleQueue
from uuid import uuid4

from harness.registry import get_workspace
from harness.task_baseline import (
    TaskBaselineRecord,
    capture_workspace_task_baseline,
    persist_task_baseline,
)

MAX_TASK_TITLE_BYTES = 256
MAX_TASK_STACK_HINTS = 16
MAX_TASK_STACK_HINT_BYTES = 128


class TaskError(RuntimeError):
    """Base class for durable Harness Task domain failures."""


class TaskNotFoundError(TaskError):
    """Raised when a requested Task does not exist."""


class TaskConflictError(TaskError):
    """Raised when a Task operation conflicts with Workspace Task state."""


class TaskRevisionConflictError(TaskConflictError):
    """Raised when an existing Task mutation uses a stale revision."""


class TaskWorkspaceConflictError(TaskConflictError):
    """Raised when an explicit Task does not belong to the caller's Workspace."""


class TaskTransitionError(TaskError):
    """Raised when a Task state transition is not valid in v1."""


class TaskValidationError(TaskError):
    """Raised when Task input violates a bounded domain contract."""


class TaskState(StrEnum):
    """Minimal durable Task states supported by Harness v1."""

    WORKING = "working"
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskWaitReason(StrEnum):
    """Allowed reasons for a Task to wait on something outside agent work."""

    OPERATOR_REVIEW = "operator_review"
    OPERATOR_INPUT = "operator_input"
    EXTERNAL = "external"


class TaskOperatorStatus(StrEnum):
    """Human-maintained delivery marker, independent from the Task lifecycle state."""

    DEPLOY_TEST = "deploy_test"
    DEPLOY_PROD = "deploy_prod"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Durable Task identity and optimistic-concurrency state."""

    task_id: str
    workspace_id: str
    title: str
    state: TaskState
    wait_reason: TaskWaitReason | None
    jira_url: str | None
    operator_status: TaskOperatorStatus | None
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TaskCreationRecord:
    """Atomic new-Task creation result including its required mechanical baseline."""

    task: TaskRecord
    baseline: TaskBaselineRecord


@dataclass(frozen=True, slots=True)
class SkillRelevanceKey:
    """Skill-resolver input derived from the Workspace's current relevant Task.

    The resolver selects the working Task, else the newest waiting Task, and reads that
    Task's stack hints. Terminal Tasks do not participate. Task lifecycle state is not
    part of the key unless it changes which Task is relevant or that Task's hints.
    """

    task_id: str | None
    stack_hints: tuple[str, ...]


def create_task_record(
    connection: sqlite3.Connection,
    workspace_id: str,
    title: str,
    *,
    stack_hints: tuple[str, ...] = (),
    now: datetime | None = None,
) -> TaskRecord:
    """Create one working Task together with its mandatory mechanical baseline."""
    return create_task_with_baseline(
        connection, workspace_id, title, stack_hints=stack_hints, now=now
    ).task


def create_task_with_baseline(
    connection: sqlite3.Connection,
    workspace_id: str,
    title: str,
    *,
    stack_hints: tuple[str, ...] = (),
    now: datetime | None = None,
) -> TaskCreationRecord:
    """Atomically create a new working Task and its mandatory mechanical baseline."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        created = _create_task_with_baseline_in_transaction(
            connection,
            workspace_id,
            title,
            stack_hints=stack_hints,
            now=now,
        )
        connection.execute("COMMIT")
        return created
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def get_task(connection: sqlite3.Connection, task_id: str) -> TaskRecord:
    """Load one Task by stable Harness identity."""
    row = connection.execute(
        """
        SELECT id, workspace_id, title, state, wait_reason, jira_url, operator_status,
               revision, created_at, updated_at
        FROM tasks
        WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"task does not exist: {task_id}")
    return _task_from_row(row)


def get_task_stack_hints(connection: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
    """Return normalized Task stack hints in caller-supplied order."""
    get_task(connection, task_id)
    rows = connection.execute(
        "SELECT hint FROM task_stack_hints WHERE task_id = ? ORDER BY position",
        (task_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def normalize_task_stack_hints(stack_hints: tuple[str, ...]) -> tuple[str, ...]:
    """Validate and canonicalize bounded greenfield stack hints."""
    if not isinstance(stack_hints, tuple):
        raise TaskValidationError("task stack_hints must be a tuple")
    if len(stack_hints) > MAX_TASK_STACK_HINTS:
        raise TaskValidationError(f"task stack_hints exceeds {MAX_TASK_STACK_HINTS} items")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in stack_hints:
        if not isinstance(value, str):
            raise TaskValidationError("task stack hint must be text")
        hint = value.strip().casefold()
        if not hint or "\x00" in hint:
            raise TaskValidationError("task stack hint must be non-empty text")
        try:
            size = len(hint.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise TaskValidationError("task stack hint must be valid UTF-8 text") from exc
        if size > MAX_TASK_STACK_HINT_BYTES:
            raise TaskValidationError(
                f"task stack hint exceeds {MAX_TASK_STACK_HINT_BYTES} UTF-8 bytes"
            )
        if hint in seen:
            raise TaskValidationError("task stack_hints must not contain duplicates")
        seen.add(hint)
        normalized.append(hint)
    return tuple(normalized)


def get_working_task(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> TaskRecord | None:
    """Return the one working Task for a Workspace, if present."""
    get_workspace(connection, workspace_id)
    return _working_task(connection, workspace_id)


def get_relevant_task(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> TaskRecord | None:
    """Return the current working Task, or the most recently updated waiting Task."""
    get_workspace(connection, workspace_id)
    row = connection.execute(
        """
        SELECT id, workspace_id, title, state, wait_reason, jira_url, operator_status,
               revision, created_at, updated_at
        FROM tasks
        WHERE workspace_id = ? AND state IN ('working', 'waiting')
        ORDER BY CASE state WHEN 'working' THEN 0 ELSE 1 END, updated_at DESC, id DESC
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    return None if row is None else _task_from_row(row)


def skill_relevance_key(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> SkillRelevanceKey:
    """Return the relevant Task identity and stack hints consumed by skill resolution."""
    task = get_relevant_task(connection, workspace_id)
    if task is None:
        return SkillRelevanceKey(task_id=None, stack_hints=())
    return SkillRelevanceKey(
        task_id=task.task_id,
        stack_hints=get_task_stack_hints(connection, task.task_id),
    )


def enqueue_skill_reconcile_if_relevance_changed(
    watcher_invalidations: SimpleQueue[str] | None,
    workspace_id: str,
    before: SkillRelevanceKey,
    after: SkillRelevanceKey,
) -> bool:
    """Enqueue watcher skill reconciliation when resolver input changed after commit.

    Returns whether ``before`` and ``after`` differ. A ``None`` queue still reports the
    change; it does not enqueue.
    """
    changed = before != after
    if changed and watcher_invalidations is not None:
        watcher_invalidations.put(workspace_id)
    return changed


def get_latest_task(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> TaskRecord | None:
    """Return the most recently updated Task for a Workspace, regardless of terminal state."""
    get_workspace(connection, workspace_id)
    row = connection.execute(
        """
        SELECT id, workspace_id, title, state, wait_reason, jira_url, operator_status,
               revision, created_at, updated_at
        FROM tasks
        WHERE workspace_id = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    return None if row is None else _task_from_row(row)


def transition_task_state(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    wait_reason: TaskWaitReason | None = None,
    now: datetime | None = None,
) -> TaskRecord:
    """Apply one explicit existing-Task state transition using revision compare-and-set."""
    _validate_expected_revision(expected_revision)
    _validate_state_reason(state, wait_reason)
    timestamp = _utc_timestamp(now)

    connection.execute("BEGIN IMMEDIATE")
    try:
        updated = _transition_task_state_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            state=state,
            wait_reason=wait_reason,
            timestamp=timestamp,
        )
        connection.execute("COMMIT")
        return updated
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _create_task_with_baseline_in_transaction(
    connection: sqlite3.Connection,
    workspace_id: str,
    title: str,
    *,
    stack_hints: tuple[str, ...] = (),
    now: datetime | None = None,
) -> TaskCreationRecord:
    """Create Task + baseline inside a caller-owned ``BEGIN IMMEDIATE`` transaction."""
    normalized_title = _validate_title(title)
    normalized_stack_hints = normalize_task_stack_hints(stack_hints)
    get_workspace(connection, workspace_id)
    _require_no_working_task(connection, workspace_id)
    snapshot = capture_workspace_task_baseline(connection, workspace_id, now=now)
    task = _new_task_record(
        workspace_id=workspace_id,
        title=normalized_title,
        timestamp=snapshot.captured_at,
    )
    try:
        _insert_task(connection, task)
        _insert_task_stack_hints(connection, task.task_id, normalized_stack_hints)
    except sqlite3.IntegrityError as exc:
        if _working_task(connection, workspace_id) is not None:
            raise TaskConflictError("workspace already has a working task") from exc
        raise
    baseline = persist_task_baseline(connection, task.task_id, snapshot)
    return TaskCreationRecord(task=task, baseline=baseline)


def _transition_task_state_in_transaction(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    wait_reason: TaskWaitReason | None,
    timestamp: str,
) -> TaskRecord:
    """Transition an existing Task inside a caller-owned ``BEGIN IMMEDIATE`` transaction."""
    current = get_task(connection, task_id)
    if current.revision != expected_revision:
        raise TaskRevisionConflictError(
            f"task revision mismatch: expected {expected_revision}, current {current.revision}"
        )
    _validate_transition(current.state, state)

    if state is TaskState.WORKING:
        existing = _working_task(connection, current.workspace_id)
        if existing is not None and existing.task_id != current.task_id:
            raise TaskConflictError(f"workspace already has a working task: {existing.task_id}")

    try:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET state = ?, wait_reason = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (
                state.value,
                wait_reason.value if wait_reason is not None else None,
                timestamp,
                task_id,
                expected_revision,
            ),
        )
    except sqlite3.IntegrityError as exc:
        if state is TaskState.WORKING:
            raise TaskConflictError("workspace already has a working task") from exc
        raise
    if cursor.rowcount != 1:
        raise TaskRevisionConflictError(
            f"task revision changed during mutation: expected {expected_revision}"
        )
    return get_task(connection, task_id)


def _reopen_task_in_transaction(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    timestamp: str,
) -> TaskRecord:
    """Explicitly reopen one terminal Task without weakening ordinary transition rules."""
    current = get_task(connection, task_id)
    if current.revision != expected_revision:
        raise TaskRevisionConflictError(
            f"task revision mismatch: expected {expected_revision}, current {current.revision}"
        )
    if current.state not in {TaskState.COMPLETED, TaskState.CANCELLED}:
        raise TaskTransitionError(
            f"reopen requires terminal Task; current state is {current.state.value}"
        )
    existing = _working_task(connection, current.workspace_id)
    if existing is not None and existing.task_id != current.task_id:
        raise TaskConflictError(f"workspace already has a working task: {existing.task_id}")
    try:
        cursor = connection.execute(
            """
            UPDATE tasks
            SET state = 'working', wait_reason = NULL, revision = revision + 1, updated_at = ?
            WHERE id = ? AND revision = ? AND state IN ('completed', 'cancelled')
            """,
            (timestamp, task_id, expected_revision),
        )
    except sqlite3.IntegrityError as exc:
        raise TaskConflictError("workspace already has a working task") from exc
    if cursor.rowcount != 1:
        raise TaskRevisionConflictError(
            f"task revision changed during reopen: expected {expected_revision}"
        )
    return get_task(connection, task_id)


def _advance_task_revision_in_transaction(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    timestamp: str,
) -> TaskRecord:
    """Advance one Task revision for an operator-owned metadata/history mutation."""
    current = get_task(connection, task_id)
    if current.revision != expected_revision:
        raise TaskRevisionConflictError(
            f"task revision mismatch: expected {expected_revision}, current {current.revision}"
        )
    cursor = connection.execute(
        """
        UPDATE tasks
        SET revision = revision + 1, updated_at = ?
        WHERE id = ? AND revision = ?
        """,
        (timestamp, task_id, expected_revision),
    )
    if cursor.rowcount != 1:
        raise TaskRevisionConflictError(
            f"task revision changed during operator mutation: expected {expected_revision}"
        )
    return get_task(connection, task_id)


def _require_no_working_task(connection: sqlite3.Connection, workspace_id: str) -> None:
    existing = _working_task(connection, workspace_id)
    if existing is not None:
        raise TaskConflictError(f"workspace already has a working task: {existing.task_id}")


def _new_task_record(*, workspace_id: str, title: str, timestamp: str) -> TaskRecord:
    return TaskRecord(
        task_id=uuid4().hex,
        workspace_id=workspace_id,
        title=title,
        state=TaskState.WORKING,
        wait_reason=None,
        jira_url=None,
        operator_status=None,
        revision=1,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _insert_task(connection: sqlite3.Connection, task: TaskRecord) -> None:
    connection.execute(
        """
        INSERT INTO tasks(
            id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            task.workspace_id,
            task.title,
            task.state.value,
            None,
            task.revision,
            task.created_at,
            task.updated_at,
        ),
    )


def _insert_task_stack_hints(
    connection: sqlite3.Connection,
    task_id: str,
    stack_hints: tuple[str, ...],
) -> None:
    connection.executemany(
        "INSERT INTO task_stack_hints(task_id, position, hint) VALUES (?, ?, ?)",
        ((task_id, position, hint) for position, hint in enumerate(stack_hints)),
    )


def _working_task(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> TaskRecord | None:
    row = connection.execute(
        """
        SELECT id, workspace_id, title, state, wait_reason, jira_url, operator_status,
               revision, created_at, updated_at
        FROM tasks
        WHERE workspace_id = ? AND state = 'working'
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        return None
    return _task_from_row(row)


def _validate_title(title: str) -> str:
    if not isinstance(title, str):
        raise TaskValidationError("task title must be text")
    normalized = title.strip()
    if not normalized or "\x00" in normalized:
        raise TaskValidationError("task title must be non-empty text")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise TaskValidationError("task title must be valid UTF-8 text") from exc
    if size > MAX_TASK_TITLE_BYTES:
        raise TaskValidationError(f"task title exceeds {MAX_TASK_TITLE_BYTES} UTF-8 bytes")
    return normalized


def _validate_expected_revision(expected_revision: int) -> None:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision <= 0
    ):
        raise TaskValidationError("expected_revision must be a positive integer")


def _validate_state_reason(
    state: TaskState,
    wait_reason: TaskWaitReason | None,
) -> None:
    if state is TaskState.WAITING and wait_reason is None:
        raise TaskValidationError("waiting Task transition requires wait_reason")
    if state is not TaskState.WAITING and wait_reason is not None:
        raise TaskValidationError("wait_reason is only valid for waiting Tasks")


def _validate_transition(current: TaskState, target: TaskState) -> None:
    allowed = {
        TaskState.WORKING: {
            TaskState.WAITING,
            TaskState.COMPLETED,
            TaskState.CANCELLED,
        },
        TaskState.WAITING: {
            TaskState.WORKING,
            TaskState.COMPLETED,
            TaskState.CANCELLED,
        },
        TaskState.COMPLETED: set(),
        TaskState.CANCELLED: set(),
    }
    if target not in allowed[current]:
        raise TaskTransitionError(f"invalid Task transition: {current.value} -> {target.value}")


def _utc_timestamp(now: datetime | None) -> str:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise TaskValidationError("Task timestamps require a timezone-aware datetime")
    return current.astimezone(UTC).isoformat(timespec="microseconds")


def _task_from_row(row: tuple[object, ...]) -> TaskRecord:
    (
        task_id,
        workspace_id,
        title,
        state,
        wait_reason,
        jira_url,
        operator_status,
        revision,
        created_at,
        updated_at,
    ) = row
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(workspace_id, str)
        or not workspace_id
        or not isinstance(title, str)
        or not title
        or not isinstance(state, str)
        or (wait_reason is not None and not isinstance(wait_reason, str))
        or (jira_url is not None and not isinstance(jira_url, str))
        or (operator_status is not None and not isinstance(operator_status, str))
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(updated_at, str)
        or not updated_at
    ):
        raise TaskError("task row has invalid persisted types")
    try:
        task_state = TaskState(state)
    except ValueError as exc:
        raise TaskError(f"task row has unsupported state: {state!r}") from exc
    try:
        task_wait_reason = TaskWaitReason(wait_reason) if wait_reason is not None else None
    except ValueError as exc:
        raise TaskError(f"task row has unsupported wait_reason: {wait_reason!r}") from exc
    _validate_state_reason(task_state, task_wait_reason)
    try:
        task_operator_status = (
            TaskOperatorStatus(operator_status) if operator_status is not None else None
        )
    except ValueError as exc:
        raise TaskError(f"task row has unsupported operator_status: {operator_status!r}") from exc
    return TaskRecord(
        task_id=task_id,
        workspace_id=workspace_id,
        title=title,
        state=task_state,
        wait_reason=task_wait_reason,
        jira_url=jira_url,
        operator_status=task_operator_status,
        revision=revision,
        created_at=created_at,
        updated_at=updated_at,
    )
