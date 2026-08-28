from __future__ import annotations

import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

import harness.task_checkpoints as task_checkpoints
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_changes import TaskChangedFilesError
from harness.task_checkpoints import (
    MAX_CHECKPOINT_SUMMARY_BYTES,
    TaskCheckpointError,
    TaskCheckpointMechanicalError,
    checkpoint_task,
    get_task_checkpoint,
    list_task_checkpoints,
    list_task_events,
)
from harness.tasks import (
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    create_task_with_baseline,
    get_task,
)
from harness.verification import (
    VerificationDraft,
    VerificationSource,
    VerificationStatus,
    VerificationValidationError,
    list_checkpoint_verification,
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


def _registered(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _commit(root, "init")

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, database, connection, workspace.workspace_id


def test_working_checkpoint_persists_mechanical_snapshot_event_and_revision(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Checkpoint")
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (root / "new.txt").write_text("new\n", encoding="utf-8")
        when = datetime(2026, 8, 23, 8, 30, tzinfo=UTC)

        result = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="  Meaningful progress  ",
            next_step="  Finish verification  ",
            now=when,
        )

        assert result.task.revision == 2
        assert result.task.state is TaskState.WORKING
        assert result.task.updated_at == "2026-08-23T08:30:00.000000+00:00"
        assert result.checkpoint.task_revision == 2
        assert result.checkpoint.summary == "Meaningful progress"
        assert result.checkpoint.next_step == "Finish verification"
        assert result.checkpoint.baseline_head == created.baseline.snapshot.head
        assert result.checkpoint.current_head == created.baseline.snapshot.head
        assert result.checkpoint.current_branch == "main"
        assert result.checkpoint.current_dirty_path_count == 2
        assert result.checkpoint.changed_paths == ("new.txt", "tracked.txt")
        assert result.event.task_revision == 2
        assert result.event.checkpoint_id == result.checkpoint.checkpoint_id
        assert get_task_checkpoint(connection, result.checkpoint.checkpoint_id) == result.checkpoint
        assert list_task_checkpoints(connection, created.task.task_id) == (result.checkpoint,)
        assert list_task_events(connection, created.task.task_id) == (result.event,)
    finally:
        connection.close()


def test_sequential_working_checkpoints_increment_revision_and_preserve_event_order(
    tmp_path: Path,
) -> None:
    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Progress")
        (root / "tracked.txt").write_text("first change\n", encoding="utf-8")
        first = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="First milestone",
        )
        (root / "new.txt").write_text("second change\n", encoding="utf-8")
        second = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=2,
            state=TaskState.WORKING,
            summary="Second milestone",
        )

        assert first.task.revision == 2
        assert second.task.revision == 3
        assert first.checkpoint.changed_paths == ("tracked.txt",)
        assert second.checkpoint.changed_paths == ("new.txt", "tracked.txt")
        assert tuple(
            item.task_revision for item in list_task_checkpoints(connection, created.task.task_id)
        ) == (
            2,
            3,
        )
        events = list_task_events(connection, created.task.task_id)
        assert tuple(item.task_revision for item in events) == (2, 3)
        assert events[0].event_id < events[1].event_id
        assert list_task_checkpoints(connection, created.task.task_id, limit=1) == (
            second.checkpoint,
        )
        assert list_task_events(connection, created.task.task_id, limit=1) == (second.event,)
        with pytest.raises(TaskCheckpointError, match="positive integer"):
            list_task_events(connection, created.task.task_id, limit=0)
        with pytest.raises(TaskCheckpointError, match="positive integer"):
            list_task_checkpoints(connection, created.task.task_id, limit=True)
    finally:
        connection.close()


def test_waiting_checkpoint_requires_reason_and_next_step(tmp_path: Path) -> None:
    _root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Wait")

        with pytest.raises(TaskValidationError, match="requires wait_reason"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=1,
                state=TaskState.WAITING,
                summary="Need review",
                next_step="Review output",
            )
        with pytest.raises(TaskValidationError, match="requires next_step"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=1,
                state=TaskState.WAITING,
                wait_reason=TaskWaitReason.OPERATOR_REVIEW,
                summary="Need review",
            )

        result = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Ready for review",
            next_step="Operator reviews result",
        )

        assert result.task.state is TaskState.WAITING
        assert result.task.wait_reason is TaskWaitReason.OPERATOR_REVIEW
        assert result.task.revision == 2
        assert result.checkpoint.wait_reason is TaskWaitReason.OPERATOR_REVIEW
    finally:
        connection.close()


def test_checkpoint_requires_current_working_task(tmp_path: Path) -> None:
    _root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Wait once")
        first = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
            summary="Waiting externally",
            next_step="Wait for dependency",
        )

        with pytest.raises(TaskTransitionError, match="requires working Task"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=2,
                state=TaskState.WAITING,
                wait_reason=TaskWaitReason.EXTERNAL,
                summary="Still waiting",
                next_step="Keep waiting",
            )

        assert get_task(connection, created.task.task_id).revision == 2
        assert list_task_checkpoints(connection, created.task.task_id) == (first.checkpoint,)
        assert list_task_events(connection, created.task.task_id) == (first.event,)
    finally:
        connection.close()


def test_completed_checkpoint_persists_committed_changed_path(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Complete")
        (root / "tracked.txt").write_text("finished\n", encoding="utf-8")
        _git(root, "add", "tracked.txt")
        _commit(root, "finish")

        result = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.COMPLETED,
            summary="Finished task",
        )

        assert result.task.state is TaskState.COMPLETED
        assert result.task.revision == 2
        assert result.checkpoint.current_head != result.checkpoint.baseline_head
        assert result.checkpoint.current_dirty_path_count == 0
        assert result.checkpoint.changed_paths == ("tracked.txt",)
    finally:
        connection.close()


def test_stale_checkpoint_is_fully_non_mutating(tmp_path: Path) -> None:
    _root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "CAS")
        first = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="First",
        )

        with pytest.raises(TaskRevisionConflictError, match="revision mismatch"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=1,
                state=TaskState.COMPLETED,
                summary="Stale overwrite",
            )

        assert get_task(connection, created.task.task_id) == first.task
        assert list_task_checkpoints(connection, created.task.task_id) == (first.checkpoint,)
        assert list_task_events(connection, created.task.task_id) == (first.event,)
    finally:
        connection.close()


def test_mechanical_failure_rolls_back_checkpoint_state_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Rollback")

        def fail_calculation(_connection: sqlite3.Connection, _task_id: str) -> None:
            raise TaskChangedFilesError("synthetic failure")

        monkeypatch.setattr(task_checkpoints, "calculate_task_changed_files", fail_calculation)
        with pytest.raises(TaskCheckpointMechanicalError, match="mechanical"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=1,
                state=TaskState.COMPLETED,
                summary="Should not persist",
            )

        task = get_task(connection, created.task.task_id)
        assert task.state is TaskState.WORKING
        assert task.revision == 1
        assert list_task_checkpoints(connection, created.task.task_id) == ()
        assert list_task_events(connection, created.task.task_id) == ()
    finally:
        connection.close()


def test_two_checkpoint_writers_cannot_commit_same_revision(tmp_path: Path) -> None:
    _root, database, connection, workspace_id = _registered(tmp_path)
    created = create_task_with_baseline(connection, workspace_id, "Parallel")
    task_id = created.task.task_id
    connection.close()

    def write(summary: str) -> str:
        worker = connect_database(database)
        try:
            try:
                checkpoint_task(
                    worker,
                    task_id,
                    expected_revision=1,
                    state=TaskState.WORKING,
                    summary=summary,
                )
            except TaskRevisionConflictError:
                return "conflict"
            return "committed"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(write, ("Writer A", "Writer B")))

    assert outcomes == ["committed", "conflict"]
    verification = connect_database(database)
    try:
        assert get_task(verification, task_id).revision == 2
        assert len(list_task_checkpoints(verification, task_id)) == 1
        assert len(list_task_events(verification, task_id)) == 1
    finally:
        verification.close()


def test_checkpoint_text_is_bounded_utf8(tmp_path: Path) -> None:
    _root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Bounds")
        valid = "é" * (MAX_CHECKPOINT_SUMMARY_BYTES // 2)
        result = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary=valid,
        )
        assert result.checkpoint.summary == valid
    finally:
        connection.close()

    _root, _database, connection, workspace_id = _registered(tmp_path / "overflow")
    try:
        created = create_task_with_baseline(connection, workspace_id, "Overflow")
        with pytest.raises(TaskValidationError, match="exceeds"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="é" * (MAX_CHECKPOINT_SUMMARY_BYTES // 2 + 1),
            )
        assert get_task(connection, created.task.task_id).revision == 1
    finally:
        connection.close()


def test_checkpoint_persists_bounded_agent_reported_verification_atomically(tmp_path: Path) -> None:
    _root, _database, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Verified checkpoint")
        result = checkpoint_task(
            connection,
            created.task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Verification recorded",
            verification=(
                VerificationDraft(
                    "focused tests", VerificationStatus.PASSED, "pytest target: passed"
                ),
                VerificationDraft(
                    "real host acceptance",
                    VerificationStatus.NOT_RUN,
                    "Proprietary host unavailable",
                ),
            ),
        )
        persisted = list_checkpoint_verification(connection, result.checkpoint.checkpoint_id)
        assert result.verification == persisted
        assert all(item.source is VerificationSource.AGENT_REPORTED for item in persisted)
        with pytest.raises(VerificationValidationError, match="unique"):
            checkpoint_task(
                connection,
                created.task.task_id,
                expected_revision=2,
                state=TaskState.WORKING,
                summary="duplicate",
                verification=(
                    VerificationDraft("Tests", VerificationStatus.PASSED, "ok"),
                    VerificationDraft(" tests ", VerificationStatus.FAILED, "failed"),
                ),
            )
        assert get_task(connection, created.task.task_id).revision == 2
        assert len(list_task_checkpoints(connection, created.task.task_id)) == 1
    finally:
        connection.close()
