from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from threading import Lock

from harness.daemon import read_project_search
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import (
    PROJECT_SEARCH_MAX_BYTES,
    ProjectSearchScope,
    exact_coverage_response_reserve,
    project_exact_search_coverage_payload,
    project_search_hit_payload,
    project_symbol_navigation_payload,
    search_exact_source_inspection,
    search_project,
    symbol_navigation_response_reserve,
)
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint


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
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id


def test_symbol_navigation_classifies_python_definition_calls_imports_and_tests(
    tmp_path: Path,
) -> None:
    root, connection, _workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": (
                "def target_call(value):\n"
                "    return value + 1\n\n"
                "class Worker:\n"
                "    def run(self):\n"
                "        return target_call(1)\n"
            ),
            "src/api.py": (
                "from service import target_call\n\ndef handle():\n    return target_call(2)\n"
            ),
            "tests/test_service.py": (
                "from service import target_call\n\n"
                "def test_behavior():\n"
                "    assert target_call(1) == 2\n"
            ),
        },
    )
    try:
        result = read_project_search(
            connection,
            (WorkspaceHint(root, "explicit-root"),),
            "target_call",
            5,
            ProjectSearchScope.CODE,
            Lock(),
        )

        assert result.exact_coverage is not None
        assert result.exact_coverage.complete is True
        navigation = result.symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == ("python",)
        assert navigation.candidate_precise_files == 3
        assert navigation.parsed_precise_files == 3
        assert navigation.parse_failures == 0
        assert navigation.parse_skipped_files == 0
        assert navigation.matching_unsupported_files == 0
        assert navigation.precise_classification_complete is True
        assert navigation.definition_count == 1
        assert navigation.call_count == 3
        assert navigation.test_call_count == 1
        assert navigation.import_count == 2
        relations = list(navigation.relations)
        assert relations[0].kind == "definition"
        assert relations[0].path == "src/service.py"
        assert relations[0].target == "target_call"
        assert relations[0].symbol_kind == "function"
        assert relations[0].evidence is not None
        assert "def target_call" in relations[0].evidence.snippet
        production_calls = [item for item in relations if item.kind == "call" and not item.in_test]
        test_calls = [item for item in relations if item.kind == "call" and item.in_test]
        assert {item.scope for item in production_calls} == {"Worker.run", "handle"}
        assert [item.scope for item in test_calls] == ["test_behavior"]
        assert all(item.evidence is not None for item in production_calls + test_calls)
    finally:
        connection.close()


def test_symbol_navigation_matches_qualified_python_definition_and_call(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/client.py": ("class Client:\n    def fetch(self):\n        return 1\n"),
            "src/caller.py": ("def invoke():\n    return Client.fetch(None)\n"),
        },
    )
    try:
        inspection = search_exact_source_inspection(
            connection,
            workspace_id,
            "Client.fetch",
            scope=ProjectSearchScope.CODE,
        )
        assert inspection.coverage is not None
        assert inspection.coverage.matched_occurrences == 1
        navigation = inspection.symbol_navigation
        assert navigation is not None
        assert navigation.candidate_precise_files == 2
        assert navigation.parsed_precise_files == 2
        assert navigation.definition_count == 1
        assert navigation.call_count == 1
        definition = next(item for item in navigation.relations if item.kind == "definition")
        call = next(item for item in navigation.relations if item.kind == "call")
        assert definition.target == "Client.fetch"
        assert definition.scope == "Client"
        assert definition.symbol_kind == "method"
        assert call.target == "Client.fetch"
        assert call.scope == "invoke"
    finally:
        connection.close()


def test_symbol_navigation_marks_dirty_python_syntax_failure_without_weak_guessing(
    tmp_path: Path,
) -> None:
    root, connection, _workspace_id = _registered(
        tmp_path,
        {"src/service.py": "def old_name():\n    return 1\n"},
    )
    try:
        (root / "src" / "service.py").write_text(
            "def target_call(:\n    target_call()\n",
            encoding="utf-8",
        )
        result = read_project_search(
            connection,
            (WorkspaceHint(root, "explicit-root"),),
            "target_call",
            5,
            ProjectSearchScope.CODE,
            Lock(),
        )
        assert result.exact_coverage is not None
        assert result.exact_coverage.complete is True
        assert result.exact_coverage.matched_occurrences == 2
        navigation = result.symbol_navigation
        assert navigation is not None
        assert navigation.candidate_precise_files == 1
        assert navigation.parsed_precise_files == 0
        assert navigation.parse_failures == 1
        assert navigation.precise_classification_complete is False
        assert navigation.relations == ()
    finally:
        connection.close()


def test_symbol_navigation_reports_unsupported_matching_code_without_regex_classification(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/app.ts": "function target_call() {}\ntarget_call()\n"},
    )
    try:
        inspection = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        assert inspection.coverage is not None
        assert inspection.coverage.complete is True
        assert inspection.coverage.matched_occurrences == 2
        navigation = inspection.symbol_navigation
        assert navigation is not None
        assert navigation.candidate_precise_files == 0
        assert navigation.parsed_precise_files == 0
        assert navigation.matching_unsupported_files == 1
        assert navigation.relations == ()
    finally:
        connection.close()


def test_quoted_literal_and_docs_search_do_not_claim_symbol_navigation(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "MESSAGE = 'target_call'\n",
            "docs/guide.md": "target_call is documented here\n",
        },
    )
    try:
        quoted = search_exact_source_inspection(
            connection,
            workspace_id,
            "'target_call'",
            scope=ProjectSearchScope.CODE,
        )
        docs = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.DOCS,
        )
        assert quoted.coverage is not None
        assert quoted.symbol_navigation is None
        assert docs.coverage is not None
        assert docs.symbol_navigation is None
    finally:
        connection.close()


def test_symbol_navigation_and_exact_coverage_share_existing_search_budget(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": (
                "def target_call(value):\n"
                "    return value\n\n"
                + "\n\n".join(
                    f"def caller_{index}():\n    return target_call({index})" for index in range(40)
                )
                + "\n"
            )
        },
    )
    try:
        inspection = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        coverage = inspection.coverage
        navigation = inspection.symbol_navigation
        assert coverage is not None
        assert navigation is not None
        assert navigation.call_count == 40
        assert navigation.relations_truncated is True
        hits = search_project(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
            limit=5,
            response_reserve_bytes=(
                exact_coverage_response_reserve(coverage)
                + symbol_navigation_response_reserve(navigation)
            ),
        )
        payload = {
            "query": "target_call",
            "scope": "code",
            "workspace_state": "current",
            "exact_coverage": project_exact_search_coverage_payload(coverage),
            "symbol_navigation": project_symbol_navigation_payload(navigation),
            "results": [project_search_hit_payload(hit) for hit in hits],
        }
        assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= (
            PROJECT_SEARCH_MAX_BYTES
        )
    finally:
        connection.close()


def test_symbol_navigation_classifies_alias_import_and_inheritance(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/model.py": ("class Base:\n    pass\n\nclass Child(Base):\n    pass\n"),
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc(1)\n"
            ),
        },
    )
    try:
        base = search_exact_source_inspection(
            connection, workspace_id, "Base", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        alias = search_exact_source_inspection(
            connection, workspace_id, "tc", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert base is not None
        assert base.definition_count == 1
        assert base.inheritance_count == 1
        assert any(item.kind == "inheritance" and item.target == "Base" for item in base.relations)
        assert alias is not None
        assert alias.import_count == 1
        assert alias.call_count == 1
        assert any(
            item.kind == "import" and item.target == "service.target_call"
            for item in alias.relations
        )
        assert any(item.kind == "call" and item.target == "tc" for item in alias.relations)
    finally:
        connection.close()


def test_symbol_navigation_bounds_ast_parse_size_without_weak_fallback(tmp_path: Path) -> None:
    padding = "# padding\n" * 120_000
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/large.py": padding + "def target_call():\n    return 1\n"},
    )
    try:
        inspection = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        )
        assert inspection.coverage is not None
        assert inspection.coverage.complete is True
        assert inspection.coverage.matched_occurrences == 1
        navigation = inspection.symbol_navigation
        assert navigation is not None
        assert navigation.candidate_precise_files == 1
        assert navigation.parsed_precise_files == 0
        assert navigation.parse_skipped_files == 1
        assert navigation.precise_classification_complete is False
        assert navigation.relations == ()
    finally:
        connection.close()


def test_symbol_navigation_keeps_relation_line_when_evidence_line_is_very_long(
    tmp_path: Path,
) -> None:
    long_prefix = "x" * 1800
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": f"value = '{long_prefix}'; target_call()\n",
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        call = next(item for item in navigation.relations if item.kind == "call")
        assert call.evidence is not None
        assert call.evidence.start_line == 1
        assert call.evidence.end_line == 1
        assert call.evidence.snippet
        assert call.evidence.truncated is True
    finally:
        connection.close()


def test_symbol_navigation_reports_unicode_character_column_for_definition(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/model.py": "π, target_call = 1, 2\n"},
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        definition = next(item for item in navigation.relations if item.kind == "definition")
        assert definition.line == 1
        assert definition.column == 4
    finally:
        connection.close()


def test_python_keyword_exact_search_does_not_claim_symbol_navigation(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/service.py": "def run():\n    return 1\n"},
    )
    try:
        inspection = search_exact_source_inspection(
            connection, workspace_id, "return", scope=ProjectSearchScope.CODE
        )
        assert inspection.coverage is not None
        assert inspection.coverage.matched_occurrences == 1
        assert inspection.symbol_navigation is None
    finally:
        connection.close()


def test_symbol_navigation_preserves_relative_import_target(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"pkg/use.py": "from .service import target_call\n"},
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        relation = next(item for item in navigation.relations if item.kind == "import")
        assert relation.target == ".service.target_call"
    finally:
        connection.close()
