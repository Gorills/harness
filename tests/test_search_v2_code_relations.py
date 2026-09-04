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


def test_precise_code_structure_extracts_supported_references_and_python_lexical_resolution() -> (
    None
):
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
    python_call = next(item for item in python.relations if item.kind == "call")
    assert python_call.resolved_target == "pkg.helper"
    assert python_call.resolution_kind == "python_from_import_binding"
    assert python_call.resolution_module == "pkg"
    # Non-Python scan-time relations remain unresolved syntax in this slice.
    typescript_call = next(item for item in typescript.relations if item.kind == "call")
    assert typescript_call.target == "h"
    assert typescript_call.resolved_target is None


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


def test_scan_persists_proven_python_direct_resolved_edge_and_searches_it(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/caller.py": (
                "from service import target_call as tc\n"
                "def invoke():\n"
                "    return tc()  # private-resolved-edge-marker\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        assert connection.execute(
            """
            SELECT status, relation_status, resolution_status
            FROM indexed_code_unit_files
            WHERE workspace_id = ? AND relative_path = 'src/caller.py'
            """,
            (workspace_id,),
        ).fetchone() == ("ok", "ok", "ok")
        relation = connection.execute(
            """
            SELECT id, target, resolved_target, resolution_kind, resolution_module
            FROM indexed_code_relations
            WHERE workspace_id = ?
              AND relative_path = 'src/caller.py'
              AND relation_kind = 'call'
            """,
            (workspace_id,),
        ).fetchone()
        assert relation is not None
        relation_id, target, resolved_target, resolution_kind, resolution_module = relation
        assert (target, resolved_target, resolution_kind, resolution_module) == (
            "tc",
            "service.target_call",
            "python_from_import_binding",
            "service",
        )
        assert connection.execute(
            """
            SELECT targets.relative_path, targets.qualified_name, resolved.validation_kind
            FROM indexed_resolved_code_relations AS resolved
            JOIN indexed_code_units AS targets ON targets.id = resolved.target_unit_id
            WHERE resolved.relation_id = ?
            """,
            (relation_id,),
        ).fetchone() == (
            "src/service.py",
            "target_call",
            "python_workspace_direct_export",
        )
        assert connection.execute(
            """
            SELECT status, edge_count FROM indexed_resolved_relation_workspaces
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone() == ("ok", 1)

        results = search_project(
            connection,
            workspace_id,
            "who calls service target call",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )
        assert results[0].ref == "code:src/caller.py"
        assert results[0].match_reason == "code resolved call relation"
        assert results[0].short_summary == "resolved call service.target_call in invoke"
        dump = "\n".join(connection.iterdump())
        assert "private-resolved-edge-marker" not in dump
    finally:
        connection.close()


def test_scan_persists_bounded_python_reexport_chain_edge(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/impl.py": "def target_call():\n    return 1\n",
            "src/service.py": "from impl import target_call\n",
            "src/caller.py": (
                "from service import target_call as tc\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        assert connection.execute(
            """
            SELECT exported_name, imported_name, module
            FROM indexed_python_reexports
            WHERE workspace_id = ? AND relative_path = 'src/service.py'
            """,
            (workspace_id,),
        ).fetchall() == [("target_call", "target_call", "impl")]
        assert connection.execute(
            """
            SELECT targets.relative_path, resolved.validation_kind
            FROM indexed_resolved_code_relations AS resolved
            JOIN indexed_code_relations AS source ON source.id = resolved.relation_id
            JOIN indexed_code_units AS targets ON targets.id = resolved.target_unit_id
            WHERE resolved.workspace_id = ? AND source.relative_path = 'src/caller.py'
            """,
            (workspace_id,),
        ).fetchone() == ("src/impl.py", "python_workspace_reexport_chain")
    finally:
        connection.close()


def test_incremental_target_change_rebuilds_resolved_edges_for_unchanged_caller(
    tmp_path: Path,
) -> None:
    root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/caller.py": (
                "from service import target_call as tc\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_resolved_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (1,)

        (root / "src/service.py").write_text(
            "def renamed():\n    return 1\n",
            encoding="utf-8",
        )
        scan_workspace_paths(connection, workspace_id, ("src/service.py",))

        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_resolved_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT status, edge_count FROM indexed_resolved_relation_workspaces
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone() == ("ok", 0)
        assert (
            search_project(
                connection,
                workspace_id,
                "who calls service target call",
                scope=ProjectSearchScope.CODE,
                limit=5,
            )
            == ()
        )
    finally:
        connection.close()


def test_persistent_resolution_fails_closed_on_ambiguous_module_and_shadowing(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "lib/service.py": "def target_call():\n    return 2\n",
            "src/ambiguous.py": (
                "from service import target_call as tc\ndef invoke():\n    return tc()\n"
            ),
            "src/shadowed.py": (
                "from service import target_call as tc\ndef invoke(tc):\n    return tc()\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        rows = connection.execute(
            """
            SELECT relative_path, resolved_target
            FROM indexed_code_relations
            WHERE workspace_id = ? AND relation_kind = 'call'
            ORDER BY relative_path
            """,
            (workspace_id,),
        ).fetchall()
        assert rows == [
            ("src/ambiguous.py", "service.target_call"),
            ("src/shadowed.py", None),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_resolved_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_persistent_resolution_counts_parse_failed_module_candidate_as_ambiguous(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "lib/service.py": "def broken(:\n    pass\n",
            "src/caller.py": (
                "from service import target_call as tc\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)
        assert connection.execute(
            """
            SELECT status FROM indexed_code_unit_files
            WHERE workspace_id = ? AND relative_path = 'lib/service.py'
            """,
            (workspace_id,),
        ).fetchone() == ("parse_error",)
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_resolved_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_resolved_edge_workspace_limit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def first():\n    pass\ndef second():\n    pass\n",
            "src/caller.py": (
                "from service import first, second\ndef invoke():\n    first()\n    second()\n"
            ),
        },
    )
    monkeypatch.setattr(index_module, "MAX_INDEXED_RESOLVED_CODE_RELATIONS_PER_WORKSPACE", 1)
    try:
        scan_workspace(connection, workspace_id)
        assert connection.execute(
            """
            SELECT status, edge_count FROM indexed_resolved_relation_workspaces
            WHERE workspace_id = ?
            """,
            (workspace_id,),
        ).fetchone() == ("edge_limit", 0)
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_resolved_code_relations WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_resolved_code_relation_search"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_schema_20_migrates_existing_19_database_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    current = storage.SCHEMA_VERSION
    assert current == 20
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 19)
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('preserved-project')")
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", current)
    status = initialize_database(database)

    assert status.schema_version == 20
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("preserved-project",)]
        resolution_status = connection.execute(
            """
            SELECT dflt_value FROM pragma_table_info('indexed_code_unit_files')
            WHERE name = 'resolution_status'
            """
        ).fetchone()
        assert resolution_status == ("'unindexed'",)
        relation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(indexed_code_relations)")
        }
        assert {"resolved_target", "resolution_kind", "resolution_module"} <= relation_columns
        for table in (
            "indexed_python_reexports",
            "indexed_resolved_relation_workspaces",
            "indexed_resolved_code_relations",
            "indexed_resolved_code_relation_search",
        ):
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() == (table,)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (20,)
    finally:
        connection.close()
