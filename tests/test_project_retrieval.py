from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import (
    ProjectRetrievalRefError,
    read_project_context,
)
from harness.storage import connect_database, initialize_database
from harness.task_baseline import capture_workspace_task_baseline, persist_task_baseline


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _workspace(connection: sqlite3.Connection, root: Path) -> tuple[str, str]:
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "refresh_token.py").write_text(
        """
def rotateRefreshToken(repository, previous_credential):
    \"\"\"Invalidate the previous credential through one atomic replacement.\"\"\"
    return repository.replace(previous_credential)
""".lstrip(),
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "refresh-rotation.md").write_text("rotation docs\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-m", "init")
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return project.project_id, workspace.workspace_id


def _knowledge(
    connection: sqlite3.Connection,
    *,
    knowledge_id: str,
    project_id: str,
    title: str,
    body: str,
    freshness: str = "fresh",
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_cards(
            id, project_id, kind, title, body, source_type,
            created_at, updated_at, freshness
        ) VALUES (?, ?, 'invariant', ?, ?, 'operator', 'created', 'updated', ?)
        """,
        (knowledge_id, project_id, title, body, freshness),
    )


def _task_history(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    workspace_id: str,
    title: str,
    summary: str,
    feedback: str,
) -> tuple[str, int]:
    connection.execute(
        """
        INSERT INTO tasks(
            id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
        ) VALUES (?, ?, ?, 'working', NULL, 3, 'created', 'updated')
        """,
        (task_id, workspace_id, title),
    )
    checkpoint_id = f"checkpoint-{task_id}"
    baseline = capture_workspace_task_baseline(connection, workspace_id)
    persist_task_baseline(connection, task_id, baseline)
    connection.execute(
        """
        INSERT INTO task_checkpoints(
            id, task_id, task_revision, state, wait_reason, summary, next_step,
            created_at, baseline_head, current_head, current_branch, current_dirty_path_count
        ) VALUES (?, ?, 2, 'working', NULL, ?, 'Continue verification', 'checkpoint-time',
                  ?, ?, ?, ?)
        """,
        (
            checkpoint_id,
            task_id,
            summary,
            baseline.head,
            baseline.head,
            baseline.branch,
            len(baseline.dirty_paths),
        ),
    )
    connection.execute(
        """
        INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at)
        VALUES (?, 1, 'created', NULL, NULL, 'created')
        """,
        (task_id,),
    )
    connection.execute(
        """
        INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at)
        VALUES (?, 2, 'checkpoint', ?, NULL, 'checkpoint-time')
        """,
        (task_id, checkpoint_id),
    )
    connection.execute(
        """
        INSERT INTO task_checkpoint_verification(
            checkpoint_id, position, name, status, evidence, source
        ) VALUES (?, 0, 'focused tests', 'passed', 'pytest target: passed', 'agent_reported')
        """,
        (checkpoint_id,),
    )
    cursor = connection.execute(
        """
        INSERT INTO task_events(task_id, task_revision, event_type, checkpoint_id, operator_feedback, created_at)
        VALUES (?, 3, 'operator_feedback', NULL, ?, 'feedback-time')
        """,
        (task_id, feedback),
    )
    assert cursor.lastrowid is not None
    return checkpoint_id, cursor.lastrowid


def test_project_context_expands_only_selected_refs_and_fails_closed_cross_project(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project_id, workspace_id = _workspace(connection, tmp_path / "repo")
        other_project_id, other_workspace_id = _workspace(connection, tmp_path / "other")
        _knowledge(
            connection,
            knowledge_id="selected-card",
            project_id=project_id,
            title="Selected invariant",
            body="Selected semantic body",
            freshness="needs_revalidation",
        )
        _knowledge(
            connection,
            knowledge_id="unrelated-card",
            project_id=project_id,
            title="Unrelated invariant",
            body="Must not be disclosed unless selected",
        )
        _knowledge(
            connection,
            knowledge_id="other-card",
            project_id=other_project_id,
            title="Other Project",
            body="Cross Project secret",
        )
        checkpoint_id, feedback_event_id = _task_history(
            connection,
            task_id="selected-task",
            workspace_id=workspace_id,
            title="Selected task",
            summary="Selected checkpoint semantic detail",
            feedback="Selected operator feedback",
        )
        _task_history(
            connection,
            task_id="other-task",
            workspace_id=other_workspace_id,
            title="Other task",
            summary="Other secret summary",
            feedback="Other secret feedback",
        )

        items = read_project_context(
            connection,
            workspace_id,
            (
                "knowledge:selected-card",
                f"task:selected-task#checkpoint:{checkpoint_id}",
                f"task:selected-task#event:{feedback_event_id}",
                "doc:docs/refresh-rotation.md",
            ),
        )
        serialized = str(items)
        assert "Selected semantic body" in serialized
        assert "historical_clue': True" in serialized
        assert "Selected checkpoint semantic detail" in serialized
        assert "pytest target: passed" in serialized
        assert "Selected operator feedback" in serialized
        assert "Unrelated invariant" not in serialized
        assert "Cross Project secret" not in serialized
        assert "Other secret" not in serialized

        with pytest.raises(ProjectRetrievalRefError, match="another Project"):
            read_project_context(connection, workspace_id, ("knowledge:other-card",))
        with pytest.raises(ProjectRetrievalRefError, match=r"unavailable|another Project"):
            read_project_context(connection, workspace_id, ("task:other-task",))
        with pytest.raises(ProjectRetrievalRefError, match="kind does not match"):
            read_project_context(connection, workspace_id, ("code:docs/refresh-rotation.md",))
    finally:
        connection.close()


def test_project_context_compacts_maximum_semantic_payloads(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project_id, workspace_id = _workspace(connection, tmp_path / "repo")
        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type,
                created_at, updated_at, freshness
            ) VALUES (?, ?, 'invariant', ?, ?, 'operator', 'created', 'updated', 'fresh')
            """,
            ("large-card", project_id, "T" * 256, "B" * 8192),
        )
        for index in range(8):
            relative_path = f"deep/{index}/" + "/".join(["p" * 180] * 10)
            anchor_path = tmp_path / "repo" / relative_path
            anchor_path.parent.mkdir(parents=True, exist_ok=True)
            anchor_path.write_bytes(b"anchored content\n")
            connection.execute(
                """
                INSERT INTO knowledge_anchors(
                    knowledge_id, workspace_id, relative_path, symbol,
                    fingerprint_kind, content_sha256
                ) VALUES (?, ?, ?, ?, 'file', ?)
                """,
                (
                    "large-card",
                    workspace_id,
                    relative_path,
                    f"symbol-{index}",
                    hashlib.sha256(anchor_path.read_bytes()).hexdigest(),
                ),
            )

        checkpoint_id, _ = _task_history(
            connection,
            task_id="large-task",
            workspace_id=workspace_id,
            title="Large context task",
            summary="initial",
            feedback="feedback",
        )
        connection.execute(
            "UPDATE task_checkpoints SET summary = ?, next_step = ? WHERE id = ?",
            ("S" * 4096, "N" * 2048, checkpoint_id),
        )
        for index in range(12):
            connection.execute(
                """
                INSERT INTO task_checkpoint_changed_paths(checkpoint_id, relative_path)
                VALUES (?, ?)
                """,
                (checkpoint_id, f"src/{index}/" + ("x" * 500)),
            )
        connection.execute(
            "DELETE FROM task_checkpoint_verification WHERE checkpoint_id = ?",
            (checkpoint_id,),
        )
        for index in range(12):
            connection.execute(
                """
                INSERT INTO task_checkpoint_verification(
                    checkpoint_id, position, name, status, evidence, source
                ) VALUES (?, ?, ?, 'passed', ?, 'agent_reported')
                """,
                (checkpoint_id, index, f"verification-{index}", "E" * 2048),
            )

        knowledge_item = read_project_context(connection, workspace_id, ("knowledge:large-card",))[
            0
        ]
        task_item = read_project_context(
            connection, workspace_id, (f"task:large-task#checkpoint:{checkpoint_id}",)
        )[0]

        assert knowledge_item.data["body_truncated"] is True
        assert knowledge_item.data["anchor_count"] == 8
        assert knowledge_item.data["anchors_truncated"] is True
        assert len(json.dumps(knowledge_item.data, ensure_ascii=False).encode("utf-8")) < 4096

        selected = task_item.data["selected_checkpoint"]
        assert isinstance(selected, dict)
        assert selected["summary_truncated"] is True
        assert selected["next_step_truncated"] is True
        assert selected["changed_path_count"] == 12
        assert selected["changed_paths_truncated"] is True
        assert selected["verification_count"] == 12
        assert selected["verification_truncated"] is True
        assert len(selected["verification"]) == 4
        assert all(item["evidence_truncated"] is True for item in selected["verification"])
        assert len(json.dumps(task_item.data, ensure_ascii=False).encode("utf-8")) < 8192
    finally:
        connection.close()
