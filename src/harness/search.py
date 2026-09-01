from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from harness.index import IndexedFileKind, IndexingError
from harness.registry import get_workspace
from harness.search_text import (
    analyze_search_query,
    is_document_path,
    is_generated_text_output_path,
    matching_term_count,
    query_term_prefixes,
)

DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
MAX_SEARCH_QUERY_BYTES = 256


class SearchError(RuntimeError):
    """Raised when an indexed-path search request violates its bounded contract."""


class IndexedPathSearchScope(StrEnum):
    """Mechanical path classes supported by the indexed-path search channel."""

    ALL = "all"
    CODE = "code"
    DOCS = "docs"


class SearchMatchKind(StrEnum):
    """Mechanical reason one indexed path matched the search query."""

    EXACT_PATH = "exact_path"
    EXACT_FILENAME = "exact_filename"
    IDENTIFIER_TOKENS = "identifier_tokens"
    PATH_SUBSTRING = "path_substring"


@dataclass(frozen=True, slots=True)
class IndexedPathSearchResult:
    """One bounded search hit without source text or semantic state."""

    relative_path: str
    kind: IndexedFileKind
    size_bytes: int
    match_kind: SearchMatchKind


def search_indexed_paths(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    scope: IndexedPathSearchScope = IndexedPathSearchScope.ALL,
) -> tuple[IndexedPathSearchResult, ...]:
    """Search one Workspace's current Structural Index using deterministic path signals."""
    get_workspace(connection, workspace_id)
    normalized_query = _validate_query(query)
    _validate_limit(limit)
    if not isinstance(scope, IndexedPathSearchScope):
        raise SearchError("search scope is unsupported")

    query_path = normalized_query.replace("\\", "/").casefold()
    query_terms = analyze_search_query(normalized_query).terms
    ranked: list[tuple[int, str, IndexedPathSearchResult]] = []

    for relative_path, kind_value, size_bytes in _iter_indexed_path_candidates(
        connection,
        workspace_id,
        query_path,
        query_terms,
    ):
        if not isinstance(relative_path, str) or not isinstance(kind_value, str):
            raise IndexingError("indexed file row has invalid persisted types")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise IndexingError("indexed file row has invalid persisted types")
        try:
            kind = IndexedFileKind(kind_value)
        except ValueError as exc:
            raise IndexingError(f"indexed file row has unsupported kind: {kind_value!r}") from exc
        is_document = is_document_path(relative_path)
        if scope is not IndexedPathSearchScope.ALL and is_generated_text_output_path(relative_path):
            continue
        if scope is IndexedPathSearchScope.DOCS and not is_document:
            continue
        if scope is IndexedPathSearchScope.CODE and is_document:
            continue
        match = _match_path(relative_path, query_path, query_terms)
        if match is None:
            continue
        rank, match_kind = match
        ranked.append(
            (
                rank,
                relative_path.casefold(),
                IndexedPathSearchResult(
                    relative_path=relative_path,
                    kind=kind,
                    size_bytes=size_bytes,
                    match_kind=match_kind,
                ),
            )
        )

    ranked.sort(key=lambda item: (item[0], item[1], item[2].relative_path))
    return tuple(item[2] for item in ranked[:limit])


def _iter_indexed_path_candidates(
    connection: sqlite3.Connection,
    workspace_id: str,
    query_path: str,
    query_terms: tuple[str, ...],
) -> sqlite3.Cursor:
    needles = _path_candidate_needles(query_path, query_terms)
    clauses = ["LOWER(relative_path) = ?"]
    params: list[object] = [workspace_id, query_path]
    for needle in needles:
        clauses.append("LOWER(relative_path) LIKE ? ESCAPE '!'")
        params.append(_like_contains(needle))
    return connection.execute(
        f"""
        SELECT relative_path, kind, size_bytes
        FROM indexed_files
        WHERE workspace_id = ?
          AND ({" OR ".join(clauses)})
        """,
        params,
    )


def _path_candidate_needles(query_path: str, query_terms: tuple[str, ...]) -> tuple[str, ...]:
    needles: list[str] = [query_path]
    seen = {query_path}
    for term in query_terms:
        for prefix in query_term_prefixes(term):
            if prefix and prefix not in seen:
                seen.add(prefix)
                needles.append(prefix)
    return tuple(needles)


def _like_contains(value: str) -> str:
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _validate_query(query: str) -> str:
    normalized = query.strip()
    if not normalized or "\x00" in normalized:
        raise SearchError("search query must be a non-empty bounded string")
    if len(normalized.encode("utf-8")) > MAX_SEARCH_QUERY_BYTES:
        raise SearchError(f"search query exceeds {MAX_SEARCH_QUERY_BYTES} UTF-8 bytes")
    return normalized


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise SearchError(f"search limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")


def _match_path(
    relative_path: str,
    query_path: str,
    query_terms: tuple[str, ...],
) -> tuple[int, SearchMatchKind] | None:
    path = relative_path.casefold()
    filename = relative_path.rsplit("/", 1)[-1].casefold()
    if path == query_path:
        return 0, SearchMatchKind.EXACT_PATH
    if filename == query_path:
        return 1, SearchMatchKind.EXACT_FILENAME

    if query_terms and matching_term_count(query_terms, relative_path) == len(query_terms):
        return 2, SearchMatchKind.IDENTIFIER_TOKENS
    if query_path in path:
        return 3, SearchMatchKind.PATH_SUBSTRING
    return None
