from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import harness.task_workflow as task_workflow
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import get_task_baseline
from harness.task_checkpoints import TaskEventType, list_task_checkpoints, list_task_events
from harness.task_workflow import (
    task_checkpoint,
    task_resume,
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


def _commit(cwd: Path, message: str) -> None:
    _git(
        cwd,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        message,
    )


def _register_repo(connection: sqlite3.Connection, root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _commit(root, "init")
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return workspace.workspace_id


def _database(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    workspace_id = _register_repo(connection, tmp_path / "repo")
    return database, connection, workspace_id


def test_task_start_atomically_creates_baseline_and_created_event(tmp_path: Path) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        when = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
        task = task_start(connection, workspace_id, "  Public task  ", now=when)

        assert task.title == "Public task"
        assert task.state is TaskState.WORKING
        assert task.revision == 1
        assert get_task(connection, task.task_id) == task
        assert get_task_baseline(connection, task.task_id).snapshot.captured_at == task.created_at
        events = list_task_events(connection, task.task_id)
        assert len(events) == 1
        assert events[0].event_type is TaskEventType.CREATED
        assert events[0].task_revision == 1
        assert events[0].checkpoint_id is None
        assert events[0].created_at == task.created_at
    finally:
        connection.close()


def test_task_start_rolls_back_task_and_baseline_when_event_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:

        def fail_event(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic event failure")

        monkeypatch.setattr(task_workflow, "_insert_lifecycle_event", fail_event)
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            task_start(connection, workspace_id, "Must roll back")

        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone() == (0,)
    finally:
        connection.close()


def test_task_resume_already_working_is_idempotent_and_read_like(tmp_path: Path) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        task = task_start(connection, workspace_id, "Already working")

        resumed = task_resume(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=999,
        )

        assert resumed == task
        assert get_task(connection, task.task_id) == task
        assert tuple(event.event_type for event in list_task_events(connection, task.task_id)) == (
            TaskEventType.CREATED,
        )
    finally:
        connection.close()


def test_waiting_task_resume_requires_cas_and_persists_one_resumed_event(tmp_path: Path) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        started = task_start(connection, workspace_id, "Resume me")
        waiting = task_checkpoint(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
            summary="Waiting for dependency",
            next_step="Retry after dependency",
        )
        assert waiting.task.revision == 2

        with pytest.raises(TaskValidationError, match="requires expected_revision"):
            task_resume(connection, workspace_id, started.task_id)
        with pytest.raises(TaskRevisionConflictError, match="revision mismatch"):
            task_resume(
                connection,
                workspace_id,
                started.task_id,
                expected_revision=1,
            )

        resumed_at = datetime(2026, 8, 23, 9, 5, tzinfo=UTC)
        resumed = task_resume(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=2,
            now=resumed_at,
        )

        assert resumed.state is TaskState.WORKING
        assert resumed.wait_reason is None
        assert resumed.revision == 3
        assert resumed.updated_at == "2026-08-23T09:05:00.000000+00:00"
        events = list_task_events(connection, started.task_id)
        assert tuple(event.event_type for event in events) == (
            TaskEventType.CREATED,
            TaskEventType.CHECKPOINT,
            TaskEventType.RESUMED,
        )
        assert tuple(event.task_revision for event in events) == (1, 2, 3)
        assert events[-1].checkpoint_id is None
    finally:
        connection.close()


def test_task_resume_rolls_back_state_when_resumed_event_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        started = task_start(connection, workspace_id, "Atomic resume")
        waiting = task_checkpoint(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_INPUT,
            summary="Need input",
            next_step="Wait for answer",
        )
        before_events = list_task_events(connection, started.task_id)

        def fail_event(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic event failure")

        monkeypatch.setattr(task_workflow, "_insert_lifecycle_event", fail_event)
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            task_resume(
                connection,
                workspace_id,
                started.task_id,
                expected_revision=waiting.task.revision,
            )

        assert get_task(connection, started.task_id) == waiting.task
        assert list_task_events(connection, started.task_id) == before_events
    finally:
        connection.close()


def test_task_resume_rejects_terminal_task_and_distinct_working_task(tmp_path: Path) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        first = task_start(connection, workspace_id, "First")
        waiting = task_checkpoint(
            connection,
            workspace_id,
            first.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
            summary="Pause first",
            next_step="Resume later",
        )
        second = task_start(connection, workspace_id, "Second")

        with pytest.raises(TaskConflictError, match="already has a working task"):
            task_resume(
                connection,
                workspace_id,
                first.task_id,
                expected_revision=waiting.task.revision,
            )
        assert get_task(connection, first.task_id) == waiting.task

        completed = task_checkpoint(
            connection,
            workspace_id,
            second.task_id,
            expected_revision=1,
            state=TaskState.COMPLETED,
            summary="Second done",
        )
        with pytest.raises(TaskTransitionError, match="terminal Task"):
            task_resume(
                connection,
                workspace_id,
                second.task_id,
                expected_revision=completed.task.revision,
            )
    finally:
        connection.close()


def test_public_checkpoint_checks_workspace_ownership_inside_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        workspace_a = _register_repo(connection, tmp_path / "repo-a")
        workspace_b = _register_repo(connection, tmp_path / "repo-b")
        task = task_start(connection, workspace_a, "Scoped checkpoint")
        original_get_task = get_task
        transaction_states: list[bool] = []

        def guarded_get_task(conn: sqlite3.Connection, task_id: str) -> TaskRecord:
            transaction_states.append(conn.in_transaction)
            return original_get_task(conn, task_id)

        monkeypatch.setattr("harness.task_checkpoints.get_task", guarded_get_task)
        with pytest.raises(TaskWorkspaceConflictError, match="does not belong"):
            task_checkpoint(
                connection,
                workspace_b,
                task.task_id,
                expected_revision=task.revision,
                state=TaskState.WORKING,
                summary="Wrong workspace",
            )

        assert transaction_states == [True]
        assert get_task(connection, task.task_id) == task
        assert list_task_checkpoints(connection, task.task_id) == ()
    finally:
        connection.close()


def test_public_mutations_reject_task_from_different_workspace(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        workspace_a = _register_repo(connection, tmp_path / "repo-a")
        workspace_b = _register_repo(connection, tmp_path / "repo-b")
        task = task_start(connection, workspace_a, "Scoped")
        before = list_task_events(connection, task.task_id)

        with pytest.raises(TaskWorkspaceConflictError, match="does not belong"):
            task_resume(connection, workspace_b, task.task_id)
        with pytest.raises(TaskWorkspaceConflictError, match="does not belong"):
            task_checkpoint(
                connection,
                workspace_b,
                task.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Wrong workspace",
            )

        assert get_task(connection, task.task_id) == task
        assert list_task_checkpoints(connection, task.task_id) == ()
        assert list_task_events(connection, task.task_id) == before
    finally:
        connection.close()


def test_public_checkpoint_keeps_explicit_identity_across_sequential_revisions(
    tmp_path: Path,
) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        started_at = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
        started = task_start(connection, workspace_id, "Checkpoint", now=started_at)
        first = task_checkpoint(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Progress",
            now=started_at + timedelta(minutes=1),
        )

        with pytest.raises(TaskRevisionConflictError, match="revision mismatch"):
            task_checkpoint(
                connection,
                workspace_id,
                started.task_id,
                expected_revision=1,
                state=TaskState.COMPLETED,
                summary="Stale",
            )

        assert get_task(connection, started.task_id) == first.task
        events = list_task_events(connection, started.task_id)
        assert tuple(event.event_type for event in events) == (
            TaskEventType.CREATED,
            TaskEventType.CHECKPOINT,
        )
        assert tuple(event.task_revision for event in events) == (1, 2)
    finally:
        connection.close()
