from __future__ import annotations

import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import harness.tasks as tasks
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_baseline import (
    TaskBaselineChangedError,
    TaskBaselineFingerprintKind,
    capture_workspace_task_baseline,
    get_task_baseline,
)
from harness.tasks import TaskConflictError, create_task_with_baseline, get_working_task


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _git_text(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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


def _registered(tmp_path: Path, *, scan: bool = True) -> tuple[Path, sqlite3.Connection, str]:
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
    if scan:
        scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id


def test_create_task_with_baseline_captures_git_dirty_and_stale_index(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    started_at = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)
    try:
        (root / "tracked.txt").write_text("pre-existing dirty content\n", encoding="utf-8")
        (root / "untracked.txt").write_text("pre-existing untracked content\n", encoding="utf-8")
        expected_head = _git_text(root, "rev-parse", "HEAD")

        created = create_task_with_baseline(
            connection,
            workspace_id,
            "  Capture baseline  ",
            now=started_at,
        )
        persisted = get_task_baseline(connection, created.task.task_id)

        assert created.task.title == "Capture baseline"
        assert created.task.revision == 1
        assert created.task.created_at == "2026-08-23T05:00:00.000000+00:00"
        assert created.task.updated_at == created.task.created_at
        assert persisted == created.baseline
        assert persisted.snapshot.workspace_id == workspace_id
        assert persisted.snapshot.head == expected_head
        assert persisted.snapshot.branch == "main"
        assert persisted.snapshot.captured_at == created.task.created_at
        assert persisted.snapshot.index_is_fresh is False
        assert persisted.snapshot.index_file_count == 2
        assert len(persisted.snapshot.index_snapshot_sha256) == 64

        dirty = {item.relative_path: item for item in persisted.snapshot.dirty_paths}
        assert set(dirty) == {"tracked.txt", "untracked.txt"}
        assert dirty["tracked.txt"].status_code == " M"
        assert dirty["untracked.txt"].status_code == "??"
        assert dirty["tracked.txt"].original_relative_path is None
        assert dirty["tracked.txt"].fingerprint_kind is TaskBaselineFingerprintKind.FILE
        assert dirty["untracked.txt"].fingerprint_kind is TaskBaselineFingerprintKind.FILE
        assert all(len(item.state_sha256) == 64 for item in dirty.values())
    finally:
        connection.close()


def test_task_baseline_dirty_fingerprint_distinguishes_later_change(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    started_at = datetime(2026, 8, 23, 5, 0, tzinfo=UTC)
    try:
        tracked = root / "tracked.txt"
        tracked.write_text("dirty before task\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Fingerprint", now=started_at)
        before = {
            item.relative_path: item.state_sha256 for item in created.baseline.snapshot.dirty_paths
        }["tracked.txt"]

        tracked.write_text("dirty changed during task\n", encoding="utf-8")
        current = capture_workspace_task_baseline(
            connection,
            workspace_id,
            now=started_at + timedelta(minutes=1),
        )
        after = {item.relative_path: item.state_sha256 for item in current.dirty_paths}[
            "tracked.txt"
        ]

        assert after != before
    finally:
        connection.close()


def test_task_baseline_marks_current_index_fresh_without_mutating_it(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(tmp_path)
    try:
        indexed_before = connection.execute(
            """
            SELECT relative_path, kind, size_bytes, content_sha256
            FROM indexed_files
            WHERE workspace_id = ?
            ORDER BY relative_path
            """,
            (workspace_id,),
        ).fetchall()
        created = create_task_with_baseline(connection, workspace_id, "Fresh index")
        indexed_after = connection.execute(
            """
            SELECT relative_path, kind, size_bytes, content_sha256
            FROM indexed_files
            WHERE workspace_id = ?
            ORDER BY relative_path
            """,
            (workspace_id,),
        ).fetchall()

        assert created.baseline.snapshot.index_is_fresh is True
        assert created.baseline.snapshot.index_file_count == 2
        assert indexed_after == indexed_before
    finally:
        connection.close()


def test_task_baseline_preserves_git_rename_origin(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        _git(root, "mv", "tracked.txt", "renamed.txt")
        created = create_task_with_baseline(connection, workspace_id, "Rename")

        dirty = {item.relative_path: item for item in created.baseline.snapshot.dirty_paths}
        renamed = dirty["renamed.txt"]
        assert renamed.status_code == "R "
        assert renamed.original_relative_path == "tracked.txt"
        assert renamed.fingerprint_kind is TaskBaselineFingerprintKind.FILE
    finally:
        connection.close()


def test_dirty_submodule_baseline_is_explicitly_opaque(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "inner.txt").write_text("initial\n", encoding="utf-8")
    _git(source, "add", "inner.txt")
    _commit(source, "submodule init")

    root, connection, workspace_id = _registered(tmp_path / "parent", scan=False)
    try:
        _git(
            root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(source),
            "vendor/sub",
        )
        _git(root, "add", ".gitmodules", "vendor/sub")
        _commit(root, "add submodule")
        scan_workspace(connection, workspace_id)

        (root / "vendor" / "sub" / "inner.txt").write_text("dirty\n", encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "Dirty submodule")
        dirty = {item.relative_path: item for item in created.baseline.snapshot.dirty_paths}

        assert dirty["vendor/sub"].fingerprint_kind is TaskBaselineFingerprintKind.OPAQUE
        assert len(dirty["vendor/sub"].state_sha256) == 64
    finally:
        connection.close()


def test_baseline_capture_failure_rolls_back_new_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, connection, workspace_id = _registered(tmp_path)

    def fail_capture(*args: object, **kwargs: object) -> object:
        raise TaskBaselineChangedError("fixture race")

    monkeypatch.setattr(tasks, "capture_workspace_task_baseline", fail_capture)
    try:
        with pytest.raises(TaskBaselineChangedError, match="fixture race"):
            create_task_with_baseline(connection, workspace_id, "Must rollback")

        assert get_working_task(connection, workspace_id) is None
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (0,)
    finally:
        connection.close()


def test_second_baseline_task_conflicts_without_extra_baseline(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(tmp_path)
    try:
        first = create_task_with_baseline(connection, workspace_id, "First")
        with pytest.raises(TaskConflictError, match="already has a working task"):
            create_task_with_baseline(connection, workspace_id, "Second")

        assert get_working_task(connection, workspace_id) == first.task
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (1,)
    finally:
        connection.close()


def test_task_baseline_persists_hashes_not_dirty_source_content(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    secret = "BASELINE_SECRET_DO_NOT_PERSIST"
    try:
        (root / "tracked.txt").write_text(secret, encoding="utf-8")
        created = create_task_with_baseline(connection, workspace_id, "No source persistence")

        baseline_row = connection.execute(
            "SELECT * FROM task_baselines WHERE task_id = ?",
            (created.task.task_id,),
        ).fetchone()
        dirty_rows = connection.execute(
            "SELECT * FROM task_baseline_dirty_paths WHERE task_id = ?",
            (created.task.task_id,),
        ).fetchall()
        persisted_text = repr((baseline_row, dirty_rows))

        assert secret not in persisted_text
        assert "tracked.txt" in persisted_text
    finally:
        connection.close()
