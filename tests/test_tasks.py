from __future__ import annotations

import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.tasks import (
    MAX_TASK_TITLE_BYTES,
    TaskConflictError,
    TaskRevisionConflictError,
    TaskState,
    TaskTransitionError,
    TaskValidationError,
    TaskWaitReason,
    create_task_record,
    get_task,
    get_working_task,
    transition_task_state,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _workspace_database(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
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
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        return database, workspace.workspace_id
    finally:
        connection.close()


def test_create_task_record_is_bounded_and_enforces_one_working_task(tmp_path: Path) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    created_at = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)
    connection = connect_database(database)
    try:
        task = create_task_record(
            connection,
            workspace_id,
            "  Fix refresh token race  ",
            now=created_at,
        )
        assert task.workspace_id == workspace_id
        assert task.title == "Fix refresh token race"
        assert task.state is TaskState.WORKING
        assert task.wait_reason is None
        assert task.revision == 1
        assert task.created_at == "2026-08-23T04:00:00.000000+00:00"
        assert task.updated_at == task.created_at
        assert get_task(connection, task.task_id) == task
        assert get_working_task(connection, workspace_id) == task

        with pytest.raises(TaskConflictError, match="already has a working task"):
            create_task_record(connection, workspace_id, "Second task", now=created_at)
        assert get_task(connection, task.task_id) == task
    finally:
        connection.close()


def test_task_title_bounds_use_utf8_bytes(tmp_path: Path) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    connection = connect_database(database)
    try:
        with pytest.raises(TaskValidationError, match="non-empty"):
            create_task_record(connection, workspace_id, "   ")
        with pytest.raises(TaskValidationError, match="non-empty"):
            create_task_record(connection, workspace_id, "bad\x00title")
        with pytest.raises(TaskValidationError, match=str(MAX_TASK_TITLE_BYTES)):
            create_task_record(connection, workspace_id, "é" * (MAX_TASK_TITLE_BYTES // 2 + 1))
    finally:
        connection.close()


def test_task_state_transition_requires_revision_and_wait_reason(tmp_path: Path) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    started_at = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "Review UI", now=started_at)
        waiting = transition_task_state(
            connection,
            task.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            now=started_at + timedelta(minutes=1),
        )
        assert waiting.state is TaskState.WAITING
        assert waiting.wait_reason is TaskWaitReason.OPERATOR_REVIEW
        assert waiting.revision == 2
        assert waiting.updated_at == "2026-08-23T04:01:00.000000+00:00"
        assert get_working_task(connection, workspace_id) is None

        working = transition_task_state(
            connection,
            task.task_id,
            expected_revision=2,
            state=TaskState.WORKING,
            now=started_at + timedelta(minutes=2),
        )
        assert working.state is TaskState.WORKING
        assert working.wait_reason is None
        assert working.revision == 3

        with pytest.raises(TaskValidationError, match="requires wait_reason"):
            transition_task_state(
                connection,
                task.task_id,
                expected_revision=3,
                state=TaskState.WAITING,
            )
        with pytest.raises(TaskValidationError, match="only valid for waiting"):
            transition_task_state(
                connection,
                task.task_id,
                expected_revision=3,
                state=TaskState.COMPLETED,
                wait_reason=TaskWaitReason.EXTERNAL,
            )
        assert get_task(connection, task.task_id) == working
    finally:
        connection.close()


def test_task_revision_conflict_is_non_mutating(tmp_path: Path) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "CAS")
        waiting = transition_task_state(
            connection,
            task.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.EXTERNAL,
        )

        with pytest.raises(TaskRevisionConflictError, match="expected 1, current 2"):
            transition_task_state(
                connection,
                task.task_id,
                expected_revision=1,
                state=TaskState.COMPLETED,
            )
        assert get_task(connection, task.task_id) == waiting
    finally:
        connection.close()


def test_terminal_task_states_cannot_be_reopened(tmp_path: Path) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "Finish")
        completed = transition_task_state(
            connection,
            task.task_id,
            expected_revision=1,
            state=TaskState.COMPLETED,
        )
        assert completed.revision == 2

        with pytest.raises(TaskTransitionError, match="completed -> working"):
            transition_task_state(
                connection,
                task.task_id,
                expected_revision=2,
                state=TaskState.WORKING,
            )
        assert get_task(connection, task.task_id) == completed
    finally:
        connection.close()


def test_workspace_unique_working_constraint_blocks_resume_into_other_working_task(
    tmp_path: Path,
) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    connection = connect_database(database)
    try:
        first = create_task_record(connection, workspace_id, "First")
        waiting = transition_task_state(
            connection,
            first.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_INPUT,
        )
        second = create_task_record(connection, workspace_id, "Second")

        with pytest.raises(TaskConflictError, match="already has a working task"):
            transition_task_state(
                connection,
                waiting.task_id,
                expected_revision=2,
                state=TaskState.WORKING,
            )
        assert get_task(connection, waiting.task_id) == waiting
        assert get_working_task(connection, workspace_id) == second
    finally:
        connection.close()


def test_parallel_same_revision_writers_cannot_both_commit(tmp_path: Path) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "Parallel writers")
    finally:
        connection.close()

    barrier = Barrier(2)

    def mutate(target: TaskState) -> str:
        worker = connect_database(database)
        try:
            barrier.wait(timeout=5)
            try:
                transition_task_state(
                    worker,
                    task.task_id,
                    expected_revision=1,
                    state=target,
                    wait_reason=(
                        TaskWaitReason.EXTERNAL if target is TaskState.WAITING else None
                    ),
                )
            except TaskRevisionConflictError:
                return "conflict"
            return "committed"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(mutate, (TaskState.WAITING, TaskState.COMPLETED)))
    assert outcomes == ["committed", "conflict"]

    connection = connect_database(database)
    try:
        persisted = get_task(connection, task.task_id)
        assert persisted.revision == 2
        assert persisted.state in {TaskState.WAITING, TaskState.COMPLETED}
    finally:
        connection.close()


def test_database_constraint_rejects_two_working_rows_even_outside_domain_api(
    tmp_path: Path,
) -> None:
    database, workspace_id = _workspace_database(tmp_path)
    connection = connect_database(database)
    try:
        task = create_task_record(connection, workspace_id, "Protected")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO tasks(
                    id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
                ) VALUES ('other', ?, 'Other', 'working', NULL, 1, 'now', 'now')
                """,
                (workspace_id,),
            )
        assert get_working_task(connection, workspace_id) == task
    finally:
        connection.close()
