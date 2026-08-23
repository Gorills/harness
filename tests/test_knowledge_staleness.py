from __future__ import annotations

import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

import harness.index as index_module
from harness.index import IndexedFileRecord, scan_workspace
from harness.knowledge import (
    KnowledgeAnchorDraft,
    KnowledgeDraft,
    KnowledgeFreshness,
    KnowledgeKind,
    get_knowledge_card,
)
from harness.registry import WorkspaceRecord, create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskState


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _setup(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    (root / "other.txt").write_text("other\n", encoding="utf-8")
    _git(root, "add", "tracked.txt", "other.txt")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
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
    return root, connection, workspace.workspace_id


def _anchored_card(connection: sqlite3.Connection, workspace_id: str) -> str:
    task = task_start(connection, workspace_id, "Learn")
    mutation = task_checkpoint(
        connection,
        workspace_id,
        task.task_id,
        expected_revision=1,
        state=TaskState.WORKING,
        summary="Learned invariant",
        knowledge=(
            KnowledgeDraft(
                kind=KnowledgeKind.INVARIANT,
                title="Tracked invariant",
                body="This fact depends on tracked.txt.",
                anchors=(KnowledgeAnchorDraft(path="tracked.txt"),),
            ),
        ),
    )
    return mutation.knowledge_cards[0].knowledge_id


def test_matching_scan_keeps_anchored_knowledge_fresh(tmp_path: Path) -> None:
    _root, connection, workspace_id = _setup(tmp_path)
    try:
        knowledge_id = _anchored_card(connection, workspace_id)

        scan_workspace(connection, workspace_id)

        assert get_knowledge_card(connection, knowledge_id).freshness is KnowledgeFreshness.FRESH
    finally:
        connection.close()


def test_scan_snapshot_does_not_stale_knowledge_created_after_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _setup(tmp_path)
    release_scan = threading.Event()
    scan_thread: threading.Thread | None = None
    try:
        task = task_start(connection, workspace_id, "Concurrent Knowledge")
        snapshot_captured = threading.Event()
        original_build_snapshot = index_module._build_snapshot

        def blocked_build_snapshot(
            workspace: WorkspaceRecord,
            *,
            deadline: float | None,
        ) -> dict[str, IndexedFileRecord]:
            snapshot = original_build_snapshot(workspace, deadline=deadline)
            snapshot_captured.set()
            assert release_scan.wait(5)
            return snapshot

        monkeypatch.setattr(index_module, "_build_snapshot", blocked_build_snapshot)
        scan_errors: list[BaseException] = []

        def run_scan() -> None:
            scan_connection = connect_database(tmp_path / "harness.db")
            try:
                scan_workspace(scan_connection, workspace_id)
            except BaseException as exc:  # pragma: no cover - surfaced below
                scan_errors.append(exc)
            finally:
                scan_connection.close()

        scan_thread = threading.Thread(target=run_scan)
        scan_thread.start()
        assert snapshot_captured.wait(5)

        (root / "tracked.txt").write_text("changed after snapshot\n", encoding="utf-8")
        mutation = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Learned after scan snapshot",
            knowledge=(
                KnowledgeDraft(
                    kind=KnowledgeKind.INVARIANT,
                    title="Post-snapshot fact",
                    body="This fact was learned from the changed file.",
                    anchors=(KnowledgeAnchorDraft(path="tracked.txt"),),
                ),
            ),
        )
        knowledge_id = mutation.knowledge_cards[0].knowledge_id
        assert get_knowledge_card(connection, knowledge_id).freshness is KnowledgeFreshness.FRESH

        release_scan.set()
        scan_thread.join(5)
        assert not scan_thread.is_alive()
        assert scan_errors == []
        assert get_knowledge_card(connection, knowledge_id).freshness is KnowledgeFreshness.FRESH
    finally:
        release_scan.set()
        if scan_thread is not None:
            scan_thread.join(5)
        connection.close()


def test_changed_anchor_becomes_stale_and_never_auto_refreshes(tmp_path: Path) -> None:
    root, connection, workspace_id = _setup(tmp_path)
    try:
        knowledge_id = _anchored_card(connection, workspace_id)
        original = get_knowledge_card(connection, knowledge_id)
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

        scan_workspace(connection, workspace_id)

        stale = get_knowledge_card(connection, knowledge_id)
        assert stale.freshness is KnowledgeFreshness.NEEDS_REVALIDATION
        assert stale.updated_at != original.updated_at

        (root / "tracked.txt").write_text("original\n", encoding="utf-8")
        scan_workspace(connection, workspace_id)
        assert (
            get_knowledge_card(connection, knowledge_id).freshness
            is KnowledgeFreshness.NEEDS_REVALIDATION
        )
    finally:
        connection.close()


def test_removed_anchor_marks_knowledge_stale(tmp_path: Path) -> None:
    root, connection, workspace_id = _setup(tmp_path)
    try:
        knowledge_id = _anchored_card(connection, workspace_id)
        (root / "tracked.txt").unlink()

        scan_workspace(connection, workspace_id)

        assert (
            get_knowledge_card(connection, knowledge_id).freshness
            is KnowledgeFreshness.NEEDS_REVALIDATION
        )
    finally:
        connection.close()


def test_unanchored_knowledge_is_not_invalidated_by_file_scans(tmp_path: Path) -> None:
    root, connection, workspace_id = _setup(tmp_path)
    try:
        task = task_start(connection, workspace_id, "Unanchored")
        mutation = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Architecture rationale",
            knowledge=(
                KnowledgeDraft(
                    kind=KnowledgeKind.ARCHITECTURE_RATIONALE,
                    title="Local daemon owns durable state",
                    body="This is a project-level architecture rationale.",
                ),
            ),
        )
        knowledge_id = mutation.knowledge_cards[0].knowledge_id
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (root / "other.txt").unlink()

        scan_workspace(connection, workspace_id)

        assert get_knowledge_card(connection, knowledge_id).freshness is KnowledgeFreshness.FRESH
    finally:
        connection.close()
