from __future__ import annotations

import sqlite3
from datetime import datetime

from harness.registry import get_workspace
from harness.task_checkpoints import (
    TaskCheckpointMutation,
    TaskEventRecord,
    TaskEventType,
)
from harness.task_checkpoints import (
    checkpoint_task as _checkpoint_task,
)
from harness.tasks import (
    TaskRecord,
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


def task_start(
    connection: sqlite3.Connection,
    workspace_id: str,
    title: str,
    *,
    now: datetime | None = None,
) -> TaskRecord:
    """Create a public-domain working Task, baseline, and creation event atomically."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        created = _create_task_with_baseline_in_transaction(
            connection,
            workspace_id,
            title,
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
        now=now,
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
        INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, created_at)
        VALUES (?, ?, ?, NULL, ?)
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
        created_at=task.updated_at,
    )
