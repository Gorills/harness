from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from harness.git_applicability import WorkspaceApplicability, persist_initial_handoff
from harness.knowledge import KnowledgeDraft
from harness.registry import get_workspace
from harness.task_checkpoints import (
    MAX_JIRA_URL_BYTES,
    MAX_OPERATOR_COMMENT_BYTES,
    MAX_OPERATOR_FEEDBACK_BYTES,
    TaskCheckpointMutation,
    TaskEventRecord,
    TaskEventType,
)
from harness.task_checkpoints import (
    checkpoint_task as _checkpoint_task,
)
from harness.tasks import (
    TaskOperatorStatus,
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    TaskWorkspaceConflictError,
    _advance_task_revision_in_transaction,
    _create_task_with_baseline_in_transaction,
    _reopen_task_in_transaction,
    _transition_task_state_in_transaction,
    _utc_timestamp,
    _validate_expected_revision,
    _validate_state_reason,
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
    """Resume the explicit Task, with CAS for waiting or initial branch handoff."""
    get_workspace(connection, workspace_id)
    if expected_revision is not None:
        _validate_expected_revision(expected_revision)
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        applicability = WorkspaceApplicability(connection, workspace_id)
        handoff = not applicability.task_visible(task_id)
        if handoff and not applicability.initial_handoff_allowed(task_id):
            raise TaskWorkspaceConflictError("Task is unavailable in the active Git context")
        if current.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
            raise TaskTransitionError(f"cannot resume terminal Task in state {current.state.value}")
        if current.state is TaskState.WORKING and not handoff:
            applicability.validate()
            connection.execute("COMMIT")
            return current
        if expected_revision is None:
            raise TaskValidationError(
                "resuming a waiting Task or initial branch handoff requires expected_revision"
            )
        _require_expected_revision(current, expected_revision)
        if handoff:
            persist_initial_handoff(connection, task_id, applicability)
        if current.state is TaskState.WORKING:
            updated = _advance_task_revision_in_transaction(
                connection, task_id, expected_revision=expected_revision, timestamp=timestamp
            )
        else:
            updated = _transition_task_state_in_transaction(
                connection,
                task_id,
                expected_revision=expected_revision,
                state=TaskState.WORKING,
                wait_reason=None,
                timestamp=timestamp,
            )
        _insert_lifecycle_event(connection, updated, TaskEventType.RESUMED)
        applicability.validate()
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
    """Accept the explicit Task from any state; completion belongs to the operator."""
    return task_set_state(
        connection,
        workspace_id,
        task_id,
        expected_revision=expected_revision,
        state=TaskState.COMPLETED,
        now=now,
    )


def task_set_state(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    wait_reason: TaskWaitReason | None = None,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """Choose any lifecycle state explicitly as operator, preserving CAS and one working Task."""
    _validate_expected_revision(expected_revision)
    if not isinstance(state, TaskState) or (
        wait_reason is not None and not isinstance(wait_reason, TaskWaitReason)
    ):
        raise TaskValidationError("operator Task state or wait reason is unsupported")
    _validate_state_reason(state, wait_reason)
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        if state is TaskState.WORKING:
            existing = connection.execute(
                "SELECT id FROM tasks WHERE workspace_id = ? AND state = 'working' AND id <> ?",
                (workspace_id, task_id),
            ).fetchone()
            if existing is not None:
                from harness.tasks import TaskConflictError

                raise TaskConflictError("workspace already has a working task")
        connection.execute(
            "UPDATE tasks SET state = ?, wait_reason = ?, revision = revision + 1, "
            "updated_at = ? WHERE id = ? AND revision = ?",
            (
                state.value,
                None if wait_reason is None else wait_reason.value,
                timestamp,
                task_id,
                expected_revision,
            ),
        )
        updated = get_task(connection, task_id)
        event = _insert_operator_event(
            connection,
            updated,
            TaskEventType.ACCEPTED if state is TaskState.COMPLETED else TaskEventType.STATE_CHANGED,
            target_state=None if state is TaskState.COMPLETED else state,
            target_wait_reason=wait_reason,
        )
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_delete(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
) -> None:
    """Permanently remove a Task and its provenance Knowledge, never repository files."""
    _validate_expected_revision(expected_revision)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        connection.execute("DELETE FROM knowledge_cards WHERE source_task_id = ?", (task_id,))
        cursor = connection.execute(
            "DELETE FROM tasks WHERE id = ? AND revision = ?", (task_id, expected_revision)
        )
        if cursor.rowcount != 1:
            raise TaskRevisionConflictError("task revision changed during deletion")
        connection.execute("COMMIT")
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


def task_reopen(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """CAS-reopen one explicit terminal Task and append durable history."""
    _validate_expected_revision(expected_revision)
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        updated = _reopen_task_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            timestamp=timestamp,
        )
        event = _insert_operator_event(connection, updated, TaskEventType.REOPENED)
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_comment(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    comment: str,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """Append one operator note to any Task state under revision CAS."""
    _validate_expected_revision(expected_revision)
    normalized_comment = _validate_operator_text(
        comment,
        label="operator comment",
        maximum_bytes=MAX_OPERATOR_COMMENT_BYTES,
    )
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        updated = _advance_task_revision_in_transaction(
            connection,
            task_id,
            expected_revision=expected_revision,
            timestamp=timestamp,
        )
        event = _insert_operator_event(
            connection,
            updated,
            TaskEventType.OPERATOR_COMMENT,
            operator_comment=normalized_comment,
        )
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def task_set_jira_url(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    jira_url: str | None,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """Set or clear the Task's current Jira work-item link under revision CAS."""
    _validate_expected_revision(expected_revision)
    normalized_url = _validate_jira_url(jira_url)
    return _mutate_operator_metadata(
        connection,
        workspace_id,
        task_id,
        expected_revision=expected_revision,
        column="jira_url",
        value=normalized_url,
        event_type=TaskEventType.JIRA_LINK_UPDATED,
        now=now,
    )


def task_set_operator_status(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    operator_status: TaskOperatorStatus | None,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """Set or clear the human-owned delivery marker independently from lifecycle state."""
    _validate_expected_revision(expected_revision)
    if operator_status is not None and not isinstance(operator_status, TaskOperatorStatus):
        raise TaskValidationError("operator_status is unsupported")
    return task_set_deployment(
        connection,
        workspace_id,
        task_id,
        expected_revision=expected_revision,
        deploy_test=operator_status
        in {TaskOperatorStatus.DEPLOY_TEST, TaskOperatorStatus.DEPLOY_BOTH},
        deploy_prod=operator_status
        in {TaskOperatorStatus.DEPLOY_PROD, TaskOperatorStatus.DEPLOY_BOTH},
        now=now,
    )


def task_set_deployment(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    deploy_test: bool,
    deploy_prod: bool,
    now: datetime | None = None,
) -> TaskOperatorMutation:
    """Atomically set both independent deployment flags with an immutable snapshot."""
    _validate_expected_revision(expected_revision)
    if type(deploy_test) is not bool or type(deploy_prod) is not bool:
        raise TaskValidationError("deployment flags must be booleans")
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        connection.execute(
            "UPDATE tasks SET deploy_test = ?, deploy_prod = ?, operator_status = NULL, "
            "revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?",
            (int(deploy_test), int(deploy_prod), timestamp, task_id, expected_revision),
        )
        updated = get_task(connection, task_id)
        event = _insert_operator_event(
            connection,
            updated,
            TaskEventType.OPERATOR_STATUS_UPDATED,
            deploy_test=deploy_test,
            deploy_prod=deploy_prod,
        )
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
    return _validate_operator_text(
        feedback,
        label="operator feedback",
        maximum_bytes=MAX_OPERATOR_FEEDBACK_BYTES,
    )


def _validate_operator_text(value: str, *, label: str, maximum_bytes: int) -> str:
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


def _validate_jira_url(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    normalized = _validate_operator_text(
        value,
        label="Jira URL",
        maximum_bytes=MAX_JIRA_URL_BYTES,
    )
    if any(character.isspace() or ord(character) < 0x20 for character in normalized):
        raise TaskValidationError("Jira URL must not contain whitespace or control characters")
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
    except ValueError as exc:
        raise TaskValidationError("Jira URL is malformed") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TaskValidationError("Jira URL must be an http(s) URL without credentials")
    return normalized


def _mutate_operator_metadata(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    *,
    expected_revision: int,
    column: str,
    value: str | None,
    event_type: TaskEventType,
    now: datetime | None,
) -> TaskOperatorMutation:
    if (column, event_type) not in {
        ("jira_url", TaskEventType.JIRA_LINK_UPDATED),
        ("operator_status", TaskEventType.OPERATOR_STATUS_UPDATED),
    }:
        raise TaskValidationError("unsupported operator metadata mutation")
    timestamp = _utc_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = _task_in_workspace(connection, workspace_id, task_id)
        _require_expected_revision(current, expected_revision)
        cursor = connection.execute(
            f"""
            UPDATE tasks
            SET {column} = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (value, timestamp, task_id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise TaskRevisionConflictError(
                f"task revision changed during operator mutation: expected {expected_revision}"
            )
        updated = get_task(connection, task_id)
        event = _insert_operator_event(
            connection,
            updated,
            event_type,
            jira_url=value if column == "jira_url" else None,
            operator_status=(
                TaskOperatorStatus(value)
                if column == "operator_status" and value is not None
                else None
            ),
        )
        connection.execute("COMMIT")
        return TaskOperatorMutation(task=updated, event=event)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _insert_operator_event(
    connection: sqlite3.Connection,
    task: TaskRecord,
    event_type: TaskEventType,
    *,
    operator_feedback: str | None = None,
    operator_comment: str | None = None,
    jira_url: str | None = None,
    operator_status: TaskOperatorStatus | None = None,
    deploy_test: bool | None = None,
    deploy_prod: bool | None = None,
    target_state: TaskState | None = None,
    target_wait_reason: TaskWaitReason | None = None,
) -> TaskEventRecord:
    if event_type not in {
        TaskEventType.ACCEPTED,
        TaskEventType.STATE_CHANGED,
        TaskEventType.OPERATOR_FEEDBACK,
        TaskEventType.OPERATOR_COMMENT,
        TaskEventType.JIRA_LINK_UPDATED,
        TaskEventType.OPERATOR_STATUS_UPDATED,
        TaskEventType.REOPENED,
        TaskEventType.CANCELLED,
    }:
        raise TaskValidationError(f"unsupported operator event type: {event_type.value}")
    if event_type is TaskEventType.OPERATOR_FEEDBACK and operator_feedback is None:
        raise TaskValidationError("operator feedback event requires feedback")
    if event_type is TaskEventType.OPERATOR_COMMENT and operator_comment is None:
        raise TaskValidationError("operator comment event requires comment")
    payload_count = sum(
        value is not None
        for value in (operator_feedback, operator_comment, jira_url, operator_status)
    )
    expected_payload_count = int(
        event_type
        in {
            TaskEventType.OPERATOR_FEEDBACK,
            TaskEventType.OPERATOR_COMMENT,
        }
        or (event_type is TaskEventType.JIRA_LINK_UPDATED and jira_url is not None)
        or (event_type is TaskEventType.OPERATOR_STATUS_UPDATED and operator_status is not None)
    )
    if payload_count != expected_payload_count:
        raise TaskValidationError("operator event payload does not match event type")
    cursor = connection.execute(
        """
        INSERT INTO task_events(
            task_id, task_revision, event_type, checkpoint_id, operator_feedback,
            operator_comment, jira_url, operator_status, created_at,
            deploy_test, deploy_prod, target_state, target_wait_reason
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            task.revision,
            event_type.value,
            operator_feedback,
            operator_comment,
            jira_url,
            None if operator_status is None else operator_status.value,
            task.updated_at,
            None if deploy_test is None else int(deploy_test),
            None if deploy_prod is None else int(deploy_prod),
            None if target_state is None else target_state.value,
            None if target_wait_reason is None else target_wait_reason.value,
        ),
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
        operator_comment=operator_comment,
        jira_url=jira_url,
        operator_status=updated_status(deploy_test, deploy_prod, operator_status),
        created_at=task.updated_at,
        target_state=target_state,
        target_wait_reason=target_wait_reason,
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
        operator_comment=None,
        jira_url=None,
        operator_status=None,
        created_at=task.updated_at,
    )


def updated_status(
    deploy_test: bool | None, deploy_prod: bool | None, legacy: TaskOperatorStatus | None
) -> TaskOperatorStatus | None:
    if deploy_test is None:
        return legacy
    if deploy_test and deploy_prod:
        return TaskOperatorStatus.DEPLOY_BOTH
    if deploy_test:
        return TaskOperatorStatus.DEPLOY_TEST
    return TaskOperatorStatus.DEPLOY_PROD if deploy_prod else None
