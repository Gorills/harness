from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from time import monotonic

import pytest

import harness.task_checkpoints as task_checkpoints
from harness.index import scan_workspace
from harness.knowledge import (
    KnowledgeAnchorDraft,
    KnowledgeAnchorError,
    KnowledgeDraft,
    KnowledgeFreshness,
    KnowledgeKind,
    KnowledgeSourceType,
    KnowledgeValidationError,
    _capture_anchor_fingerprint,
    get_knowledge_card,
    list_project_knowledge,
)
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import list_task_checkpoints, list_task_events
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskRevisionConflictError, TaskState, get_task


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _setup(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("SOURCE_SECRET_TOKEN\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
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
    return database, root, connection, project.project_id, workspace.workspace_id


def _draft(path: str = "tracked.txt") -> KnowledgeDraft:
    return KnowledgeDraft(
        kind=KnowledgeKind.INVARIANT,
        title="  Token replacement is atomic  ",
        body="  Replacement commits before the old token becomes invalid.  ",
        anchors=(KnowledgeAnchorDraft(path=path, symbol="  replace_token  "),),
    )


def test_anchor_capture_rejects_replaced_workspace_root_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.txt").write_text("inside\n", encoding="utf-8")
    moved = tmp_path / "moved-repo"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)

    with pytest.raises(KnowledgeAnchorError, match="root is no longer canonical"):
        _capture_anchor_fingerprint(root, "tracked.txt", deadline=monotonic() + 1.0)


def test_task_checkpoint_atomically_persists_provenance_and_mechanical_anchor(
    tmp_path: Path,
) -> None:
    database, root, connection, project_id, workspace_id = _setup(tmp_path)
    try:
        task = task_start(connection, workspace_id, "Learn invariant")
        mutation = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Investigated token replacement",
            knowledge=(_draft(),),
        )

        assert mutation.task.revision == 2
        assert len(mutation.knowledge_cards) == 1
        card = mutation.knowledge_cards[0]
        assert get_knowledge_card(connection, card.knowledge_id) == card
        assert list_project_knowledge(connection, project_id) == (card,)
        assert card.project_id == project_id
        assert card.kind is KnowledgeKind.INVARIANT
        assert card.title == "Token replacement is atomic"
        assert card.body == "Replacement commits before the old token becomes invalid."
        assert card.source_type is KnowledgeSourceType.AGENT_ASSERTED
        assert card.source_task_id == task.task_id
        assert card.source_checkpoint_id == mutation.checkpoint.checkpoint_id
        assert card.created_at == mutation.checkpoint.created_at == card.updated_at
        assert card.freshness is KnowledgeFreshness.FRESH
        assert len(card.anchors) == 1
        anchor = card.anchors[0]
        assert anchor.workspace_id == workspace_id
        assert anchor.relative_path == "tracked.txt"
        assert anchor.symbol == "replace_token"
        assert (
            anchor.content_sha256 == hashlib.sha256((root / "tracked.txt").read_bytes()).hexdigest()
        )
        assert b"SOURCE_SECRET_TOKEN" not in database.read_bytes()
    finally:
        connection.close()


def test_stale_checkpoint_revision_never_persists_knowledge(tmp_path: Path) -> None:
    _database, _root, connection, project_id, workspace_id = _setup(tmp_path)
    try:
        task = task_start(connection, workspace_id, "CAS")
        first = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="First",
        )

        with pytest.raises(TaskRevisionConflictError, match="revision mismatch"):
            task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Stale",
                knowledge=(_draft(),),
            )

        assert get_task(connection, task.task_id) == first.task
        assert list_project_knowledge(connection, project_id) == ()
        assert len(list_task_checkpoints(connection, task.task_id)) == 1
    finally:
        connection.close()


def test_invalid_anchor_rolls_back_task_checkpoint_event_and_knowledge(tmp_path: Path) -> None:
    _database, _root, connection, project_id, workspace_id = _setup(tmp_path)
    try:
        task = task_start(connection, workspace_id, "Bad anchor")
        before_events = list_task_events(connection, task.task_id)

        with pytest.raises(KnowledgeAnchorError, match="does not exist"):
            task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Must roll back",
                knowledge=(_draft("missing.py"),),
            )

        assert get_task(connection, task.task_id) == task
        assert list_task_checkpoints(connection, task.task_id) == ()
        assert list_task_events(connection, task.task_id) == before_events
        assert list_project_knowledge(connection, project_id) == ()
    finally:
        connection.close()


def test_event_failure_after_knowledge_insert_rolls_back_entire_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, _root, connection, project_id, workspace_id = _setup(tmp_path)
    try:
        task = task_start(connection, workspace_id, "Atomic knowledge")
        before_events = list_task_events(connection, task.task_id)

        def fail_event(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic event failure")

        monkeypatch.setattr(task_checkpoints, "_insert_checkpoint_event", fail_event)
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Must roll back",
                knowledge=(_draft(),),
            )

        assert get_task(connection, task.task_id) == task
        assert list_task_checkpoints(connection, task.task_id) == ()
        assert list_task_events(connection, task.task_id) == before_events
        assert list_project_knowledge(connection, project_id) == ()
    finally:
        connection.close()


def test_symlink_anchor_hashes_link_text_without_reading_external_target(tmp_path: Path) -> None:
    database, root, connection, _project_id, workspace_id = _setup(tmp_path)
    try:
        outside = tmp_path / "outside.txt"
        outside.write_text("EXTERNAL_SECRET_CONTENT\n", encoding="utf-8")
        link = root / "outside-link"
        link.symlink_to(outside)
        task = task_start(connection, workspace_id, "Symlink knowledge")

        mutation = task_checkpoint(
            connection,
            workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Observed symlink",
            knowledge=(
                KnowledgeDraft(
                    kind=KnowledgeKind.OPERATIONAL_DETAIL,
                    title="Link target",
                    body="The repository uses a link for this integration.",
                    anchors=(KnowledgeAnchorDraft(path="outside-link"),),
                ),
            ),
        )

        anchor = mutation.knowledge_cards[0].anchors[0]
        expected = hashlib.sha256(b"symlink\0" + str(outside).encode()).hexdigest()
        assert anchor.content_sha256 == expected
        assert b"EXTERNAL_SECRET_CONTENT" not in database.read_bytes()
    finally:
        connection.close()


def test_checkpoint_knowledge_batch_is_bounded_before_task_mutation(tmp_path: Path) -> None:
    _database, _root, connection, project_id, workspace_id = _setup(tmp_path)
    try:
        task = task_start(connection, workspace_id, "Bounds")
        too_many = tuple(
            KnowledgeDraft(kind=KnowledgeKind.CAVEAT, title=f"Card {index}", body="Body")
            for index in range(9)
        )

        with pytest.raises(KnowledgeValidationError, match="exceeds 8 cards"):
            task_checkpoint(
                connection,
                workspace_id,
                task.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="No mutation",
                knowledge=too_many,
            )

        assert get_task(connection, task.task_id) == task
        assert list_project_knowledge(connection, project_id) == ()
    finally:
        connection.close()
