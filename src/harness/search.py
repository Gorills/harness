from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from harness.index import IndexedFileKind, list_indexed_files
from harness.registry import get_workspace

DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 50
MAX_SEARCH_QUERY_BYTES = 256

_CAMEL_LOWER_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_IDENTIFIER = re.compile(r"[^0-9A-Za-z]+")


class SearchError(RuntimeError):
    """Raised when an indexed-path search request violates its bounded contract."""


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
) -> tuple[IndexedPathSearchResult, ...]:
    """Search one Workspace's current Structural Index using deterministic path signals."""
    get_workspace(connection, workspace_id)
    normalized_query = _validate_query(query)
    _validate_limit(limit)

    query_path = normalized_query.replace("\\", "/").casefold()
    query_tokens = frozenset(_identifier_tokens(normalized_query))
    ranked: list[tuple[int, str, IndexedPathSearchResult]] = []

    for record in list_indexed_files(connection, workspace_id):
        match = _match_path(record.relative_path, query_path, query_tokens)
        if match is None:
            continue
        rank, match_kind = match
        ranked.append(
            (
                rank,
                record.relative_path.casefold(),
                IndexedPathSearchResult(
                    relative_path=record.relative_path,
                    kind=record.kind,
                    size_bytes=record.size_bytes,
                    match_kind=match_kind,
                ),
            )
        )

    ranked.sort(key=lambda item: (item[0], item[1], item[2].relative_path))
    return tuple(item[2] for item in ranked[:limit])


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
    query_tokens: frozenset[str],
) -> tuple[int, SearchMatchKind] | None:
    path = relative_path.casefold()
    filename = relative_path.rsplit("/", 1)[-1].casefold()
    if path == query_path:
        return 0, SearchMatchKind.EXACT_PATH
    if filename == query_path:
        return 1, SearchMatchKind.EXACT_FILENAME

    path_tokens = frozenset(_identifier_tokens(relative_path))
    if query_tokens and query_tokens.issubset(path_tokens):
        return 2, SearchMatchKind.IDENTIFIER_TOKENS
    if query_path in path:
        return 3, SearchMatchKind.PATH_SUBSTRING
    return None


def _identifier_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for component in _NON_IDENTIFIER.sub(" ", value).split():
        split_component = _CAMEL_LOWER_BOUNDARY.sub(" ", component)
        split_component = _CAMEL_ACRONYM_BOUNDARY.sub(" ", split_component)
        tokens.extend(token.casefold() for token in split_component.split())
    return tuple(tokens)
