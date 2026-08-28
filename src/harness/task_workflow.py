from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from harness.knowledge import KnowledgeDraft
from harness.registry import get_workspace
from harness.task_checkpoints import (
    MAX_OPERATOR_FEEDBACK_BYTES,
    TaskCheckpointMutation,
    TaskEventRecord,
    TaskEventType,
)
from harness.task_checkpoints import (
    checkpoint_task as _checkpoint_task,
)
from harness.tasks import (
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    TaskWorkspaceConflictError,
    _create_task_with_baseline_in_transaction,
    _transition_task_state_in_transaction,
    _utc_timestamp,
    _validate_expected_revision,
    get_task,
)
from harness.verification import VerificationDraft


@dataclass(frozen=True, slots=True)
class TaskOperatorMutation:
    """Atomic human-review Task mutation plus its immutable history event."""

    task: TaskRecord
    event: TaskEventRecord


def task_start(
    connection: sqlite3.Connection,
    workspace_id: str,
    title: str,
    *,
    stack_hints: tuple[str, ...] = (),
    now: datetime | None = None,
) -> TaskRecord:
    """Create a public-domain working Task, baseline, and creation event atomically."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        created = _create_task_with_baseline_in_transaction(
            connection,
            workspace_id,
            title,
            stack_hints=stack_hints,
            now=now,
        )
        _insert_lifecycle_event(connection, created.task, TaskEventType.CREATED)
        connection.execute("COMMIT")
        return created.task
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_resume(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> TaskRecord:
    """Idempotently attach to working Task or CAS-resume a waiting Task."""
    get_workspace(connection, workspace_id)
    current = _task_in_workspace(connection, workspace_id, task_id)
    if current.state is TaskState.WORKING:
        return current
    if current.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
        raise TaskTransitionError(f"cannot resume terminal Task in state {current.state.value}")
    if expected_revision is None:
        raise TaskValidationError("resuming a waiting Task requires expected_revision")
    _validate_expected_revision(expected_revision)
    timestamp = _utc_timestamp(now)

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        if current.state is TaskState.WORKING:
            connection.execute("COMMIT")
            return current
        if current.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
            raise TaskTransitionError(f"cannot resume terminal Task in state {current.state.value}")
        updated = _transition_task_state_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            state=TaskState.WORKING,
            wait_reason=None,
            timestamp=timestamp,
        )
        _insert_lifecycle_event(connection, updated, TaskEventType.RESUMED)
        connection.execute("COMMIT")
        return updated
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_checkpoint(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    summary: str,
    next_step: str | None = None,
    wait_reason: TaskWaitReason | None = None,
    verification: Sequence[VerificationDraft] = (),
    knowledge: Sequence[KnowledgeDraft] = (),
    now: datetime | None = None,
) -> TaskCheckpointMutation:
    """Checkpoint the explicit Task only after verifying immutable Workspace ownership."""
    return _checkpoint_task(
        connection,
        task_id,
        expected_revision=expected_revision,
        state=state,
        expected_workspace_id=workspace_id,
        summary=summary,
        next_step=next_step,
        wait_reason=wait_reason,
        verification=verification,
        knowledge=knowledge,
        now=now,
    )


def task_accept(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """CAS-complete one Task waiting specifically for operator review."""
    _validate_expected_revision(expected_revision)
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        _require_operator_review_wait(current)
        updated = _transition_task_state_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            state=TaskState.COMPLETED,
            wait_reason=None,
            timestamp=timestamp,
        )
        event = _insert_operator_event(connection, updated, TaskEventType.ACCEPTED)
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_feedback(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    feedback: str,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """Persist operator feedback and CAS-resume the same review Task atomically."""
    _validate_expected_revision(expected_revision)
    normalized_feedback = _validate_operator_feedback(feedback)
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        _require_operator_review_wait(current)
        updated = _transition_task_state_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            state=TaskState.WORKING,
            wait_reason=None,
            timestamp=timestamp,
        )
        event = _insert_operator_event(
            connection,
            updated,
            TaskEventType.OPERATOR_FEEDBACK,
            operator_feedback=normalized_feedback,
        )
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_cancel(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """CAS-cancel one explicit non-terminal Task and append durable history."""
    _validate_expected_revision(expected_revision)
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        if current.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
            raise TaskTransitionError(f"cannot cancel terminal Task in state {current.state.value}")
        updated = _transition_task_state_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            state=TaskState.CANCELLED,
            wait_reason=None,
            timestamp=timestamp,
        )
        event = _insert_operator_event(connection, updated, TaskEventType.CANCELLED)
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _require_expected_revision(task: TaskRecord, expected_revision: int) -> None:
    if task.revision != expected_revision:
        raise TaskRevisionConflictError(
            f"task revision mismatch: expected {expected_revision}, current {task.revision}"
        )


def _require_operator_review_wait(task: TaskRecord) -> None:
    if (
        task.state is not TaskState.WAITING
        or task.wait_reason is not TaskWaitReason.OPERATOR_REVIEW
    ):
        raise TaskTransitionError(
            "operator review action requires Task waiting for operator_review"
        )


def _validate_operator_feedback(feedback: str) -> str:
    if not isinstance(feedback, str):
        raise TaskValidationError("operator feedback must be text")
    normalized = feedback.strip()
    if not normalized or "\x00" in normalized:
        raise TaskValidationError("operator feedback must be non-empty text")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise TaskValidationError("operator feedback must be valid UTF-8 text") from exc
    if size > MAX_OPERATOR_FEEDBACK_BYTES:
        raise TaskValidationError(
            f"operator feedback exceeds {MAX_OPERATOR_FEEDBACK_BYTES} UTF-8 bytes"
        )
    return normalized


def _insert_operator_event(
    connection: sqlite3.Connection,
    task: TaskRecord,
    event_type: TaskEventType,
    *,
    operator_feedback: str | None = None,
) -> TaskEventRecord:
    if event_type not in {
        TaskEventType.ACCEPTED,
        TaskEventType.OPERATOR_FEEDBACK,
        TaskEventType.CANCELLED,
    }:
        raise TaskValidationError(f"unsupported operator event type: {event_type.value}")
    if (event_type is TaskEventType.OPERATOR_FEEDBACK) != (operator_feedback is not None):
        raise TaskValidationError("operator feedback event payload does not match event type")
    cursor = connection.execute(
        """
        INSERT INTO task_events(
            task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
        ) VALUES (?, ?, ?, NULL, ?, ?)
        """,
        (task.task_id, task.revision, event_type.value, operator_feedback, task.updated_at),
    )
    event_id = cursor.lastrowid
    if not isinstance(event_id, int) or event_id <= 0:
        raise TaskValidationError("Task operator event did not receive a valid identity")
    return TaskEventRecord(
        event_id=event_id,
        task_id=task.task_id,
        task_revision=task.revision,
        event_type=event_type,
        checkpoint_id=None,
        operator_feedback=operator_feedback,
        created_at=task.updated_at,
    )


def _task_in_workspace(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
) -> TaskRecord:
    task = get_task(connection, task_id)
    if task.workspace_id != workspace_id:
        raise TaskWorkspaceConflictError(
            f"task {task_id} does not belong to workspace {workspace_id}"
        )
    return task


def _insert_lifecycle_event(
    connection: sqlite3.Connection,
    task: TaskRecord,
    event_type: TaskEventType,
) -> TaskEventRecord:
    if event_type not in {TaskEventType.CREATED, TaskEventType.RESUMED}:
        raise TaskValidationError(f"unsupported lifecycle event type: {event_type.value}")
    cursor = connection.execute(
        """
        INSERT INTO task_events(
            task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at
        )
        VALUES (?, ?, ?, NULL, NULL, ?)
        """,
        (task.task_id, task.revision, event_type.value, task.updated_at),
    )
    event_id = cursor.lastrowid
    if not isinstance(event_id, int) or event_id <= 0:
        raise TaskValidationError("Task lifecycle event did not receive a valid identity")
    return TaskEventRecord(
        event_id=event_id,
        task_id=task.task_id,
        task_revision=task.revision,
        event_type=event_type,
        checkpoint_id=None,
        operator_feedback=None,
        created_at=task.updated_at,
    )
