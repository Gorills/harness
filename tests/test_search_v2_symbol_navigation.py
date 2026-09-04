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
        {"src/app.vue": "function target_call() {}\ntarget_call()\n"},
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
        assert navigation.precise_classification_complete is False
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


def test_symbol_navigation_resolves_python_from_import_alias_call(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        alias_call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert alias_call.target == "tc"
        assert alias_call.resolved_target == "service.target_call"
        assert alias_call.resolution_kind == "python_from_import_binding"
        assert alias_call.resolved_definition_path == "src/service.py"
        assert alias_call.resolved_definition_line == 1
        assert alias_call.resolved_definition_column == 5
        assert alias_call.resolved_definition_kind == "function"
        assert alias_call.resolution_validation_kind == "python_workspace_direct_export"
        payload = project_symbol_navigation_payload(navigation)
        relations_payload = payload["relations"]
        assert isinstance(relations_payload, list)
        payload_call = next(
            item
            for item in relations_payload
            if isinstance(item, dict)
            and item.get("kind") == "call"
            and item.get("path") == "src/use.py"
        )
        assert payload_call["target"] == "tc"
        assert payload_call["resolved_target"] == "service.target_call"
        assert payload_call["resolution_kind"] == "python_from_import_binding"
        assert payload_call["resolved_definition_path"] == "src/service.py"
        assert payload_call["resolved_definition_line"] == 1
        assert payload_call["resolved_definition_column"] == 5
        assert payload_call["resolved_definition_kind"] == "function"
        assert payload_call["resolution_validation_kind"] == "python_workspace_direct_export"
        assert "resolution_module" not in payload_call
    finally:
        connection.close()


def test_symbol_navigation_resolves_python_module_alias_qualified_call(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/use.py": "import service as svc\n\ndef invoke():\n    return svc.target_call()\n"},
    )
    try:
        inspection = search_exact_source_inspection(
            connection,
            workspace_id,
            "service.target_call",
            scope=ProjectSearchScope.CODE,
        )
        assert inspection.coverage is not None
        assert inspection.coverage.matched_occurrences == 0
        navigation = inspection.symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 1
        call = next(item for item in navigation.relations if item.kind == "call")
        assert call.target == "svc.target_call"
        assert call.resolved_target == "service.target_call"
        assert call.resolution_kind == "python_import_binding"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_validates_python_module_alias_against_unique_workspace_export(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": "import service as svc\n\ndef invoke():\n    return svc.target_call()\n",
        },
    )
    try:
        inspection = search_exact_source_inspection(
            connection,
            workspace_id,
            "service.target_call",
            scope=ProjectSearchScope.CODE,
        )
        assert inspection.coverage is not None
        assert inspection.coverage.matched_occurrences == 0
        navigation = inspection.symbol_navigation
        assert navigation is not None
        call = next(item for item in navigation.relations if item.kind == "call")
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path == "src/service.py"
        assert call.resolved_definition_line == 1
        assert call.resolved_definition_kind == "function"
        assert call.resolution_validation_kind == "python_workspace_direct_export"
    finally:
        connection.close()


def test_symbol_navigation_validates_relative_python_import_against_package_export(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/service.py": "def target_call():\n    return 1\n",
            "pkg/use.py": (
                "from .service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "pkg/use.py"
        )
        assert call.resolved_target == ".service.target_call"
        assert call.resolved_definition_path == "pkg/service.py"
        assert call.resolution_validation_kind == "python_workspace_direct_export"
    finally:
        connection.close()


def test_symbol_navigation_validates_pure_relative_import_only_against_package_init(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "pkg.py": "def target_call():\n    return 99\n",
            "pkg/__init__.py": "def target_call():\n    return 1\n",
            "pkg/use.py": ("from . import target_call as tc\n\ndef invoke():\n    return tc()\n"),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "pkg/use.py"
        )
        assert call.resolved_target == ".target_call"
        assert call.resolved_definition_path == "pkg/__init__.py"
        assert call.resolution_validation_kind == "python_workspace_direct_export"
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_fails_closed_on_ambiguous_module(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "other/service.py": "def target_call():\n    return 2\n",
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_follows_single_reexport_chain(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/impl.py": "def target_call():\n    return 1\n",
            "src/service.py": "from impl import target_call\n",
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path == "src/impl.py"
        assert call.resolved_definition_line == 1
        assert call.resolved_definition_kind == "function"
        assert call.resolution_validation_kind == "python_workspace_reexport_chain"
        payload = project_symbol_navigation_payload(navigation)
        relations_payload = payload["relations"]
        assert isinstance(relations_payload, list)
        payload_call = next(
            item
            for item in relations_payload
            if isinstance(item, dict)
            and item.get("kind") == "call"
            and item.get("path") == "src/use.py"
        )
        assert payload_call["resolution_validation_kind"] == ("python_workspace_reexport_chain")
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_follows_relative_aliased_reexports(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/impl.py": "def implementation():\n    return 1\n",
            "pkg/api.py": "from .impl import implementation as target_call\n",
            "pkg/facade.py": "from .api import target_call\n",
            "pkg/use.py": (
                "from .facade import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "pkg/use.py"
        )
        assert call.resolved_target == ".facade.target_call"
        assert call.resolved_definition_path == "pkg/impl.py"
        assert call.resolved_definition_line == 1
        assert call.resolved_definition_kind == "function"
        assert call.resolution_validation_kind == "python_workspace_reexport_chain"
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_fails_closed_on_ambiguous_reexport(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/impl_a.py": "def target_call():\n    return 1\n",
            "src/impl_b.py": "def target_call():\n    return 2\n",
            "src/service.py": ("from impl_a import target_call\nfrom impl_b import target_call\n"),
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_fails_closed_on_reexport_cycle(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "from facade import target_call\n",
            "src/facade.py": "from service import target_call\n",
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_accepts_four_reexport_edges(
    tmp_path: Path,
) -> None:
    files = {
        "src/use.py": (
            "from module_0 import target_call as tc\n\ndef invoke():\n    return tc()\n"
        ),
        "src/module_4.py": "def target_call():\n    return 1\n",
    }
    for index in range(4):
        files[f"src/module_{index}.py"] = f"from module_{index + 1} import target_call\n"
    _root, connection, workspace_id = _registered(tmp_path, files)
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_definition_path == "src/module_4.py"
        assert call.resolution_validation_kind == "python_workspace_reexport_chain"
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_rejects_competing_conditional_reexport(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/impl.py": "def target_call():\n    return 1\n",
            "src/other.py": "def target_call():\n    return 2\n",
            "src/service.py": (
                "from impl import target_call\nif FLAG:\n    from other import target_call\n"
            ),
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_bounds_reexport_depth(
    tmp_path: Path,
) -> None:
    files = {
        "src/use.py": (
            "from module_0 import target_call as tc\n\ndef invoke():\n    return tc()\n"
        ),
        "src/module_5.py": "def target_call():\n    return 1\n",
    }
    for index in range(5):
        files[f"src/module_{index}.py"] = f"from module_{index + 1} import target_call\n"
    _root, connection, workspace_id = _registered(tmp_path, files)
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "module_0.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_does_not_follow_pure_relative_reexport(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "pkg/__init__.py": "from . import target_call\n",
            "pkg/target_call.py": "def target_call():\n    return 1\n",
            "src/use.py": ("from pkg import target_call as tc\n\ndef invoke():\n    return tc()\n"),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "pkg.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_workspace_export_validation_drops_changed_target_definition(
    tmp_path: Path,
) -> None:
    root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": (
                "from service import target_call as tc\n\ndef invoke():\n    return tc()\n"
            ),
        },
    )
    try:
        (root / "src" / "service.py").write_text(
            "def replacement():\n    return 2\n",
            encoding="utf-8",
        )
        inspection = search_exact_source_inspection(
            connection,
            workspace_id,
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        assert inspection.coverage is not None
        assert inspection.coverage.complete is False
        navigation = inspection.symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
    finally:
        connection.close()


def test_symbol_navigation_resolves_python_import_from_enclosing_function(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": (
                "def outer():\n"
                "    from service import target_call as tc\n"
                "    def inner():\n"
                "        return tc()\n"
                "    return inner\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.scope == "outer.inner"
        assert call.target == "tc"
        assert call.resolved_target == "service.target_call"
        assert call.resolution_kind == "python_from_import_binding"
        assert call.resolved_definition_path == "src/service.py"
        assert call.resolution_validation_kind == "python_workspace_direct_export"
    finally:
        connection.close()


def test_symbol_navigation_resolves_nearest_safe_enclosing_function_import(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": (
                "def outer():\n"
                "    from service import target_call as tc\n"
                "    def middle():\n"
                "        def inner():\n"
                "            return tc()\n"
                "        return inner\n"
                "    return middle\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        call = next(
            item
            for item in navigation.relations
            if item.kind == "call" and item.path == "src/use.py"
        )
        assert call.scope == "outer.middle.inner"
        assert call.resolved_target == "service.target_call"
        assert call.resolved_definition_path == "src/service.py"
    finally:
        connection.close()


def test_symbol_navigation_enclosing_import_fails_closed_on_intermediate_shadowing(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": (
                "def outer():\n"
                "    from service import target_call as tc\n"
                "    def middle():\n"
                "        tc = lambda: 0\n"
                "        def inner():\n"
                "            return tc()\n"
                "        return inner\n"
                "    return middle\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.import_count == 1
        assert navigation.call_count == 0
        assert all(item.resolved_target is None for item in navigation.relations)
    finally:
        connection.close()


def test_symbol_navigation_enclosing_import_fails_closed_on_nonlocal_declaration(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": (
                "def outer():\n"
                "    from service import target_call as tc\n"
                "    def inner():\n"
                "        nonlocal tc\n"
                "        return tc()\n"
                "    return inner\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.import_count == 1
        assert navigation.call_count == 0
    finally:
        connection.close()


def test_symbol_navigation_resolves_direct_self_method_receiver(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    def target_call(self):\n"
                "        return 1\n\n"
                "    def invoke(self):\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        call = next(item for item in navigation.relations if item.kind == "call")
        assert call.scope == "Worker.invoke"
        assert call.target == "self.target_call"
        assert call.resolved_target == "Worker.target_call"
        assert call.resolution_kind == "python_self_method_binding"
        assert call.resolved_definition_path is None
        assert call.resolution_validation_kind is None
        payload = project_symbol_navigation_payload(navigation)
        payload_relations = payload["relations"]
        assert isinstance(payload_relations, list)
        payload_call = next(
            item
            for item in payload_relations
            if isinstance(item, dict) and item.get("kind") == "call"
        )
        assert payload_call["resolved_target"] == "Worker.target_call"
        assert payload_call["resolution_kind"] == "python_self_method_binding"
    finally:
        connection.close()


def test_symbol_navigation_resolves_direct_cls_method_receiver(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    @classmethod\n"
                "    def target_call(cls):\n"
                "        return 1\n\n"
                "    @classmethod\n"
                "    def invoke(cls):\n"
                "        return cls.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        call = next(item for item in navigation.relations if item.kind == "call")
        assert call.target == "cls.target_call"
        assert call.resolved_target == "Worker.target_call"
        assert call.resolution_kind == "python_cls_method_binding"
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_accepts_safe_static_target(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    @staticmethod\n"
                "    def target_call():\n"
                "        return 1\n\n"
                "    def invoke(self):\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        call = next(item for item in navigation.relations if item.kind == "call")
        assert call.resolved_target == "Worker.target_call"
        assert call.resolution_kind == "python_self_method_binding"
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_requires_receiver_as_first_positional_parameter(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    def target_call(self):\n"
                "        return 1\n\n"
                "    def invoke(other, self):\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 0
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_fails_closed_on_decorated_caller(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "def trace(fn):\n"
                "    return fn\n\n"
                "class Worker:\n"
                "    def target_call(self):\n"
                "        return 1\n\n"
                "    @trace\n"
                "    def invoke(self):\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 0
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_fails_closed_on_receiver_rebinding(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    def target_call(self):\n"
                "        return 1\n\n"
                "    def invoke(self):\n"
                "        self = object()\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 0
        assert all(item.resolved_target is None for item in navigation.relations)
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_fails_closed_on_custom_target_descriptor(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    @property\n"
                "    def target_call(self):\n"
                "        return lambda: 1\n\n"
                "    def invoke(self):\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 0
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_does_not_infer_inherited_method(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Base:\n"
                "    def target_call(self):\n"
                "        return 1\n\n"
                "class Worker(Base):\n"
                "    def invoke(self):\n"
                "        return self.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 0
    finally:
        connection.close()


def test_symbol_navigation_receiver_resolution_does_not_follow_member_chain(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/worker.py": (
                "class Worker:\n"
                "    def target_call(self):\n"
                "        return 1\n\n"
                "    def invoke(self):\n"
                "        return self.helper.target_call()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Worker.target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.call_count == 0
    finally:
        connection.close()


def test_symbol_navigation_python_import_binding_fails_closed_on_rebinding(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/parameter.py": (
                "from service import target_call as tc\n\ndef invoke(tc):\n    return tc()\n"
            ),
            "src/module.py": (
                "from service import target_call as tc\n"
                "tc = lambda: 0\n\n"
                "def invoke():\n"
                "    return tc()\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.import_count == 2
        assert navigation.call_count == 0
        assert all(item.resolved_target is None for item in navigation.relations)
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


def test_symbol_navigation_classifies_typescript_relations(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/service.ts": "export function target_call(value: number) { return value + 1 }\n",
            "src/use.ts": (
                "import { target_call } from './service'\n"
                "export function run() { return target_call(1) }\n"
            ),
            "tests/service.test.ts": (
                "import { target_call } from '../src/service'\n"
                "test('works', () => target_call(1))\n"
            ),
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == ("typescript",)
        assert navigation.candidate_precise_files == 3
        assert navigation.parsed_precise_files == 3
        assert navigation.definition_count == 1
        assert navigation.call_count == 2
        assert navigation.test_call_count == 1
        assert navigation.import_count == 2
        assert navigation.precise_classification_complete is True
        assert navigation.relations[0].kind == "definition"
        assert navigation.relations[0].target == "target_call"
    finally:
        connection.close()


def test_symbol_navigation_classifies_javascript_inheritance_and_method(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/model.js": (
                "class Base {}\n"
                "class Client extends Base { fetch() { return 1 } }\n"
                "function run() { return new Client().fetch() }\n"
            )
        },
    )
    try:
        base = search_exact_source_inspection(
            connection, workspace_id, "Base", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        fetch = search_exact_source_inspection(
            connection, workspace_id, "fetch", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert base is not None
        assert base.precise_languages == ("javascript",)
        assert base.definition_count == 1
        assert base.inheritance_count == 1
        assert fetch is not None
        definition = next(item for item in fetch.relations if item.kind == "definition")
        call = next(item for item in fetch.relations if item.kind == "call")
        assert definition.target == "Client.fetch"
        assert definition.symbol_kind == "method"
        assert call.target.endswith(".fetch")
    finally:
        connection.close()


def test_symbol_navigation_classifies_go_method_and_calls(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/client.go": (
                "package service\n"
                "type Client struct{}\n"
                "func (c Client) Fetch() { target_call() }\n"
                "func target_call() {}\n"
            )
        },
    )
    try:
        target = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        fetch = search_exact_source_inspection(
            connection, workspace_id, "Fetch", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert target is not None
        assert target.precise_languages == ("go",)
        assert target.definition_count == 1
        assert target.call_count == 1
        assert fetch is not None
        definition = next(item for item in fetch.relations if item.kind == "definition")
        assert definition.target == "Client.Fetch"
        assert definition.scope == "Client"
    finally:
        connection.close()


def test_symbol_navigation_classifies_rust_trait_impl_calls_and_use(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/lib.rs": (
                "use crate::service::target_call;\n"
                "trait Repo { fn fetch(&self); }\n"
                "struct Client;\n"
                "impl Repo for Client { fn fetch(&self) { target_call(); } }\n"
            )
        },
    )
    try:
        target = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        repo = search_exact_source_inspection(
            connection, workspace_id, "Repo", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        fetch = search_exact_source_inspection(
            connection, workspace_id, "fetch", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert target is not None
        assert target.precise_languages == ("rust",)
        assert target.call_count == 1
        assert target.import_count == 1
        assert repo is not None
        assert repo.definition_count == 1
        assert repo.inheritance_count == 1
        assert fetch is not None
        assert fetch.definition_count == 2
        assert {item.target for item in fetch.relations if item.kind == "definition"} == {
            "Repo.fetch",
            "Client.fetch",
        }
    finally:
        connection.close()


def test_symbol_navigation_classifies_java_relations(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/Client.java": (
                "import pkg.Target;\n"
                "class Base {}\n"
                "interface Repo {}\n"
                "class Client extends Base implements Repo {\n"
                "  void fetch() { target_call(); }\n"
                "  static void target_call() {}\n"
                "}\n"
            )
        },
    )
    try:
        target = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        base = search_exact_source_inspection(
            connection, workspace_id, "Base", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert target is not None
        assert target.precise_languages == ("java",)
        assert target.definition_count == 1
        assert target.call_count == 1
        assert next(item for item in target.relations if item.kind == "definition").target == (
            "Client.target_call"
        )
        assert base is not None
        assert base.definition_count == 1
        assert base.inheritance_count == 1
    finally:
        connection.close()


def test_symbol_navigation_reports_polyglot_parse_error_without_guessing(tmp_path: Path) -> None:
    root, connection, _workspace_id = _registered(
        tmp_path,
        {"src/app.ts": "function old_name() {}\n"},
    )
    try:
        (root / "src/app.ts").write_text(
            "function target_call( { target_call()\n",
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
        navigation = result.symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == ("typescript",)
        assert navigation.parse_failures == 1
        assert navigation.parsed_precise_files == 0
        assert navigation.relations == ()
    finally:
        connection.close()


def test_symbol_navigation_reports_multiple_precise_languages(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/a.ts": "export function target_call() {}\n",
            "src/b.go": "package b\nfunc target_call() {}\n",
            "src/c.vue": "function target_call() {}\n",
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == ("go", "typescript")
        assert navigation.candidate_precise_files == 2
        assert navigation.parsed_precise_files == 2
        assert navigation.matching_unsupported_files == 1
        assert navigation.precise_classification_complete is False
        assert navigation.definition_count == 2
    finally:
        connection.close()


def test_symbol_navigation_classifies_tsx_definition_and_call(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/Button.tsx": "export function Button() { return <button onClick={() => Button()} /> }\n"
        },
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Button", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == ("tsx",)
        assert navigation.definition_count == 1
        assert navigation.call_count == 1
        assert navigation.precise_classification_complete is True
    finally:
        connection.close()


def test_symbol_navigation_classifies_java_import(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {"src/App.java": "import pkg.Target;\nclass App { Target value; }\n"},
    )
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "Target", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        assert navigation.import_count == 1
        assert any(
            item.kind == "import" and item.target == "pkg.Target" for item in navigation.relations
        )
    finally:
        connection.close()


def test_symbol_navigation_classifies_rust_alias_and_grouped_imports(tmp_path: Path) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/lib.rs": (
                "use crate::service::target_call as tc;\n"
                "use crate::service::{other_call, third_call as third};\n"
                "fn run() { tc(); other_call(); third(); }\n"
            )
        },
    )
    try:
        alias = search_exact_source_inspection(
            connection, workspace_id, "tc", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        grouped = search_exact_source_inspection(
            connection, workspace_id, "other_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        grouped_alias = search_exact_source_inspection(
            connection, workspace_id, "third", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert alias is not None
        assert alias.import_count == 1
        assert any(
            item.kind == "import" and item.target == "crate::service::target_call"
            for item in alias.relations
        )
        assert alias.call_count == 1
        assert grouped is not None
        assert any(
            item.kind == "import" and item.target == "crate::service::other_call"
            for item in grouped.relations
        )
        assert grouped_alias is not None
        assert any(
            item.kind == "import" and item.target == "crate::service::third_call"
            for item in grouped_alias.relations
        )
    finally:
        connection.close()


def test_qualified_polyglot_calls_do_not_claim_runtime_receiver_types(tmp_path: Path) -> None:
    cases: tuple[tuple[str, str, str, str, set[str]], ...] = (
        (
            "src/app.ts",
            "class Client { fetch() {} }\nfunction run(client: Client) { client.fetch(); new Client().fetch(); }\n",
            "Client.fetch",
            "typescript",
            {"Client.fetch"},
        ),
        (
            "src/app.go",
            "package p\ntype Client struct{}\nfunc (c Client) Fetch(){}\nfunc run(c Client){ c.Fetch(); Client.Fetch(c) }\n",
            "Client.Fetch",
            "go",
            {"Client.Fetch"},
        ),
        (
            "src/app.rs",
            "struct Client; impl Client { fn fetch(&self){} } fn run(c: Client){ c.fetch(); Client::fetch(&c); }\n",
            "Client.fetch",
            "rust",
            {"Client::fetch"},
        ),
        (
            "src/App.java",
            "class Client { void fetch() {} } class App { void run(Client client) { client.fetch(); } }\n",
            "Client.fetch",
            "java",
            set(),
        ),
    )
    for index, (path, source, needle, language, expected_call_targets) in enumerate(cases):
        case_root = tmp_path / f"case-{index}"
        case_root.mkdir()
        _root, connection, workspace_id = _registered(case_root, {path: source})
        try:
            navigation = search_exact_source_inspection(
                connection, workspace_id, needle, scope=ProjectSearchScope.CODE
            ).symbol_navigation
            assert navigation is not None
            assert navigation.precise_languages == (language,)
            call_targets = {item.target for item in navigation.relations if item.kind == "call"}
            assert call_targets == expected_call_targets
            assert all(
                not target.lower().startswith("client.") or target in expected_call_targets
                for target in call_targets
            )
        finally:
            connection.close()


def test_java_symbol_navigation_only_classifies_field_declarators_as_class_variables(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/App.java": (
                "class App {\n"
                "  int target_field = 1;\n"
                "  Runnable r = () -> { int target_local = 2; };\n"
                "  void run() { int target_method_local = 3; }\n"
                "}\n"
            )
        },
    )
    try:
        field = search_exact_source_inspection(
            connection, workspace_id, "target_field", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        lambda_local = search_exact_source_inspection(
            connection, workspace_id, "target_local", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        method_local = search_exact_source_inspection(
            connection, workspace_id, "target_method_local", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert field is not None
        assert any(
            item.kind == "definition" and item.target == "App.target_field"
            for item in field.relations
        )
        assert lambda_local is not None
        assert lambda_local.definition_count == 0
        assert method_local is not None
        assert method_local.definition_count == 0
    finally:
        connection.close()


def test_polyglot_symbol_navigation_reports_unicode_character_column(tmp_path: Path) -> None:
    source = "const π = 1; function target_call() { return π }\n"
    _root, connection, workspace_id = _registered(tmp_path, {"src/app.ts": source})
    try:
        navigation = search_exact_source_inspection(
            connection, workspace_id, "target_call", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert navigation is not None
        definition = next(item for item in navigation.relations if item.kind == "definition")
        assert definition.line == 1
        assert definition.column == source.index("target_call") + 1
    finally:
        connection.close()


def test_typescript_symbol_navigation_classifies_import_alias_and_generic_inheritance(
    tmp_path: Path,
) -> None:
    _root, connection, workspace_id = _registered(
        tmp_path,
        {
            "src/model.ts": "export interface Base<T> {}\nexport interface Child extends Base<string> {}\n",
            "src/use.ts": "import { target_call as tc } from './service'; tc();\n",
        },
    )
    try:
        alias = search_exact_source_inspection(
            connection, workspace_id, "tc", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        base = search_exact_source_inspection(
            connection, workspace_id, "Base", scope=ProjectSearchScope.CODE
        ).symbol_navigation
        assert alias is not None
        assert alias.import_count == 1
        assert alias.call_count == 1
        assert any(
            item.kind == "import" and item.target == "./service.target_call"
            for item in alias.relations
        )
        assert base is not None
        assert base.definition_count == 1
        assert base.inheritance_count == 1
        assert any(item.kind == "inheritance" and item.target == "Base" for item in base.relations)
    finally:
        connection.close()
