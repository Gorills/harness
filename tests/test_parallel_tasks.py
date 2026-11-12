from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

import harness.storage as storage
from harness.daemon import read_workspace_task_status
from harness.dashboard import read_dashboard_workspace_detail, render_workspace_page
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import get_task_baseline
from harness.task_checkpoints import list_task_events
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskState, TaskWaitReason, get_task
from harness.workspace_resolution import WorkspaceHint


def _setup(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    database = tmp_path / "harness.db"
    initialize_database(database)
    with closing(connect_database(database)) as connection:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
    return root, database, workspace.workspace_id


def test_concurrent_distinct_tasks_keep_independent_baselines_and_history(tmp_path: Path) -> None:
    root, database, workspace = _setup(tmp_path)
    barrier = Barrier(2)

    def start(title: str) -> str:
        with closing(connect_database(database)) as connection:
            barrier.wait(timeout=5)
            return task_start(connection, workspace, title).task_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = tuple(executor.map(start, ("First outcome", "Second outcome")))
    assert len(set(ids)) == 2
    with closing(connect_database(database)) as connection:
        for task_id in ids:
            assert get_task(connection, task_id).revision == 1
            assert get_task_baseline(connection, task_id).task_id == task_id
            assert len(list_task_events(connection, task_id)) == 1

        checkpoint = task_checkpoint(
            connection,
            workspace,
            ids[0],
            expected_revision=1,
            state=TaskState.WORKING,
            summary="First progress",
            next_step="Continue first",
        )
        assert get_task(connection, ids[1]).revision == 1
        assert len(list_task_events(connection, ids[1])) == 1
        status = read_workspace_task_status(connection, [WorkspaceHint(root, "test")])
        assert status.task is None
        assert status.last_checkpoint is None
        assert status.pending_operator_feedback is None
        assert status.active_task_count == 2
        assert {task.task_id for task in status.active_tasks} == set(ids)
        selected = read_workspace_task_status(
            connection, [WorkspaceHint(root, "test")], task_id=ids[0]
        )
        assert selected.task is not None and selected.task.task_id == ids[0]
        assert selected.last_checkpoint is not None
        assert selected.last_checkpoint.checkpoint_id == checkpoint.checkpoint.checkpoint_id
        other = read_workspace_task_status(
            connection, [WorkspaceHint(root, "test")], task_id=ids[1]
        )
        assert other.task is not None and other.task.revision == 1
        assert other.last_checkpoint is None

    detail = read_dashboard_workspace_detail(database, workspace)
    assert detail.workspace.task_id is None
    assert detail.workspace.task_selection_required
    assert detail.workspace.active_task_count == 2
    assert {row.task.task_id for row in detail.recent_tasks} == set(ids)
    html = render_workspace_page(detail, base_path="/")
    assert "First outcome" in html and "Second outcome" in html
    assert "Несколько задач" in html


def test_status_bounds_active_list_without_selecting_latest_task(tmp_path: Path) -> None:
    root, database, workspace = _setup(tmp_path)
    with closing(connect_database(database)) as connection:
        tasks = [task_start(connection, workspace, f"Outcome {i}") for i in range(8)]
        status = read_workspace_task_status(connection, [WorkspaceHint(root, "test")])
        assert status.active_task_count == 8
        assert len(status.active_tasks) == 5
        assert status.task is None
        oldest = read_workspace_task_status(
            connection, [WorkspaceHint(root, "test")], task_id=tasks[0].task_id
        )
        assert oldest.task is not None and oldest.task.task_id == tasks[0].task_id


def test_multiple_waiting_tasks_do_not_supply_an_arbitrary_continuation(tmp_path: Path) -> None:
    root, database, workspace = _setup(tmp_path)
    with closing(connect_database(database)) as connection:
        for title in ("First", "Second"):
            task = task_start(connection, workspace, title)
            task_checkpoint(
                connection,
                workspace,
                task.task_id,
                expected_revision=1,
                state=TaskState.WAITING,
                wait_reason=TaskWaitReason.OPERATOR_REVIEW,
                summary=title,
                next_step=f"Review {title}",
            )
        status = read_workspace_task_status(connection, [WorkspaceHint(root, "test")])
        assert status.task is None and status.last_checkpoint is None
        assert status.active_task_count == 2


def test_v24_migration_preserves_task_history_and_allows_parallel_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as legacy:
        legacy.setattr(storage, "SCHEMA_VERSION", 24)
        root, database, workspace = _setup(tmp_path)
        with closing(connect_database(database)) as connection:
            first = task_start(connection, workspace, "Preserved outcome")
            checkpoint = task_checkpoint(
                connection,
                workspace,
                first.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Preserved history",
            )
            baseline = get_task_baseline(connection, first.task_id)
            events = list_task_events(connection, first.task_id)
    assert initialize_database(database).schema_version == 25
    assert initialize_database(database).schema_version == 25
    with closing(connect_database(database)) as connection:
        assert get_task(connection, first.task_id) == checkpoint.task
        assert get_task_baseline(connection, first.task_id) == baseline
        assert list_task_events(connection, first.task_id) == events
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        second = task_start(connection, workspace, "New independent outcome")
        assert second.state is TaskState.WORKING
        assert get_task(connection, first.task_id) == checkpoint.task
        status = read_workspace_task_status(connection, [WorkspaceHint(root, "test")])
        assert status.active_task_count == 2


def test_failed_parallel_migration_rolls_back_indexes_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as legacy:
        legacy.setattr(storage, "SCHEMA_VERSION", 24)
        _root, database, workspace = _setup(tmp_path)
        with closing(connect_database(database)) as connection:
            task = task_start(connection, workspace, "Preserved during failed migration")
            events = list_task_events(connection, task.task_id)
    original = storage._apply_migration

    def fail_after_index_change(connection: sqlite3.Connection, target_version: int) -> None:
        original(connection, target_version)
        if target_version == 25:
            raise sqlite3.OperationalError("synthetic migration failure")

    with monkeypatch.context() as failing:
        failing.setattr(storage, "_apply_migration", fail_after_index_change)
        with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
            initialize_database(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (24,)
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(tasks)")}
        assert "tasks_one_working_per_workspace_idx" in indexes
        assert "tasks_active_workspace_idx" not in indexes
        assert connection.execute(
            "SELECT revision FROM tasks WHERE id = ?", (task.task_id,)
        ).fetchone() == (1,)
    initialize_database(database)
    with closing(connect_database(database)) as connection:
        assert get_task(connection, task.task_id) == task
        assert list_task_events(connection, task.task_id) == events
