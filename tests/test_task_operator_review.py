from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.task_workflow as task_workflow
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import (
    TaskEventType,
    get_operator_feedback_for_revision,
    list_task_events,
)
from harness.task_workflow import (
    task_accept,
    task_cancel,
    task_checkpoint,
    task_feedback,
    task_start,
)
from harness.tasks import (
    TaskConflictError,
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    TaskWorkspaceConflictError,
    get_task,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _database(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "tracked.txt")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return connection, workspace.workspace_id


def _waiting_for_review(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    title: str = "Review me",
) -> TaskRecord:
    started = task_start(connection, workspace_id, title)
    return task_checkpoint(
        connection,
        workspace_id,
        started.task_id,
        expected_revision=started.revision,
        state=TaskState.WAITING,
        wait_reason=TaskWaitReason.OPERATOR_REVIEW,
        summary="Implementation ready",
        next_step="Review the result",
    ).task


def test_operator_feedback_resumes_same_task_and_is_pending_only_for_that_revision(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _database(tmp_path)
    try:
        waiting = _waiting_for_review(connection, workspace_id)

        mutation = task_feedback(
            connection,
            workspace_id,
            waiting.task_id,
            expected_revision=waiting.revision,
            feedback="  On mobile the spacing is still too large.  ",
        )

        assert mutation.task.task_id == waiting.task_id
        assert mutation.task.state is TaskState.WORKING
        assert mutation.task.wait_reason is None
        assert mutation.task.revision == waiting.revision + 1
        assert mutation.event.event_type is TaskEventType.OPERATOR_FEEDBACK
        assert mutation.event.task_revision == mutation.task.revision
        assert mutation.event.operator_feedback == "On mobile the spacing is still too large."
        assert (
            get_operator_feedback_for_revision(connection, waiting.task_id, mutation.task.revision)
            == "On mobile the spacing is still too large."
        )

        events = list_task_events(connection, waiting.task_id)
        assert tuple(event.event_type for event in events) == (
            TaskEventType.CREATED,
            TaskEventType.CHECKPOINT,
            TaskEventType.OPERATOR_FEEDBACK,
        )

        checkpointed = task_checkpoint(
            connection,
            workspace_id,
            waiting.task_id,
            expected_revision=mutation.task.revision,
            state=TaskState.WORKING,
            summary="Applied operator feedback",
        )
        assert checkpointed.task.revision == mutation.task.revision + 1
        assert (
            get_operator_feedback_for_revision(
                connection, waiting.task_id, checkpointed.task.revision
            )
            is None
        )
    finally:
        connection.close()


def test_accept_requires_operator_review_wait_and_completes_with_cas(tmp_path: Path) -> None:
    connection, workspace_id = _database(tmp_path)
    try:
        waiting = _waiting_for_review(connection, workspace_id)

        with pytest.raises(TaskRevisionConflictError, match="revision mismatch"):
            task_accept(
                connection,
                workspace_id,
                waiting.task_id,
                expected_revision=waiting.revision - 1,
            )
        assert get_task(connection, waiting.task_id) == waiting

        accepted = task_accept(
            connection,
            workspace_id,
            waiting.task_id,
            expected_revision=waiting.revision,
        )
        assert accepted.task.state is TaskState.COMPLETED
        assert accepted.task.revision == waiting.revision + 1
        assert accepted.event.event_type is TaskEventType.ACCEPTED
        assert accepted.event.operator_feedback is None

        with pytest.raises(TaskTransitionError, match="operator review action"):
            task_accept(
                connection,
                workspace_id,
                accepted.task.task_id,
                expected_revision=accepted.task.revision,
            )
    finally:
        connection.close()


def test_feedback_rejects_non_review_wait_and_distinct_working_task_without_mutation(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _database(tmp_path)
    try:
        first = task_start(connection, workspace_id, "External wait")
        external = task_checkpoint(
            connection,
            workspace_id,
            first.task_id,
            expected_revision=first.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
            summary="Waiting externally",
            next_step="Retry later",
        ).task
        before = list_task_events(connection, first.task_id)
        with pytest.raises(TaskTransitionError, match="operator_review"):
            task_feedback(
                connection,
                workspace_id,
                external.task_id,
                expected_revision=external.revision,
                feedback="Continue now",
            )
        assert get_task(connection, external.task_id) == external
        assert list_task_events(connection, external.task_id) == before

        # Put the first Task into review, then create another working Task. Feedback cannot
        # violate the one-working-Task invariant while resuming the first Task.
        resumed = task_workflow.task_resume(
            connection,
            workspace_id,
            external.task_id,
            expected_revision=external.revision,
        )
        review = task_checkpoint(
            connection,
            workspace_id,
            resumed.task_id,
            expected_revision=resumed.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="First ready",
            next_step="Review first",
        ).task
        second = task_start(connection, workspace_id, "Second task")
        review_events = list_task_events(connection, review.task_id)

        with pytest.raises(TaskConflictError, match="already has a working task"):
            task_feedback(
                connection,
                workspace_id,
                review.task_id,
                expected_revision=review.revision,
                feedback="Change first",
            )
        assert get_task(connection, review.task_id) == review
        assert get_task(connection, second.task_id) == second
        assert list_task_events(connection, review.task_id) == review_events
    finally:
        connection.close()


def test_cancel_accepts_working_or_waiting_but_never_terminal(tmp_path: Path) -> None:
    connection, workspace_id = _database(tmp_path)
    try:
        working = task_start(connection, workspace_id, "Cancel working")
        cancelled = task_cancel(
            connection,
            workspace_id,
            working.task_id,
            expected_revision=working.revision,
        )
        assert cancelled.task.state is TaskState.CANCELLED
        assert cancelled.task.revision == 2
        assert cancelled.event.event_type is TaskEventType.CANCELLED

        with pytest.raises(TaskTransitionError, match="terminal Task"):
            task_cancel(
                connection,
                workspace_id,
                cancelled.task.task_id,
                expected_revision=cancelled.task.revision,
            )

        waiting = _waiting_for_review(connection, workspace_id, title="Cancel review")
        cancelled_waiting = task_cancel(
            connection,
            workspace_id,
            waiting.task_id,
            expected_revision=waiting.revision,
        )
        assert cancelled_waiting.task.state is TaskState.CANCELLED
        assert cancelled_waiting.event.event_type is TaskEventType.CANCELLED
    finally:
        connection.close()


def test_operator_actions_validate_feedback_workspace_and_rollback_event_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, workspace_id = _database(tmp_path)
    try:
        waiting = _waiting_for_review(connection, workspace_id)
        with pytest.raises(TaskValidationError, match="non-empty"):
            task_feedback(
                connection,
                workspace_id,
                waiting.task_id,
                expected_revision=waiting.revision,
                feedback="   ",
            )
        with pytest.raises(TaskValidationError, match="1024"):
            task_feedback(
                connection,
                workspace_id,
                waiting.task_id,
                expected_revision=waiting.revision,
                feedback="é" * 513,
            )

        other_root = tmp_path / "other"
        other_root.mkdir()
        (other_root / "tracked.txt").write_text("other\n", encoding="utf-8")
        _git(other_root, "init", "-b", "main")
        _git(other_root, "add", "tracked.txt")
        _git(
            other_root,
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=h@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "init",
        )
        other_project = create_project(connection)
        other_workspace = register_workspace(
            connection, project_id=other_project.project_id, path=other_root
        )
        with pytest.raises(TaskWorkspaceConflictError, match="does not belong"):
            task_accept(
                connection,
                other_workspace.workspace_id,
                waiting.task_id,
                expected_revision=waiting.revision,
            )

        before_task = get_task(connection, waiting.task_id)
        before_events = list_task_events(connection, waiting.task_id)

        def fail_event(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic operator event failure")

        monkeypatch.setattr(task_workflow, "_insert_operator_event", fail_event)
        with pytest.raises(RuntimeError, match="synthetic operator event failure"):
            task_feedback(
                connection,
                workspace_id,
                waiting.task_id,
                expected_revision=waiting.revision,
                feedback="Must roll back",
            )
        assert get_task(connection, waiting.task_id) == before_task
        assert list_task_events(connection, waiting.task_id) == before_events
    finally:
        connection.close()
