from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from harness.index import scan_workspace
from harness.knowledge import KnowledgeAnchorDraft, KnowledgeDraft, KnowledgeKind
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectSearchScope, search_project
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskState


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", ".")
    _git(
        cwd,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "commit",
        "-m",
        message,
    )


def _worktrees(tmp_path: Path) -> tuple[sqlite3.Connection, Path, str, Path, str]:
    main_root = tmp_path / "main"
    feature_root = tmp_path / "feature"
    main_root.mkdir()
    _git(main_root, "init", "-b", "main")
    (main_root / "service.py").write_text(
        "def retry_delay(attempt):\n    return attempt\n",
        encoding="utf-8",
    )
    _commit(main_root, "base")
    _git(main_root, "worktree", "add", "-b", "feature", str(feature_root))
    (feature_root / "service.py").write_text(
        "def retry_delay(attempt):\n    return 2 ** attempt\n",
        encoding="utf-8",
    )
    _commit(feature_root, "feature retry")

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    main_workspace = register_workspace(connection, project_id=project.project_id, path=main_root)
    feature_workspace = register_workspace(
        connection, project_id=project.project_id, path=feature_root
    )
    scan_workspace(connection, main_workspace.workspace_id)
    scan_workspace(connection, feature_workspace.workspace_id)
    return (
        connection,
        main_root,
        main_workspace.workspace_id,
        feature_root,
        feature_workspace.workspace_id,
    )


def test_anchored_agent_knowledge_requires_matching_active_workspace(tmp_path: Path) -> None:
    connection, main_root, main_workspace_id, _feature_root, feature_workspace_id = _worktrees(
        tmp_path
    )
    try:
        task = task_start(connection, feature_workspace_id, "Learn retry behavior")
        mutation = task_checkpoint(
            connection,
            feature_workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Retry behavior learned",
            knowledge=(
                KnowledgeDraft(
                    kind=KnowledgeKind.BEHAVIOR,
                    title="Exponential retry delay",
                    body="Retry delay doubles for each attempt.",
                    anchors=(KnowledgeAnchorDraft(path="service.py"),),
                ),
            ),
        )
        knowledge_ref = f"knowledge:{mutation.knowledge_cards[0].knowledge_id}"

        feature_hits = search_project(
            connection,
            feature_workspace_id,
            "exponential retry delay",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=5,
        )
        main_hits = search_project(
            connection,
            main_workspace_id,
            "exponential retry delay",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=5,
        )
        main_all_hits = search_project(
            connection,
            main_workspace_id,
            "exponential retry delay",
            scope=ProjectSearchScope.ALL,
            limit=5,
        )

        assert [hit.ref for hit in feature_hits] == [knowledge_ref]
        assert knowledge_ref not in {hit.ref for hit in main_hits}
        assert knowledge_ref not in {hit.ref for hit in main_all_hits}

        _git(main_root, "merge", "--ff-only", "feature")
        scan_workspace(connection, main_workspace_id)

        merged_hits = search_project(
            connection,
            main_workspace_id,
            "exponential retry delay",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=5,
        )
        assert [hit.ref for hit in merged_hits] == [knowledge_ref]
    finally:
        connection.close()


def test_unanchored_agent_knowledge_requires_explicit_knowledge_scope(tmp_path: Path) -> None:
    connection, _main_root, main_workspace_id, _feature_root, _feature_workspace_id = _worktrees(
        tmp_path
    )
    try:
        task = task_start(connection, main_workspace_id, "Learn project rationale")
        mutation = task_checkpoint(
            connection,
            main_workspace_id,
            task.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="Architecture rationale learned",
            knowledge=(
                KnowledgeDraft(
                    kind=KnowledgeKind.ARCHITECTURE_RATIONALE,
                    title="Daemon owns durable project state",
                    body="Durable project state is intentionally daemon-owned.",
                ),
            ),
        )
        knowledge_ref = f"knowledge:{mutation.knowledge_cards[0].knowledge_id}"

        all_hits = search_project(
            connection,
            main_workspace_id,
            "daemon durable project state",
            scope=ProjectSearchScope.ALL,
            limit=5,
        )
        explicit_hits = search_project(
            connection,
            main_workspace_id,
            "daemon durable project state",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=5,
        )

        assert knowledge_ref not in {hit.ref for hit in all_hits}
        assert knowledge_ref in {hit.ref for hit in explicit_hits}
    finally:
        connection.close()
