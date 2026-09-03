from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.index as index_module
import harness.storage as storage
from harness.index import (
    MAX_INDEXED_CODE_UNITS_PER_FILE,
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
    analyze_precise_code_units,
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


def test_precise_code_unit_analysis_extracts_definitions_without_references() -> None:
    python = analyze_precise_code_units(
        "src/service.py",
        """
class RefreshTokenService:
    def rotateRefreshToken(self):
        return helper()

def helper():
    return 1
""".lstrip(),
    )
    typescript = analyze_precise_code_units(
        "src/service.ts",
        """
class RefreshTokenService {
  rotateRefreshToken() { return helper(); }
}
function helper() { return 1; }
""".lstrip(),
    )

    assert python.status == "ok"
    assert typescript.status == "ok"
    assert all(relation.kind == "definition" for relation in python.relations)
    assert all(relation.kind == "definition" for relation in typescript.relations)
    assert {(item.target, item.symbol_kind) for item in python.relations} == {
        ("RefreshTokenService", "class"),
        ("RefreshTokenService.rotateRefreshToken", "method"),
        ("helper", "function"),
    }
    assert {(item.target, item.symbol_kind) for item in typescript.relations} == {
        ("RefreshTokenService", "class"),
        ("RefreshTokenService.rotateRefreshToken", "method"),
        ("helper", "function"),
    }


def test_scan_persists_rebuildable_code_units_without_source_bodies(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": (
                "class RefreshTokenService:\n"
                "    def rotateRefreshToken(self):\n"
                "        return 'private-source-marker'\n"
            ),
            "src/service.ts": "export function issueSessionToken() { return 1; }\n",
            "docs/guide.md": "RefreshTokenService documentation\n",
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        manifests = connection.execute(
            """
            SELECT relative_path, language, status
            FROM indexed_code_unit_files
            WHERE workspace_id = ?
            ORDER BY relative_path
            """,
            (workspace_id,),
        ).fetchall()
        units = connection.execute(
            """
            SELECT relative_path, name, qualified_name, symbol_kind
            FROM indexed_code_units
            WHERE workspace_id = ?
            ORDER BY relative_path, position
            """,
            (workspace_id,),
        ).fetchall()

        assert manifests == [
            ("src/service.py", "python", "ok"),
            ("src/service.ts", "typescript", "ok"),
        ]
        assert (
            "src/service.py",
            "rotateRefreshToken",
            "RefreshTokenService.rotateRefreshToken",
            "method",
        ) in units
        assert (
            "src/service.ts",
            "issueSessionToken",
            "issueSessionToken",
            "function",
        ) in units
        assert connection.execute(
            """
            SELECT units.relative_path
            FROM indexed_code_unit_search
            JOIN indexed_code_units AS units ON units.id = indexed_code_unit_search.rowid
            WHERE indexed_code_unit_search MATCH 'rotate AND refresh AND token'
            ORDER BY units.relative_path
            """
        ).fetchall() == [("src/service.py",)]
        assert "private-source-marker" not in "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_natural_code_query_prefers_definition_unit_over_lexical_mention(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/engine.py": "def rotateRefreshToken():\n    return 1\n",
            "src/commentary.py": (
                "# rotate refresh token rotate refresh token rotate refresh token\nVALUE = 1\n"
            ),
        },
    )
    try:
        scan_workspace(connection, workspace_id)

        results = search_project(
            connection,
            workspace_id,
            "rotate refresh token",
            scope=ProjectSearchScope.CODE,
            limit=5,
        )

        assert results[0].ref == "code:src/engine.py"
        assert results[0].match_reason == "code unit definition phrase"
        assert results[0].short_summary == "function rotateRefreshToken"
        assert any(hit.ref == "code:src/commentary.py" for hit in results)
    finally:
        connection.close()


def test_incremental_scan_replaces_code_units_and_caches_parse_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(
        tmp_path,
        {"src/service.py": "def oldTarget():\n    return 1\n"},
    )
    service = root / "src" / "service.py"
    try:
        scan_workspace(connection, workspace_id)
        assert connection.execute(
            "SELECT qualified_name FROM indexed_code_units WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall() == [("oldTarget",)]

        service.write_text("def newTarget():\n    return 2\n", encoding="utf-8")
        scan_workspace_paths(connection, workspace_id, ("src/service.py",))
        assert connection.execute(
            "SELECT qualified_name FROM indexed_code_units WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchall() == [("newTarget",)]
        assert (
            connection.execute(
                "SELECT rowid FROM indexed_code_unit_search WHERE indexed_code_unit_search MATCH 'oldTarget'"
            ).fetchall()
            == []
        )
        assert connection.execute(
            "SELECT rowid FROM indexed_code_unit_search WHERE indexed_code_unit_search MATCH 'newTarget'"
        ).fetchall()

        service.write_text("def broken(:\n    return 3\n", encoding="utf-8")
        scan_workspace_paths(connection, workspace_id, ("src/service.py",))
        assert connection.execute(
            """
            SELECT status FROM indexed_code_unit_files
            WHERE workspace_id = ? AND relative_path = 'src/service.py'
            """,
            (workspace_id,),
        ).fetchone() == ("parse_error",)
        assert (
            connection.execute(
                "SELECT qualified_name FROM indexed_code_units WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            == []
        )

        def unexpected_parse(_relative_path: str, _text: str) -> SyntaxRelationAnalysis:
            raise AssertionError(
                "unchanged parse-error source must use the persisted negative manifest"
            )

        monkeypatch.setattr(index_module, "analyze_precise_code_structure", unexpected_parse)
        scan_workspace_paths(connection, workspace_id, ("src/service.py",))

        service.unlink()
        scan_workspace_paths(connection, workspace_id, ("src/service.py",))
        assert (
            connection.execute(
                "SELECT relative_path FROM indexed_code_unit_files WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            == []
        )
        assert (
            connection.execute(
                "SELECT qualified_name FROM indexed_code_units WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchall()
            == []
        )
    finally:
        connection.close()


def test_code_unit_limit_fails_closed_and_is_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/service.py": "def target():\n    return 1\n"},
    )
    evidence = SyntaxRelationEvidence(1, 1, "def target():", False)
    relation = SyntaxRelation(
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
    oversized = SyntaxRelationAnalysis(
        "python",
        "ok",
        (relation,) * (MAX_INDEXED_CODE_UNITS_PER_FILE + 1),
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
            "SELECT status FROM indexed_code_unit_files WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == ("unit_limit",)
        assert connection.execute(
            "SELECT COUNT(*) FROM indexed_code_units WHERE workspace_id = ?",
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
        for table in (
            "indexed_code_unit_files",
            "indexed_code_units",
            "indexed_code_unit_search",
        ):
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() == (table,)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone() == (19,)
    finally:
        connection.close()
