from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import uuid4

from harness.knowledge import (
    KnowledgeCardRecord,
    KnowledgeDraft,
    normalize_knowledge_drafts,
    persist_checkpoint_knowledge,
)
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
    TaskWorkspaceConflictError,
    get_task,
)

MAX_CHECKPOINT_SUMMARY_BYTES = 4096
MAX_CHECKPOINT_NEXT_STEP_BYTES = 2048
MAX_OPERATOR_FEEDBACK_BYTES = 1024


class TaskCheckpointError(TaskError):
    """Base class for durable Task checkpoint failures."""


class TaskCheckpointMechanicalError(TaskCheckpointError):
    """Raised when mechanical checkpoint evidence cannot be calculated safely."""


class TaskEventType(StrEnum):
    """Persisted Task event kinds implemented by the public Task domain workflow."""

    CREATED = "created"
    RESUMED = "resumed"
    CHECKPOINT = "checkpoint"
    ACCEPTED = "accepted"
    OPERATOR_FEEDBACK = "operator_feedback"
    CANCELLED = "cancelled"


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
class TaskCheckpointStatusRecord:
    """Bounded latest-checkpoint state used by compact Task status reads."""

    checkpoint_id: str
    task_id: str
    task_revision: int
    state: TaskState
    wait_reason: TaskWaitReason | None
    next_step: str | None


@dataclass(frozen=True, slots=True)
class TaskEventRecord:
    """One immutable Task timeline event persisted by Harness."""

    event_id: int
    task_id: str
    task_revision: int
    event_type: TaskEventType
    checkpoint_id: str | None
    operator_feedback: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskCheckpointMutation:
    """Atomic checkpoint result including any Knowledge created by this checkpoint."""

    task: TaskRecord
    checkpoint: TaskCheckpointRecord
    event: TaskEventRecord
    knowledge_cards: tuple[KnowledgeCardRecord, ...]


def checkpoint_task(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    expected_workspace_id: str | None = None,
    summary: str,
    next_step: str | None = None,
    wait_reason: TaskWaitReason | None = None,
    knowledge: Sequence[KnowledgeDraft] = (),
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
    normalized_knowledge = normalize_knowledge_drafts(knowledge)

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = get_task(connection, task_id)
        if expected_workspace_id is not None and current.workspace_id != expected_workspace_id:
            raise TaskWorkspaceConflictError(
                f"task {task_id} does not belong to workspace {expected_workspace_id}"
            )
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
        knowledge_cards = persist_checkpoint_knowledge(
            connection,
            current,
            checkpoint.checkpoint_id,
            normalized_knowledge,
            timestamp=timestamp,
        )
        event = _insert_checkpoint_event(connection, checkpoint)
        updated = get_task(connection, task_id)
        connection.execute("COMMIT")
        return TaskCheckpointMutation(
            task=updated,
            checkpoint=checkpoint,
            event=event,
            knowledge_cards=knowledge_cards,
        )
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
    *,
    limit: int | None = None,
) -> tuple[TaskCheckpointRecord, ...]:
    """Load checkpoints in monotonic Task-revision order, optionally bounded to the latest rows."""
    get_task(connection, task_id)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise TaskCheckpointError("Task checkpoint list limit must be a positive integer")
    order = "ORDER BY task_revision" if limit is None else "ORDER BY task_revision DESC LIMIT ?"
    parameters: tuple[object, ...] = (task_id,) if limit is None else (task_id, limit)
    rows = connection.execute(
        f"""
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
        {order}
        """,
        parameters,
    )
    records: list[TaskCheckpointRecord] = []
    for row in rows:
        checkpoint_id = row[0]
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise TaskCheckpointError("task checkpoint row has invalid persisted identity")
        records.append(_checkpoint_from_row(row, _load_changed_paths(connection, checkpoint_id)))
    if limit is not None:
        records.reverse()
    return tuple(records)


def get_latest_task_checkpoint_status(
    connection: sqlite3.Connection,
    task_id: str,
) -> TaskCheckpointStatusRecord | None:
    """Load only the latest checkpoint fields required for bounded status continuity."""
    get_task(connection, task_id)
    row = connection.execute(
        """
        SELECT id, task_id, task_revision, state, wait_reason, next_step
        FROM task_checkpoints
        WHERE task_id = ?
        ORDER BY task_revision DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    checkpoint_id, persisted_task_id, task_revision, state, wait_reason, next_step = row
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or persisted_task_id != task_id
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision <= 0
        or not isinstance(state, str)
        or (wait_reason is not None and not isinstance(wait_reason, str))
        or (next_step is not None and not isinstance(next_step, str))
    ):
        raise TaskCheckpointError("latest Task checkpoint status has invalid persisted types")
    try:
        task_state = TaskState(state)
    except ValueError as exc:
        raise TaskCheckpointError(
            f"latest Task checkpoint status has unsupported state: {state!r}"
        ) from exc
    if task_state is TaskState.CANCELLED:
        raise TaskCheckpointError("latest Task checkpoint status cannot persist cancelled state")
    try:
        task_wait_reason = TaskWaitReason(wait_reason) if wait_reason is not None else None
    except ValueError as exc:
        raise TaskCheckpointError(
            f"latest Task checkpoint status has unsupported wait_reason: {wait_reason!r}"
        ) from exc
    _validate_checkpoint_state(task_state, task_wait_reason, next_step)
    normalized_next_step = _validate_text(
        next_step,
        label="checkpoint next_step",
        maximum_bytes=MAX_CHECKPOINT_NEXT_STEP_BYTES,
        required=False,
    )
    return TaskCheckpointStatusRecord(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        task_revision=task_revision,
        state=task_state,
        wait_reason=task_wait_reason,
        next_step=normalized_next_step,
    )


def list_task_events(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    limit: int | None = None,
) -> tuple[TaskEventRecord, ...]:
    """Load durable Task events in database order, optionally bounded to the latest rows."""
    get_task(connection, task_id)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise TaskCheckpointError("Task event list limit must be a positive integer")
    order = "ORDER BY id" if limit is None else "ORDER BY id DESC LIMIT ?"
    parameters: tuple[object, ...] = (task_id,) if limit is None else (task_id, limit)
    rows = connection.execute(
        f"""
        SELECT
            id, task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
        FROM task_events
        WHERE task_id = ?
        {order}
        """,
        parameters,
    )
    records = [_event_from_row(row) for row in rows]
    if limit is not None:
        records.reverse()
    return tuple(records)


def get_operator_feedback_for_revision(
    connection: sqlite3.Connection,
    task_id: str,
    task_revision: int,
) -> str | None:
    """Return operator feedback attached exactly to one Task revision, if present."""
    task = get_task(connection, task_id)
    if isinstance(task_revision, bool) or not isinstance(task_revision, int) or task_revision <= 0:
        raise TaskCheckpointError("operator feedback revision must be a positive integer")
    if task_revision > task.revision:
        raise TaskCheckpointError("operator feedback revision exceeds current Task revision")
    row = connection.execute(
        """
        SELECT operator_feedback
        FROM task_events
        WHERE task_id = ? AND task_revision = ? AND event_type = 'operator_feedback'
        """,
        (task_id, task_revision),
    ).fetchone()
    if row is None:
        return None
    feedback = row[0]
    if not isinstance(feedback, str):
        raise TaskCheckpointError("operator feedback event has invalid persisted text")
    normalized = _validate_text(
        feedback,
        label="operator feedback",
        maximum_bytes=MAX_OPERATOR_FEEDBACK_BYTES,
        required=True,
    )
    assert normalized is not None
    return normalized


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
        INSERT INTO task_events(
            task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
        )
        VALUES (?, ?, 'checkpoint', ?, NULL, ?)
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
        operator_feedback=None,
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
        if not isinstance(relative_path, str):
            raise TaskCheckpointError("Task checkpoint changed path has invalid persisted value")
        paths.append(_validated_changed_path(relative_path))
    return tuple(paths)


def _validated_changed_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise TaskCheckpointError(f"unsafe Task checkpoint changed path: {value!r}")
    return value


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
    (
        event_id,
        task_id,
        task_revision,
        event_type,
        checkpoint_id,
        operator_feedback,
        created_at,
    ) = row
    if (
        isinstance(event_id, bool)
        or not isinstance(event_id, int)
        or event_id <= 0
        or not isinstance(task_id, str)
        or not task_id
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision <= 0
        or not isinstance(event_type, str)
        or (checkpoint_id is not None and (not isinstance(checkpoint_id, str) or not checkpoint_id))
        or (operator_feedback is not None and not isinstance(operator_feedback, str))
        or not isinstance(created_at, str)
        or not created_at
    ):
        raise TaskCheckpointError("task event row has invalid persisted types")
    try:
        parsed_event_type = TaskEventType(event_type)
    except ValueError as exc:
        raise TaskCheckpointError(f"task event has unsupported type: {event_type!r}") from exc
    if parsed_event_type is TaskEventType.CREATED:
        if task_revision != 1 or checkpoint_id is not None or operator_feedback is not None:
            raise TaskCheckpointError("created Task event has invalid persisted linkage")
    elif parsed_event_type is TaskEventType.RESUMED:
        if task_revision <= 1 or checkpoint_id is not None or operator_feedback is not None:
            raise TaskCheckpointError("resumed Task event has invalid persisted linkage")
    elif parsed_event_type is TaskEventType.CHECKPOINT:
        if task_revision <= 1 or checkpoint_id is None or operator_feedback is not None:
            raise TaskCheckpointError("checkpoint Task event has invalid persisted linkage")
    elif parsed_event_type in {TaskEventType.ACCEPTED, TaskEventType.CANCELLED}:
        if task_revision <= 1 or checkpoint_id is not None or operator_feedback is not None:
            raise TaskCheckpointError("operator Task event has invalid persisted linkage")
    else:
        if task_revision <= 1 or checkpoint_id is not None or operator_feedback is None:
            raise TaskCheckpointError("operator feedback event has invalid persisted linkage")
        normalized_feedback = _validate_text(
            operator_feedback,
            label="operator feedback",
            maximum_bytes=MAX_OPERATOR_FEEDBACK_BYTES,
            required=True,
        )
        assert normalized_feedback is not None
        operator_feedback = normalized_feedback
    return TaskEventRecord(
        event_id=event_id,
        task_id=task_id,
        task_revision=task_revision,
        event_type=parsed_event_type,
        checkpoint_id=checkpoint_id,
        operator_feedback=operator_feedback,
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
