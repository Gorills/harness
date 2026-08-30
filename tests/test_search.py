from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from harness.index import IndexedFileKind, scan_workspace
from harness.registry import create_project, register_workspace
from harness.search import (
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_QUERY_BYTES,
    IndexedPathSearchScope,
    SearchError,
    SearchMatchKind,
    search_indexed_paths,
)
from harness.storage import connect_database, initialize_database


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    _git(root, "init")
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
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
    return root


def _registered(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    root = _repo(
        tmp_path / "repo",
        {
            "src/rotateRefreshToken.py": ("TOP_SECRET_BODY = 'never expose through path search'\n"),
            "tests/rotate_refresh_token_test.py": ("def test_rotate_refresh_token(): pass\n"),
            "docs/auth-guide.md": "authentication guide\n",
            "src/tokenBucket.py": "class TokenBucket: pass\n",
            ".pytest_nutrition_all.out": "FastAPI endpoints nutrition test log\n",
        },
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return connection, workspace.workspace_id


def test_search_ranks_exact_path_and_filename_before_broader_matches(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        exact_path = search_indexed_paths(
            connection,
            workspace_id,
            "src/rotateRefreshToken.py",
        )
        exact_filename = search_indexed_paths(connection, workspace_id, "auth-guide.md")

        assert exact_path[0].relative_path == "src/rotateRefreshToken.py"
        assert exact_path[0].kind is IndexedFileKind.FILE
        assert exact_path[0].match_kind is SearchMatchKind.EXACT_PATH
        assert exact_filename[0].relative_path == "docs/auth-guide.md"
        assert exact_filename[0].match_kind is SearchMatchKind.EXACT_FILENAME
    finally:
        connection.close()


def test_scoped_path_search_excludes_generated_output_but_all_keeps_inventory_access(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        all_results = search_indexed_paths(
            connection,
            workspace_id,
            ".pytest_nutrition_all.out",
            scope=IndexedPathSearchScope.ALL,
        )
        code_results = search_indexed_paths(
            connection,
            workspace_id,
            ".pytest_nutrition_all.out",
            scope=IndexedPathSearchScope.CODE,
        )

        assert all_results[0].relative_path == ".pytest_nutrition_all.out"
        assert all_results[0].match_kind is SearchMatchKind.EXACT_PATH
        assert code_results == ()
    finally:
        connection.close()


def test_search_normalizes_camel_snake_and_natural_identifier_tokens(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        results = search_indexed_paths(connection, workspace_id, "rotate refresh token")

        assert [result.relative_path for result in results] == [
            "src/rotateRefreshToken.py",
            "tests/rotate_refresh_token_test.py",
        ]
        assert all(result.match_kind is SearchMatchKind.IDENTIFIER_TOKENS for result in results)
    finally:
        connection.close()


def test_search_ignores_conversational_filler_and_matches_prefix_inflections(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        results = search_indexed_paths(
            connection,
            workspace_id,
            "where rotations of refresh tokens happen",
        )

        assert [result.relative_path for result in results] == [
            "src/rotateRefreshToken.py",
            "tests/rotate_refresh_token_test.py",
        ]
        assert all(result.match_kind is SearchMatchKind.IDENTIFIER_TOKENS for result in results)
    finally:
        connection.close()


def test_search_uses_deterministic_substring_fallback(tmp_path: Path) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        results = search_indexed_paths(connection, workspace_id, "RefreshT")

        assert [result.relative_path for result in results] == ["src/rotateRefreshToken.py"]
        assert results[0].match_kind is SearchMatchKind.PATH_SUBSTRING
    finally:
        connection.close()


def test_search_is_strictly_workspace_scoped(tmp_path: Path) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        foreign_root = _repo(
            tmp_path / "foreign-repo",
            {"src/foreignTokenOnly.py": "class ForeignTokenOnly: pass\n"},
        )
        foreign_project = create_project(connection)
        foreign_workspace = register_workspace(
            connection,
            project_id=foreign_project.project_id,
            path=foreign_root,
        )
        scan_workspace(connection, foreign_workspace.workspace_id)

        assert search_indexed_paths(connection, workspace_id, "foreign token") == ()
        foreign_results = search_indexed_paths(
            connection,
            foreign_workspace.workspace_id,
            "foreign token",
        )
        assert [result.relative_path for result in foreign_results] == ["src/foreignTokenOnly.py"]
    finally:
        connection.close()


def test_search_limit_is_bounded_and_deterministic(tmp_path: Path) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        results = search_indexed_paths(connection, workspace_id, "token", limit=1)
        assert len(results) == 1
        assert results[0].relative_path == "src/rotateRefreshToken.py"

        with pytest.raises(SearchError, match="between 1"):
            search_indexed_paths(connection, workspace_id, "token", limit=0)
        with pytest.raises(SearchError, match="between 1"):
            search_indexed_paths(
                connection,
                workspace_id,
                "token",
                limit=MAX_SEARCH_LIMIT + 1,
            )
        with pytest.raises(SearchError, match="between 1"):
            search_indexed_paths(connection, workspace_id, "token", limit=True)
        with pytest.raises(SearchError, match="scope is unsupported"):
            search_indexed_paths(connection, workspace_id, "token", scope="docs")  # type: ignore[arg-type]
    finally:
        connection.close()


def test_search_rejects_empty_nul_and_oversized_queries(tmp_path: Path) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        with pytest.raises(SearchError, match="non-empty bounded"):
            search_indexed_paths(connection, workspace_id, "   ")
        with pytest.raises(SearchError, match="non-empty bounded"):
            search_indexed_paths(connection, workspace_id, "token\x00secret")
        with pytest.raises(SearchError, match=str(MAX_SEARCH_QUERY_BYTES)):
            search_indexed_paths(
                connection,
                workspace_id,
                "x" * (MAX_SEARCH_QUERY_BYTES + 1),
            )
    finally:
        connection.close()


def test_search_scope_filters_before_limit(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "scoped-repo",
        {
            **{f"aa/token_{index:02}.py": "x = 1\n" for index in range(12)},
            "zz/token_notes.md": "token documentation\n",
        },
    )
    database = tmp_path / "scoped.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)

        docs = search_indexed_paths(
            connection,
            workspace.workspace_id,
            "token",
            limit=1,
            scope=IndexedPathSearchScope.DOCS,
        )
        code = search_indexed_paths(
            connection,
            workspace.workspace_id,
            "token",
            limit=3,
            scope=IndexedPathSearchScope.CODE,
        )
        assert [item.relative_path for item in docs] == ["zz/token_notes.md"]
        assert all(not item.relative_path.endswith(".md") for item in code)
    finally:
        connection.close()


def test_search_scope_classifies_readme_adr_and_asciidoc_as_docs(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "documentation-corpus",
        {
            "README": "project overview\n",
            "ADR-auth": "authentication decision\n",
            "guide.adoc": "= Operator guide\n",
            "src/readme_parser.py": "VALUE = 1\n",
        },
    )
    database = tmp_path / "documentation.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)

        docs = search_indexed_paths(
            connection,
            workspace.workspace_id,
            "readme",
            scope=IndexedPathSearchScope.DOCS,
        )
        code = search_indexed_paths(
            connection,
            workspace.workspace_id,
            "readme",
            scope=IndexedPathSearchScope.CODE,
        )
        adr = search_indexed_paths(
            connection,
            workspace.workspace_id,
            "ADR-auth",
            scope=IndexedPathSearchScope.DOCS,
        )
        asciidoc = search_indexed_paths(
            connection,
            workspace.workspace_id,
            "guide.adoc",
            scope=IndexedPathSearchScope.DOCS,
        )

        assert [item.relative_path for item in docs] == ["README"]
        assert [item.relative_path for item in code] == ["src/readme_parser.py"]
        assert [item.relative_path for item in adr] == ["ADR-auth"]
        assert [item.relative_path for item in asciidoc] == ["guide.adoc"]
    finally:
        connection.close()


def test_search_result_does_not_expose_source_or_internal_index_fields(
    tmp_path: Path,
) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        result = search_indexed_paths(connection, workspace_id, "rotateRefreshToken.py")[0]
        exposed = asdict(result)

        assert set(exposed) == {"relative_path", "kind", "size_bytes", "match_kind"}
        assert "content_sha256" not in exposed
        assert "TOP_SECRET_BODY" not in repr(result)
        assert "never expose through path search" not in repr(result)
    finally:
        connection.close()
