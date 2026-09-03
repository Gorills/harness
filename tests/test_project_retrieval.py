from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import (
    ProjectRetrievalRefError,
    ProjectSearchKind,
    ProjectSearchScope,
    read_project_context,
    search_project,
    search_tasks,
)
from harness.storage import connect_database, initialize_database


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
    connection.execute(
        """
        INSERT INTO task_checkpoints(
            id, task_id, task_revision, state, wait_reason, summary, next_step,
            created_at, baseline_head, current_head, current_branch, current_dirty_path_count
        ) VALUES (?, ?, 2, 'working', NULL, ?, 'Continue verification', 'checkpoint-time',
                  NULL, NULL, 'main', 0)
        """,
        (checkpoint_id, task_id, summary),
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


def test_project_search_retrieves_scoped_knowledge_tasks_code_and_docs(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project_id, workspace_id = _workspace(connection, tmp_path / "repo")
        other_project_id, other_workspace_id = _workspace(connection, tmp_path / "other")
        _knowledge(
            connection,
            knowledge_id="fresh-card",
            project_id=project_id,
            title="Refresh token rotation invariant",
            body="Rotation invalidates the previous refresh credential atomically.",
        )
        _knowledge(
            connection,
            knowledge_id="stale-card",
            project_id=project_id,
            title="Historical refresh token caveat",
            body="Old rotation behavior retained two credentials.",
            freshness="needs_revalidation",
        )
        _knowledge(
            connection,
            knowledge_id="other-card",
            project_id=other_project_id,
            title="Other Project refresh secret",
            body="Must never cross Project retrieval boundaries.",
        )
        checkpoint_id, feedback_event_id = _task_history(
            connection,
            task_id="rotation-task",
            workspace_id=workspace_id,
            title="Rotate session credentials",
            summary="Refresh token rotation updates durable session state",
            feedback="Preserve the legacy session marker during refresh migration",
        )
        _task_history(
            connection,
            task_id="other-task",
            workspace_id=other_workspace_id,
            title="Other Project refresh work",
            summary="Secret unrelated refresh summary",
            feedback="Secret unrelated feedback",
        )

        knowledge = search_project(
            connection,
            workspace_id,
            "refresh token rotation",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=5,
        )
        assert [hit.ref for hit in knowledge] == ["knowledge:fresh-card", "knowledge:stale-card"]
        assert knowledge[0].freshness == "fresh"
        assert knowledge[1].freshness == "needs_revalidation"
        assert "other-card" not in str(knowledge)

        tasks = search_project(
            connection,
            workspace_id,
            "refresh migration",
            scope=ProjectSearchScope.TASKS,
            limit=5,
        )
        assert len(tasks) == 1
        assert tasks[0].kind is ProjectSearchKind.TASK
        assert tasks[0].ref in {
            f"task:rotation-task#checkpoint:{checkpoint_id}",
            f"task:rotation-task#event:{feedback_event_id}",
        }
        assert "other-task" not in str(tasks)

        across_projects = search_tasks(connection, "refresh", limit=5)
        titles = {hit.title for hit in across_projects}
        assert "Rotate session credentials" in titles
        assert "Other Project refresh work" in titles

        code = search_project(
            connection, workspace_id, "refresh token", scope=ProjectSearchScope.CODE, limit=5
        )
        docs = search_project(
            connection, workspace_id, "refresh rotation", scope=ProjectSearchScope.DOCS, limit=5
        )
        assert code[0].ref == "code:src/refresh_token.py"
        assert code[0].kind is ProjectSearchKind.CODE
        assert code[0].match_reason == "exact filename stem"
        assert docs[0].ref == "doc:docs/refresh-rotation.md"
        assert docs[0].kind is ProjectSearchKind.DOC

        all_hits = search_project(
            connection, workspace_id, "refresh", scope=ProjectSearchScope.ALL, limit=5
        )
        assert len(all_hits) <= 5
        assert {hit.kind for hit in all_hits} >= {
            ProjectSearchKind.CODE,
            ProjectSearchKind.DOC,
            ProjectSearchKind.KNOWLEDGE,
        }
    finally:
        connection.close()


def test_project_search_uses_content_for_natural_queries_and_compound_identifiers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        _project_id, workspace_id = _workspace(connection, root)
        (root / "tests").mkdir()
        (root / "tests" / "test_refresh_token.py").write_text(
            "def test_rotateRefreshToken():\n    assert previous_credential_is_invalid()\n",
            encoding="utf-8",
        )
        scan_workspace(connection, workspace_id)

        natural = search_project(
            connection,
            workspace_id,
            "where previous credential invalidation happens",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )
        compound = search_project(
            connection,
            workspace_id,
            "rotate refresh token",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )
        test_query = search_project(
            connection,
            workspace_id,
            "test rotate refresh token",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )

        assert natural[0].ref == "code:src/refresh_token.py"
        assert natural[0].match_reason == "lexical content (all terms)"
        assert compound[0].ref == "code:src/refresh_token.py"
        assert compound[0].match_reason == "code unit definition phrase"
        assert compound[0].short_summary == "function rotateRefreshToken"
        assert test_query[0].ref == "code:tests/test_refresh_token.py"
        assert natural[0].evidence is not None
        assert "credential" in natural[0].evidence.snippet
        assert natural[0].short_summary is None
        assert "bm25" not in repr(natural).casefold()
    finally:
        connection.close()


def test_project_search_rejects_partial_identifier_matches_for_garbage_query(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        _project_id, workspace_id = _workspace(connection, tmp_path / "repo")

        results = search_project(
            connection,
            workspace_id,
            "nonexistent-xyzzy-token-12345",
            scope=ProjectSearchScope.ALL,
            limit=5,
        )

        assert results == ()
    finally:
        connection.close()


def test_project_search_excludes_generated_test_output_from_code_results(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        _project_id, workspace_id = _workspace(connection, root)
        (root / "src" / "nutrition.py").write_text(
            "# FastAPI endpoints for nutrition requests.\n",
            encoding="utf-8",
        )
        (root / ".pytest_nutrition_all.out").write_text(
            "FastAPI endpoints nutrition nutrition nutrition\n",
            encoding="utf-8",
        )
        scan_workspace(connection, workspace_id)

        results = search_project(
            connection,
            workspace_id,
            "FastAPI endpoints nutrition",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )

        assert results[0].ref == "code:src/nutrition.py"
        assert all(hit.path != ".pytest_nutrition_all.out" for hit in results)
    finally:
        connection.close()


def test_project_search_prioritizes_canonical_exact_stem_doc_over_archives(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        _project_id, workspace_id = _workspace(connection, root)
        (root / "docs" / "ARCHITECTURE.md").write_text(
            "Canonical system design.\n",
            encoding="utf-8",
        )
        archive = root / "epic" / "archive" / "04_architecture_stabilization"
        archive.mkdir(parents=True)
        for name in (
            "ARCHITECTURE.md",
            "architecture_notes_1.md",
            "architecture_notes_2.md",
            "architecture_notes_3.md",
            "architecture_notes_4.md",
        ):
            (archive / name).write_text(
                "Architecture architecture architecture stabilization notes.\n",
                encoding="utf-8",
            )
        scan_workspace(connection, workspace_id)

        results = search_project(
            connection,
            workspace_id,
            "architecture",
            scope=ProjectSearchScope.DOCS,
            limit=5,
        )

        assert results[0].ref == "doc:docs/ARCHITECTURE.md"
    finally:
        connection.close()


def test_all_scope_prioritizes_direct_fresh_knowledge_over_general_lexical_hits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project_id, workspace_id = _workspace(connection, tmp_path / "repo")
        _knowledge(
            connection,
            knowledge_id="direct-card",
            project_id=project_id,
            title="Atomic credential invalidation",
            body="The previous refresh credential is invalidated in the replacement transaction.",
        )
        _knowledge(
            connection,
            knowledge_id="incidental-card",
            project_id=project_id,
            title="Credential operations",
            body="General operational notes for credentials.",
        )

        knowledge_results = search_project(
            connection,
            workspace_id,
            "where atomic credential invalidation happens",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=5,
        )

        results = search_project(
            connection,
            workspace_id,
            "atomic credential invalidation",
            scope=ProjectSearchScope.ALL,
            limit=5,
        )

        assert [hit.ref for hit in knowledge_results] == [
            "knowledge:direct-card",
            "knowledge:incidental-card",
        ]
        assert results[0].ref == "knowledge:direct-card"
        assert results[0].freshness == "fresh"
        assert any(hit.ref == "code:src/refresh_token.py" for hit in results)
    finally:
        connection.close()


def test_project_search_matches_common_russian_inflections_in_docs(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        _project_id, workspace_id = _workspace(connection, root)
        (root / "docs" / "search-quality.md").write_text(
            "Поиск по проектам учитывает релевантность результатов.\n",
            encoding="utf-8",
        )
        scan_workspace(connection, workspace_id)

        results = search_project(
            connection,
            workspace_id,
            "релевантности поиска проекта",
            scope=ProjectSearchScope.DOCS,
            limit=5,
        )

        assert results[0].ref == "doc:docs/search-quality.md"
        assert results[0].match_reason == "lexical content (all terms)"
    finally:
        connection.close()


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
        with pytest.raises(ProjectRetrievalRefError, match="another Project"):
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
                    f"deep/{index}/" + ("p" * 1800),
                    f"symbol-{index}",
                    f"{index:x}" * 64,
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
