from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.task_baseline as task_baseline
import harness.task_changes as task_changes
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import (
    TaskBaselineDirtyPath,
    TaskBaselineFingerprintKind,
    TaskBaselineTimeoutError,
    TaskGitState,
    capture_task_git_state,
    get_task_baseline,
)
from harness.task_changes import (
    TaskChangedFilesChangedError,
    TaskChangedFilesError,
    calculate_task_changed_files,
)
from harness.tasks import create_task_with_baseline, get_task


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


def _registered(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("tracked baseline\n", encoding="utf-8")
    (root / "stable.txt").write_text("stable\n", encoding="utf-8")
    _git(root, "add", "tracked.txt", "stable.txt")
    _commit(root, "init")

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id


def test_changed_files_empty_for_unchanged_clean_task(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "No changes")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.task_id == created.task.task_id
        assert changed.workspace_id == workspace_id
        assert changed.baseline_head == created.baseline.snapshot.head
        assert changed.current_head == created.baseline.snapshot.head
        assert changed.relative_paths == ()
        assert get_task(connection, created.task.task_id).revision == 1
    finally:
        connection.close()


def test_changed_files_include_new_worktree_edits(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Worktree changes")
        (root / "tracked.txt").write_text("task edit\n", encoding="utf-8")
        (root / "untracked.txt").write_text("task new\n", encoding="utf-8")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ("tracked.txt", "untracked.txt")
    finally:
        connection.close()


def test_changed_files_exclude_identical_preexisting_dirty_state(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "tracked.txt").write_text("dirty before task\n", encoding="utf-8")
        (root / "preexisting.txt").write_text("already here\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Preserve dirty")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ()
    finally:
        connection.close()


def test_changed_files_include_preexisting_dirty_path_modified_again(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        tracked = root / "tracked.txt"
        tracked.write_text("dirty before task\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Dirty again")
        tracked.write_text("changed during task\n", encoding="utf-8")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ("tracked.txt",)
    finally:
        connection.close()


def test_changed_files_include_preexisting_dirty_path_that_becomes_clean(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "tracked.txt").write_text("dirty before task\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Clean dirty")
        _git(root, "restore", "tracked.txt")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ("tracked.txt",)
    finally:
        connection.close()


def test_changed_files_detect_staged_blob_change_with_identical_mm_worktree(
    tmp_path: Path,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        tracked = root / "tracked.txt"
        tracked.write_text("staged before task\n", encoding="utf-8")
        _git(root, "add", "tracked.txt")
        tracked.write_text("same worktree bytes\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Index state")

        tracked.write_text("different staged blob\n", encoding="utf-8")
        _git(root, "add", "tracked.txt")
        tracked.write_text("same worktree bytes\n", encoding="utf-8")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ("tracked.txt",)
    finally:
        connection.close()


def test_changed_files_include_committed_tree_change_with_clean_worktree(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Commit change")
        (root / "tracked.txt").write_text("committed during task\n", encoding="utf-8")
        _git(root, "add", "tracked.txt")
        _commit(root, "task commit")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.current_head != changed.baseline_head
        assert changed.relative_paths == ("tracked.txt",)
    finally:
        connection.close()


def test_changed_files_report_both_sides_of_committed_rename(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Committed rename")
        _git(root, "mv", "tracked.txt", "renamed.txt")
        _commit(root, "rename")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ("renamed.txt", "tracked.txt")
    finally:
        connection.close()


def test_changed_files_report_both_sides_of_task_time_rename(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Rename")
        _git(root, "mv", "tracked.txt", "renamed.txt")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ("renamed.txt", "tracked.txt")
    finally:
        connection.close()


def test_changed_files_exclude_unchanged_preexisting_rename(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        _git(root, "mv", "tracked.txt", "renamed.txt")
        created = create_task_with_baseline(connection, workspace_id, "Existing rename")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.relative_paths == ()
    finally:
        connection.close()


def test_changed_files_include_opaque_baseline_conservatively() -> None:
    opaque = TaskBaselineDirtyPath(
        relative_path="vendor/sub",
        original_relative_path=None,
        status_code=" M",
        fingerprint_kind=TaskBaselineFingerprintKind.OPAQUE,
        state_sha256="a" * 64,
    )
    current = TaskGitState(head="head", branch="main", dirty_paths=(opaque,))

    changed = task_changes._merge_changed_paths(
        (opaque,),
        current,
        frozenset(),
        deadline=float("inf"),
    )

    assert changed == ("vendor/sub",)


def test_changed_files_handle_unborn_baseline_then_first_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    try:
        created = create_task_with_baseline(connection, workspace.workspace_id, "Unborn")
        assert created.baseline.snapshot.head is None
        (root / "first.txt").write_text("first commit\n", encoding="utf-8")
        _git(root, "add", "first.txt")
        _commit(root, "first")

        changed = calculate_task_changed_files(connection, created.task.task_id)

        assert changed.current_head is not None
        assert changed.relative_paths == ("first.txt",)
    finally:
        connection.close()


def test_changed_files_fail_closed_when_task_baseline_is_missing(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(tmp_path)
    try:
        created = create_task_with_baseline(connection, workspace_id, "Missing baseline")
        connection.execute("DELETE FROM task_baselines WHERE task_id = ?", (created.task.task_id,))
        connection.commit()

        with pytest.raises(TaskChangedFilesError, match="baseline read failed"):
            calculate_task_changed_files(connection, created.task.task_id)
    finally:
        connection.close()


def test_task_baseline_reader_honors_changed_file_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "tracked.txt").write_text("dirty before task\n", encoding="utf-8")
        (root / "preexisting.txt").write_text("already here\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Bounded read")
        observed_times = iter((0.0, 0.0, 0.0, 0.0, 5.0))
        monkeypatch.setattr(task_baseline, "monotonic", lambda: next(observed_times))

        with pytest.raises(TaskBaselineTimeoutError, match="operation deadline exceeded"):
            get_task_baseline(connection, created.task.task_id, deadline=5.0)
    finally:
        connection.close()


def test_changed_files_fail_if_git_state_changes_during_calculation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    created = create_task_with_baseline(connection, workspace_id, "Race")
    real_capture = capture_task_git_state
    calls = 0

    def capture_with_change(workspace_root: Path, *, deadline: float) -> TaskGitState:
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "stable.txt").write_text("raced\n", encoding="utf-8")
        return real_capture(workspace_root, deadline=deadline)

    monkeypatch.setattr(task_changes, "capture_task_git_state", capture_with_change)
    try:
        with pytest.raises(TaskChangedFilesChangedError, match="Git state changed"):
            calculate_task_changed_files(connection, created.task.task_id)
    finally:
        connection.close()
