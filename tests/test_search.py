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
    IndexedPathSearchResult,
    SearchError,
    SearchMatchKind,
    search_indexed_paths,
)
from harness.storage import connect_database, initialize_database


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _registered(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "tests").mkdir()
    _git(root, "init")

    (root / "src" / "rotateRefreshToken.py").write_text(
        "TOP_SECRET_BODY = 'never expose through path search'\n",
        encoding="utf-8",
    )
    (root / "tests" / "rotate_refresh_token_test.py").write_text(
        "def test_rotate_refresh_token(): pass\n",
        encoding="utf-8",
    )
    (root / "docs" / "auth-guide.md").write_text("authentication guide\n", encoding="utf-8")
    (root / "src" / "tokenBucket.py").write_text("class TokenBucket: pass\n", encoding="utf-8")
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

    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return connection, workspace.workspace_id


def test_search_ranks_exact_path_and_filename_before_broader_matches(tmp_path: Path) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        exact_path = search_indexed_paths(connection, workspace_id, "src/rotateRefreshToken.py")
        exact_filename = search_indexed_paths(connection, workspace_id, "auth-guide.md")

        assert exact_path[0] == IndexedPathSearchResult(
            relative_path="src/rotateRefreshToken.py",
            kind=IndexedFileKind.FILE,
            size_bytes=exact_path[0].size_bytes,
            match_kind=SearchMatchKind.EXACT_PATH,
        )
        assert exact_filename[0].relative_path == "docs/auth-guide.md"
        assert exact_filename[0].match_kind is SearchMatchKind.EXACT_FILENAME
    finally:
        connection.close()


def test_search_normalizes_camel_snake_and_natural_identifier_tokens(tmp_path: Path) -> None:
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


def test_search_uses_deterministic_substring_fallback(tmp_path: Path) -> None:
    connection, workspace_id = _registered(tmp_path)
    try:
        results = search_indexed_paths(connection, workspace_id, "RefreshT")

        assert [result.relative_path for result in results] == ["src/rotateRefreshToken.py"]
        assert results[0].match_kind is SearchMatchKind.PATH_SUBSTRING
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
            search_indexed_paths(connection, workspace_id, "token", limit=MAX_SEARCH_LIMIT + 1)
        with pytest.raises(SearchError, match="between 1"):
            search_indexed_paths(connection, workspace_id, "token", limit=True)
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
            search_indexed_paths(connection, workspace_id, "x" * (MAX_SEARCH_QUERY_BYTES + 1))
    finally:
        connection.close()


def test_search_result_does_not_expose_source_or_internal_index_fields(tmp_path: Path) -> None:
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
