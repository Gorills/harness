from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.index as index_module
import harness.storage as storage
from harness.index import (
    MAX_INDEXED_CODE_RELATIONS_PER_FILE,
    scan_workspace,
    scan_workspace_paths,
)
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectSearchScope, search_project
from harness.storage import connect_database, initialize_database
from harness.symbol_navigation import (
    SyntaxRelation,
    SyntaxRelationAnalysis,
    SyntaxRelationEvidence,
    analyze_precise_code_structure,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _registered(tmp_path: Path, files: dict[str, str]) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
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
        "initial",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    return root, connection, workspace.workspace_id


def test_precise_code_structure_extracts_supported_references_without_resolving_targets() -> None:
    python = analyze_precise_code_structure(
        "src/client.py",
        """
from pkg import helper
class Client(Base):
    def fetch(self):
        return helper()
""".lstrip(),
    )
    typescript = analyze_precise_code_structure(
        "src/client.ts",
        """
import { helper as h } from "./util";
class Client extends Base { fetch() { return h(); } }
""".lstrip(),
    )

    assert python.status == "ok"
    assert typescript.status == "ok"
    assert {(item.kind, item.target) for item in python.relations} >= {
        ("definition", "Client.fetch"),
        ("call", "helper"),
        ("import", "pkg.helper"),
        ("inheritance", "Base"),
    }
    assert {(item.kind, item.target) for item in typescript.relations} >= {
        ("definition", "Client.fetch"),
        ("call", "h"),
        ("import", "./util.helper"),
        ("inheritance", "Base"),
    }
    # These are syntax targets only: no provider claims that `h` resolves to `./util.helper`.
    assert any(item.kind == "call" and item.target == "h" for item in typescript.relations)


def test_scan_persists_rebuildable_code_relations_without_source_bodies(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def rotateRefreshToken():\n    return 1\n",
            "src/caller.py": (
                "from service import rotateRefreshToken\n"
                "def issueSession():\n"
                "    return rotateRefreshToken()  # private-relation-source-marker\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        assert connection.execute(
            """
            SELECT relation_status FROM indexed_code_unit_files
            WHERE workspace_id = ? AND relative_path = 'src/caller.py'
            """,
            (workspace_id,),
        ).fetchone() == ("ok",)
        relations = connection.execute(
            """
            SELECT relation_kind, scope, target, in_test
            FROM indexed_code_relations
            WHERE workspace_id = ? AND relative_path = 'src/caller.py'
            ORDER BY position
            """,
            (workspace_id,),
        ).fetchall()
        assert ("call", "issueSession", "rotateRefreshToken", 0) in relations
        assert ("import", "", "service.rotateRefreshToken", 0) in relations
        assert connection.execute(
            """
            SELECT relations.relative_path
            FROM indexed_code_relation_search
            JOIN indexed_code_relations AS relations
              ON relations.id = indexed_code_relation_search.rowid
            WHERE indexed_code_relation_search MATCH 'rotate AND refresh AND token'
            ORDER BY relations.relative_path
            """
        ).fetchall() == [("src/caller.py",), ("src/caller.py",)]
        assert "private-relation-source-marker" not in "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_relation_intent_query_prefers_precise_caller_over_lexical_question(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def rotateRefreshToken():\n    return 1\n",
            "src/caller.py": ("def issueSession():\n    return rotateRefreshToken()\n"),
            "src/commentary.py": (
                "# who calls rotate refresh token who calls rotate refresh token\nVALUE = 1\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        results = search_project(
            connection,
            workspace_id,
            "who calls rotate refresh token",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )

        assert results[0].ref == "code:src/caller.py"
        assert results[0].match_reason == "code call relation"
        assert results[0].short_summary == "call rotateRefreshToken in issueSession"
        assert any(hit.ref == "code:src/commentary.py" for hit in results)
    finally:
        connection.close()


def test_incremental_scan_replaces_persisted_relations_and_clears_parse_failure(
    tmp_path: Path,
) -> None:
    root, connection, workspace_id = _registered(
        tmp_path,
        {"src/caller.py": "def caller():\n    return oldTarget()\n"},
    )
    caller = root / "src" / "caller.py"
    try:
        scan_workspace(connection, workspace_id)
        assert connection.execute(
            "SELECT target FROM indexed_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall() == [("oldTarget",)]

        caller.write_text("def caller():\n    return newTarget()\n", encoding="utf-8")
        scan_workspace_paths(connection, workspace_id, ("src/caller.py",))
        assert connection.execute(
            "SELECT target FROM indexed_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall() == [("newTarget",)]
        assert (
            connection.execute(
                """
                SELECT rowid FROM indexed_code_relation_search
                WHERE indexed_code_relation_search MATCH 'oldTarget'
                """
            ).fetchall()
            == []
        )

        caller.write_text("def broken(:\n    return newTarget()\n", encoding="utf-8")
        scan_workspace_paths(connection, workspace_id, ("src/caller.py",))
        assert connection.execute(
            """
            SELECT status, relation_status FROM indexed_code_unit_files
            WHERE workspace_id = ? AND relative_path = 'src/caller.py'
            """,
            (workspace_id,),
        ).fetchone() == ("parse_error", "unindexed")
        assert (
            connection.execute(
                "SELECT target FROM indexed_code_relations WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            == []
        )
    finally:
        connection.close()


def test_relation_limit_fails_closed_without_discarding_definitions_and_is_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/service.py": "def target():\n    return helper()\n"},
    )
    evidence = SyntaxRelationEvidence(1, 1, "target", False)
    definition = SyntaxRelation(
        kind="definition",
        path="src/service.py",
        line=1,
        column=5,
        scope=None,
        target="target",
        symbol_kind="function",
        in_test=False,
        evidence=evidence,
    )
    call = SyntaxRelation(
        kind="call",
        path="src/service.py",
        line=2,
        column=12,
        scope="target",
        target="helper",
        symbol_kind=None,
        in_test=False,
        evidence=evidence,
    )
    oversized = SyntaxRelationAnalysis(
        "python",
        "ok",
        (definition,) + (call,) * (MAX_INDEXED_CODE_RELATIONS_PER_FILE + 1),
    )
    calls = 0

    def oversized_analysis(_relative_path: str, _text: str) -> SyntaxRelationAnalysis:
        nonlocal calls
        calls += 1
        return oversized

    monkeypatch.setattr(index_module, "analyze_precise_code_structure", oversized_analysis)
    try:
        scan_workspace(connection, workspace_id)
        assert calls == 1
        assert connection.execute(
            "SELECT status, relation_status FROM indexed_code_unit_files WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == ("ok", "relation_limit")
        assert connection.execute(
            "SELECT qualified_name FROM indexed_code_units WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall() == [("target",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (0,)

        scan_workspace(connection, workspace_id)
        assert calls == 1
    finally:
        connection.close()


def test_schema_19_migrates_existing_18_database_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    current = storage.SCHEMA_VERSION
    assert current == 19
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 18)
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('preserved-project')")
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", current)
    status = initialize_database(database)

    assert status.schema_version == 19
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("preserved-project",)]
        relation_status = connection.execute(
            """
            SELECT dflt_value FROM pragma_table_info('indexed_code_unit_files')
            WHERE name = 'relation_status'
            """
        ).fetchone()
        assert relation_status == ("'unindexed'",)
        for table in ("indexed_code_relations", "indexed_code_relation_search"):
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() == (table,)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (19,)
    finally:
        connection.close()
