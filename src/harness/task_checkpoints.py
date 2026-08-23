from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from harness.task_changes import (
    TaskChangedFiles,
    TaskChangedFilesError,
    calculate_task_changed_files,
)
from harness.tasks import (
    TaskError,
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    get_task,
)

MAX_CHECKPOINT_SUMMARY_BYTES = 4096
MAX_CHECKPOINT_NEXT_STEP_BYTES = 2048


class TaskCheckpointError(TaskError):
    """Base class for durable Task checkpoint failures."""


class TaskCheckpointMechanicalError(TaskCheckpointError):
    """Raised when mechanical checkpoint evidence cannot be calculated safely."""


class TaskEventType(StrEnum):
    """Persisted Task event kinds implemented by the current checkpoint slice."""

    CHECKPOINT = "checkpoint"


@dataclass(frozen=True, slots=True)
class TaskCheckpointRecord:
    """One durable semantic checkpoint plus mechanically captured Workspace evidence."""

    checkpoint_id: str
    task_id: str
    task_revision: int
    state: TaskState
    wait_reason: TaskWaitReason | None
    summary: str
    next_step: str | None
    created_at: str
    baseline_head: str | None
    current_head: str | None
    current_branch: str | None
    current_dirty_path_count: int
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskEventRecord:
    """One immutable Task timeline event persisted by Harness."""

    event_id: int
    task_id: str
    task_revision: int
    event_type: TaskEventType
    checkpoint_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskCheckpointMutation:
    """Atomic checkpoint result: updated Task, checkpoint record, and timeline event."""

    task: TaskRecord
    checkpoint: TaskCheckpointRecord
    event: TaskEventRecord


def checkpoint_task(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    summary: str,
    next_step: str | None = None,
    wait_reason: TaskWaitReason | None = None,
    now: datetime | None = None,
) -> TaskCheckpointMutation:
    """Atomically checkpoint one working Task using stable identity and revision CAS."""
    _validate_expected_revision(expected_revision)
    _validate_checkpoint_state(state, wait_reason, next_step)
    normalized_summary = _validate_text(
        summary,
        label="checkpoint summary",
        maximum_bytes=MAX_CHECKPOINT_SUMMARY_BYTES,
        required=True,
    )
    assert normalized_summary is not None
    normalized_next_step = _validate_text(
        next_step,
        label="checkpoint next_step",
        maximum_bytes=MAX_CHECKPOINT_NEXT_STEP_BYTES,
        required=False,
    )

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = get_task(connection, task_id)
        if current.revision != expected_revision:
            raise TaskRevisionConflictError(
                f"task revision mismatch: expected {expected_revision}, current {current.revision}"
            )
        if current.state is not TaskState.WORKING:
            raise TaskTransitionError(
                f"checkpoint requires working Task; current state is {current.state.value}"
            )

        mechanical = _calculate_mechanical_checkpoint(connection, current)
        timestamp = _utc_timestamp(now)
        new_revision = expected_revision + 1
        cursor = connection.execute(
            """
            UPDATE tasks
            SET state = ?, wait_reason = ?, revision = ?, updated_at = ?
            WHERE id = ? AND revision = ? AND state = 'working'
            """,
            (
                state.value,
                wait_reason.value if wait_reason is not None else None,
                new_revision,
                timestamp,
                task_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise TaskRevisionConflictError(
                f"task revision changed during checkpoint: expected {expected_revision}"
            )

        checkpoint = _insert_checkpoint(
            connection,
            task_id=task_id,
            task_revision=new_revision,
            state=state,
            wait_reason=wait_reason,
            summary=normalized_summary,
            next_step=normalized_next_step,
            timestamp=timestamp,
            mechanical=mechanical,
        )
        event = _insert_checkpoint_event(connection, checkpoint)
        updated = get_task(connection, task_id)
        connection.execute("COMMIT")
        return TaskCheckpointMutation(task=updated, checkpoint=checkpoint, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def get_task_checkpoint(
    connection: sqlite3.Connection,
    checkpoint_id: str,
) -> TaskCheckpointRecord:
    """Load one persisted Task checkpoint by stable checkpoint identity."""
    row = connection.execute(
        """
        SELECT
            id,
            task_id,
            task_revision,
            state,
            wait_reason,
            summary,
            next_step,
            created_at,
            baseline_head,
            current_head,
            current_branch,
            current_dirty_path_count
        FROM task_checkpoints
        WHERE id = ?
        """,
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise TaskCheckpointError(f"task checkpoint does not exist: {checkpoint_id}")
    changed_paths = _load_changed_paths(connection, checkpoint_id)
    return _checkpoint_from_row(row, changed_paths)


def list_task_checkpoints(
    connection: sqlite3.Connection,
    task_id: str,
) -> tuple[TaskCheckpointRecord, ...]:
    """Load all checkpoints for one Task in monotonic Task-revision order."""
    get_task(connection, task_id)
    rows = connection.execute(
        """
        SELECT
            id,
            task_id,
            task_revision,
            state,
            wait_reason,
            summary,
            next_step,
            created_at,
            baseline_head,
            current_head,
            current_branch,
            current_dirty_path_count
        FROM task_checkpoints
        WHERE task_id = ?
        ORDER BY task_revision
        """,
        (task_id,),
    )
    records: list[TaskCheckpointRecord] = []
    for row in rows:
        checkpoint_id = row[0]
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise TaskCheckpointError("task checkpoint row has invalid persisted identity")
        records.append(_checkpoint_from_row(row, _load_changed_paths(connection, checkpoint_id)))
    return tuple(records)


def list_task_events(
    connection: sqlite3.Connection,
    task_id: str,
) -> tuple[TaskEventRecord, ...]:
    """Load the durable Task timeline in database event order."""
    get_task(connection, task_id)
    rows = connection.execute(
        """
        SELECT id, task_id, task_revision, event_type, checkpoint_id, created_at
        FROM task_events
        WHERE task_id = ?
        ORDER BY id
        """,
        (task_id,),
    )
    return tuple(_event_from_row(row) for row in rows)


def _calculate_mechanical_checkpoint(
    connection: sqlite3.Connection,
    task: TaskRecord,
) -> TaskChangedFiles:
    try:
        mechanical = calculate_task_changed_files(connection, task.task_id)
    except TaskChangedFilesError as exc:
        raise TaskCheckpointMechanicalError(
            "Task checkpoint mechanical changed-file calculation failed"
        ) from exc
    if mechanical.task_id != task.task_id or mechanical.workspace_id != task.workspace_id:
        raise TaskCheckpointMechanicalError(
            "Task checkpoint mechanical evidence does not match Task ownership"
        )
    return mechanical


def _insert_checkpoint(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    task_revision: int,
    state: TaskState,
    wait_reason: TaskWaitReason | None,
    summary: str,
    next_step: str | None,
    timestamp: str,
    mechanical: TaskChangedFiles,
) -> TaskCheckpointRecord:
    checkpoint_id = uuid4().hex
    record = TaskCheckpointRecord(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        task_revision=task_revision,
        state=state,
        wait_reason=wait_reason,
        summary=summary,
        next_step=next_step,
        created_at=timestamp,
        baseline_head=mechanical.baseline_head,
        current_head=mechanical.current_head,
        current_branch=mechanical.current_branch,
        current_dirty_path_count=mechanical.current_dirty_path_count,
        changed_paths=mechanical.relative_paths,
    )
    connection.execute(
        """
        INSERT INTO task_checkpoints(
            id,
            task_id,
            task_revision,
            state,
            wait_reason,
            summary,
            next_step,
            created_at,
            baseline_head,
            current_head,
            current_branch,
            current_dirty_path_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.checkpoint_id,
            record.task_id,
            record.task_revision,
            record.state.value,
            record.wait_reason.value if record.wait_reason is not None else None,
            record.summary,
            record.next_step,
            record.created_at,
            record.baseline_head,
            record.current_head,
            record.current_branch,
            record.current_dirty_path_count,
        ),
    )
    connection.executemany(
        """
        INSERT INTO task_checkpoint_changed_paths(checkpoint_id, relative_path)
        VALUES (?, ?)
        """,
        ((record.checkpoint_id, path) for path in record.changed_paths),
    )
    return record


def _insert_checkpoint_event(
    connection: sqlite3.Connection,
    checkpoint: TaskCheckpointRecord,
) -> TaskEventRecord:
    cursor = connection.execute(
        """
        INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
        VALUES (?, ?, 'checkpoint', ?, ?)
        """,
        (
            checkpoint.task_id,
            checkpoint.task_revision,
            checkpoint.checkpoint_id,
            checkpoint.created_at,
        ),
    )
    event_id = cursor.lastrowid
    if not isinstance(event_id, int) or event_id <= 0:
        raise TaskCheckpointError("Task checkpoint event did not receive a valid identity")
    return TaskEventRecord(
        event_id=event_id,
        task_id=checkpoint.task_id,
        task_revision=checkpoint.task_revision,
        event_type=TaskEventType.CHECKPOINT,
        checkpoint_id=checkpoint.checkpoint_id,
        created_at=checkpoint.created_at,
    )


def _load_changed_paths(
    connection: sqlite3.Connection,
    checkpoint_id: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT relative_path
        FROM task_checkpoint_changed_paths
        WHERE checkpoint_id = ?
        ORDER BY relative_path
        """,
        (checkpoint_id,),
    )
    paths: list[str] = []
    for row in rows:
        relative_path = row[0]
        if not isinstance(relative_path, str) or not relative_path:
            raise TaskCheckpointError("Task checkpoint changed path has invalid persisted value")
        paths.append(relative_path)
    return tuple(paths)


def _checkpoint_from_row(
    row: tuple[object, ...],
    changed_paths: tuple[str, ...],
) -> TaskCheckpointRecord:
    (
        checkpoint_id,
        task_id,
        task_revision,
        state,
        wait_reason,
        summary,
        next_step,
        created_at,
        baseline_head,
        current_head,
        current_branch,
        current_dirty_path_count,
    ) = row
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(task_id, str)
        or not task_id
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision <= 1
        or not isinstance(state, str)
        or (wait_reason is not None and not isinstance(wait_reason, str))
        or not isinstance(summary, str)
        or not summary
        or (next_step is not None and (not isinstance(next_step, str) or not next_step))
        or not isinstance(created_at, str)
        or not created_at
        or (baseline_head is not None and (not isinstance(baseline_head, str) or not baseline_head))
        or (current_head is not None and (not isinstance(current_head, str) or not current_head))
        or (
            current_branch is not None
            and (not isinstance(current_branch, str) or not current_branch)
        )
        or isinstance(current_dirty_path_count, bool)
        or not isinstance(current_dirty_path_count, int)
        or current_dirty_path_count < 0
    ):
        raise TaskCheckpointError("task checkpoint row has invalid persisted types")
    try:
        task_state = TaskState(state)
    except ValueError as exc:
        raise TaskCheckpointError(f"task checkpoint has unsupported state: {state!r}") from exc
    if task_state is TaskState.CANCELLED:
        raise TaskCheckpointError("task checkpoint cannot persist cancelled state")
    try:
        task_wait_reason = TaskWaitReason(wait_reason) if wait_reason is not None else None
    except ValueError as exc:
        raise TaskCheckpointError(
            f"task checkpoint has unsupported wait_reason: {wait_reason!r}"
        ) from exc
    _validate_checkpoint_state(task_state, task_wait_reason, next_step)
    _validate_text(
        summary,
        label="checkpoint summary",
        maximum_bytes=MAX_CHECKPOINT_SUMMARY_BYTES,
        required=True,
    )
    _validate_text(
        next_step,
        label="checkpoint next_step",
        maximum_bytes=MAX_CHECKPOINT_NEXT_STEP_BYTES,
        required=False,
    )
    return TaskCheckpointRecord(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        task_revision=task_revision,
        state=task_state,
        wait_reason=task_wait_reason,
        summary=summary,
        next_step=next_step,
        created_at=created_at,
        baseline_head=baseline_head,
        current_head=current_head,
        current_branch=current_branch,
        current_dirty_path_count=current_dirty_path_count,
        changed_paths=changed_paths,
    )


def _event_from_row(row: tuple[object, ...]) -> TaskEventRecord:
    event_id, task_id, task_revision, event_type, checkpoint_id, created_at = row
    if (
        isinstance(event_id, bool)
        or not isinstance(event_id, int)
        or event_id <= 0
        or not isinstance(task_id, str)
        or not task_id
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision <= 1
        or not isinstance(event_type, str)
        or not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or not isinstance(created_at, str)
        or not created_at
    ):
        raise TaskCheckpointError("task event row has invalid persisted types")
    try:
        parsed_event_type = TaskEventType(event_type)
    except ValueError as exc:
        raise TaskCheckpointError(f"task event has unsupported type: {event_type!r}") from exc
    return TaskEventRecord(
        event_id=event_id,
        task_id=task_id,
        task_revision=task_revision,
        event_type=parsed_event_type,
        checkpoint_id=checkpoint_id,
        created_at=created_at,
    )


def _validate_expected_revision(expected_revision: int) -> None:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision <= 0
    ):
        raise TaskValidationError("expected_revision must be a positive integer")


def _validate_checkpoint_state(
    state: TaskState,
    wait_reason: TaskWaitReason | None,
    next_step: str | None,
) -> None:
    if not isinstance(state, TaskState):
        raise TaskValidationError("checkpoint state must be a TaskState")
    if state is TaskState.CANCELLED:
        raise TaskValidationError("cancelled is not a checkpoint state")
    if state is TaskState.WAITING:
        if not isinstance(wait_reason, TaskWaitReason):
            raise TaskValidationError("waiting checkpoint requires wait_reason")
        if next_step is None or not isinstance(next_step, str) or not next_step.strip():
            raise TaskValidationError("waiting checkpoint requires next_step")
    elif wait_reason is not None:
        raise TaskValidationError("wait_reason is only valid for waiting checkpoints")


def _validate_text(
    value: str | None,
    *,
    label: str,
    maximum_bytes: int,
    required: bool,
) -> str | None:
    if value is None:
        if required:
            raise TaskValidationError(f"{label} must be non-empty text")
        return None
    if not isinstance(value, str):
        raise TaskValidationError(f"{label} must be text")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise TaskValidationError(f"{label} must be non-empty text")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise TaskValidationError(f"{label} must be valid UTF-8 text") from exc
    if size > maximum_bytes:
        raise TaskValidationError(f"{label} exceeds {maximum_bytes} UTF-8 bytes")
    return normalized


def _utc_timestamp(now: datetime | None) -> str:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise TaskValidationError("Task checkpoint timestamps require a timezone-aware datetime")
    return current.astimezone(UTC).isoformat(timespec="microseconds")
