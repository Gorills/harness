from __future__ import annotations

import json
import keyword
import re
import sqlite3
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from harness.index import (
    MAX_EXACT_SEARCH_FILE_BYTES,
    ExactSearchReadStatus,
    IndexedFileKind,
    SearchEvidenceReadStatus,
    get_indexed_file,
    read_current_exact_search_text,
    read_current_search_text,
)
from harness.knowledge import (
    KnowledgeCardRecord,
    KnowledgeError,
    KnowledgeFreshness,
    KnowledgeSourceType,
    get_knowledge_card,
)
from harness.registry import WorkspaceRecord, get_project, get_workspace
from harness.search import (
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_QUERY_BYTES,
    IndexedPathSearchScope,
    SearchError,
    search_indexed_paths,
)
from harness.search_text import (
    AnalyzedSearchQuery,
    analyze_search_query,
    contains_term_phrase,
    is_document_path,
    is_generated_text_output_path,
    matching_term_count,
)
from harness.symbol_navigation import (
    SyntaxRelation,
    analyze_precise_symbol_relations,
    is_precise_symbol_path,
    precise_symbol_language,
)
from harness.task_checkpoints import TaskCheckpointError, get_task_checkpoint, list_task_events
from harness.tasks import TaskNotFoundError, get_relevant_task, get_task, get_task_stack_hints
from harness.verification import list_checkpoint_verification

_DEFAULT_CANDIDATE_LIMIT = 96
_MAX_CANDIDATE_LIMIT = 384
_SUMMARY_MAX_BYTES = 384
_CONTEXT_HISTORY_LIMIT = 4
_CONTEXT_TEXT_MAX_BYTES = 1024
_CONTEXT_VERIFICATION_LIMIT = 4
_CONTEXT_VERIFICATION_EVIDENCE_MAX_BYTES = 512
_CONTEXT_KNOWLEDGE_ANCHOR_LIMIT = 4
_CONTEXT_KNOWLEDGE_ANCHOR_BYTES = 2048
_CONTEXT_STACK_HINT_LIMIT = 8
_CONTEXT_CHANGED_PATH_LIMIT = 16
_CONTEXT_CHANGED_PATH_BYTES = 2048
MAX_PROJECT_CONTEXT_REF_BYTES = 4096 + len("code:")
_FILE_CANDIDATE_LIMIT = 96
MAX_SEARCH_EVIDENCE_SNIPPET_LINES = 48
MAX_SEARCH_EVIDENCE_SNIPPET_BYTES = 3072
MAX_SEARCH_EVIDENCE_HITS = 3
MAX_EXACT_SEARCH_NEEDLE_BYTES = 256
MAX_EXACT_SEARCH_LOCATIONS = 24
MAX_EXACT_SEARCH_PREVIEW_BYTES = 160
MAX_EXACT_SEARCH_SCAN_BYTES = 64 * 1024 * 1024
MAX_EXACT_SEARCH_COVERAGE_BYTES = 4 * 1024
MAX_SYMBOL_NAVIGATION_RELATIONS = 16
MAX_SYMBOL_NAVIGATION_BYTES = 5 * 1024
MAX_SYMBOL_RELATION_TEXT_BYTES = 512
PROJECT_SEARCH_MAX_BYTES = 12 * 1024
_SEARCH_EVIDENCE_ENVELOPE_RESERVE_BYTES = 768
EVIDENCE_REASON_CHANGED_SINCE_INDEX = "changed_since_index"
EVIDENCE_REASON_NOT_RELOCATED = "current_match_not_relocated"
EVIDENCE_REASON_PATH_ONLY = "path_only"
EVIDENCE_REASON_RESPONSE_BUDGET = "response_budget"

_QUALITY_EXACT_PATH = 0
_QUALITY_EXACT_FILENAME = 1
_QUALITY_EXACT_FILENAME_STEM = 2
_QUALITY_TITLE_OR_IDENTIFIER_PHRASE = 3
_QUALITY_ALL_TERMS = 4
_QUALITY_PARTIAL = 5
_QUALITY_STALE_OFFSET = 3


class ProjectRetrievalError(RuntimeError):
    """Base class for bounded Project Intelligence retrieval failures."""


class ProjectRetrievalRefError(ProjectRetrievalError):
    """Raised when a selected context ref is invalid or outside the active Project."""


class ProjectSearchScope(StrEnum):
    ALL = "all"
    CODE = "code"
    DOCS = "docs"
    KNOWLEDGE = "knowledge"
    TASKS = "tasks"


class ProjectSearchKind(StrEnum):
    CODE = "code"
    DOC = "doc"
    KNOWLEDGE = "knowledge"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class ProjectSearchEvidence:
    start_line: int
    end_line: int
    snippet: str
    truncated: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ProjectExactSearchLocation:
    path: str
    line: int
    column: int
    preview: str

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class ProjectExactSearchCoverage:
    needle: str
    needle_kind: str
    case_sensitive: bool
    matched_files: int
    matched_occurrences: int
    matched_lines: int
    scanned_files: int
    scanned_bytes: int
    non_text_files: int
    unavailable_files: int
    complete: bool
    locations_truncated: bool
    locations: tuple[ProjectExactSearchLocation, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "needle": self.needle,
            "needle_kind": self.needle_kind,
            "case_sensitive": self.case_sensitive,
            "matched_files": self.matched_files,
            "matched_occurrences": self.matched_occurrences,
            "matched_lines": self.matched_lines,
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "non_text_files": self.non_text_files,
            "unavailable_files": self.unavailable_files,
            "complete": self.complete,
            "locations_truncated": self.locations_truncated,
            "locations": [location.to_wire() for location in self.locations],
        }


@dataclass(frozen=True, slots=True)
class ProjectSymbolRelation:
    kind: str
    path: str
    line: int
    column: int
    scope: str | None
    target: str
    symbol_kind: str | None
    in_test: bool
    evidence: ProjectSearchEvidence | None

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "scope": self.scope,
            "target": self.target,
            "symbol_kind": self.symbol_kind,
            "in_test": self.in_test,
            "evidence": None if self.evidence is None else self.evidence.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ProjectSymbolNavigation:
    needle: str
    precise_languages: tuple[str, ...]
    candidate_precise_files: int
    parsed_precise_files: int
    parse_failures: int
    parse_skipped_files: int
    matching_unsupported_files: int
    definition_count: int
    call_count: int
    test_call_count: int
    import_count: int
    inheritance_count: int
    precise_classification_complete: bool
    relations_truncated: bool
    evidence_truncated: bool
    relations: tuple[ProjectSymbolRelation, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "needle": self.needle,
            "precise_languages": list(self.precise_languages),
            "candidate_precise_files": self.candidate_precise_files,
            "parsed_precise_files": self.parsed_precise_files,
            "parse_failures": self.parse_failures,
            "parse_skipped_files": self.parse_skipped_files,
            "matching_unsupported_files": self.matching_unsupported_files,
            "definition_count": self.definition_count,
            "call_count": self.call_count,
            "test_call_count": self.test_call_count,
            "import_count": self.import_count,
            "inheritance_count": self.inheritance_count,
            "precise_classification_complete": self.precise_classification_complete,
            "relations_truncated": self.relations_truncated,
            "evidence_truncated": self.evidence_truncated,
            "relations": [relation.to_wire() for relation in self.relations],
        }


@dataclass(frozen=True, slots=True)
class ProjectExactSearchInspection:
    coverage: ProjectExactSearchCoverage | None
    symbol_navigation: ProjectSymbolNavigation | None


@dataclass(frozen=True, slots=True)
class ProjectSearchHit:
    ref: str
    kind: ProjectSearchKind
    title: str
    location: str
    short_summary: str | None
    match_reason: str
    freshness: str
    path: str | None = None
    evidence: ProjectSearchEvidence | None = None
    evidence_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectContextItem:
    ref: str
    kind: ProjectSearchKind
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class _RankedProjectHit:
    hit: ProjectSearchHit
    quality: int
    matched_terms: int
    lexical_score: float
    relevance_boost: int = 0


def search_project(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    *,
    scope: ProjectSearchScope = ProjectSearchScope.ALL,
    limit: int,
    response_reserve_bytes: int = 0,
) -> tuple[ProjectSearchHit, ...]:
    """Search bounded Project Intelligence while keeping filesystem search Workspace-local."""
    workspace = get_workspace(connection, workspace_id)
    project = get_project(connection, workspace.project_id)
    normalized = _normalize_query(query)
    analyzed = analyze_search_query(normalized)
    if not analyzed.terms:
        raise SearchError("project search query has no searchable tokens")
    _validate_limit(limit)
    if not isinstance(scope, ProjectSearchScope):
        raise SearchError("project search scope is unsupported")

    if scope is ProjectSearchScope.CODE:
        hits = _project_hits(
            _file_hits(
                connection,
                workspace_id,
                analyzed,
                IndexedPathSearchScope.CODE,
                limit,
            )
        )
    elif scope is ProjectSearchScope.DOCS:
        hits = _project_hits(
            _file_hits(
                connection,
                workspace_id,
                analyzed,
                IndexedPathSearchScope.DOCS,
                limit,
            )
        )
    elif scope is ProjectSearchScope.KNOWLEDGE:
        hits = _project_hits(
            _knowledge_hits(
                connection,
                project.project_id,
                analyzed,
                limit,
                active_workspace_id=workspace_id,
                include_unanchored_agent_asserted=True,
            )
        )
    elif scope is ProjectSearchScope.TASKS:
        hits = _project_hits(
            _task_hits(
                connection,
                analyzed,
                limit,
                project_id=project.project_id,
                active_workspace_id=workspace_id,
            )
        )
    else:
        channels = (
            _knowledge_hits(
                connection,
                project.project_id,
                analyzed,
                limit,
                active_workspace_id=workspace_id,
            ),
            _file_hits(
                connection,
                workspace_id,
                analyzed,
                IndexedPathSearchScope.CODE,
                limit,
            ),
            _file_hits(
                connection,
                workspace_id,
                analyzed,
                IndexedPathSearchScope.DOCS,
                limit,
            ),
            _task_hits(
                connection,
                analyzed,
                limit,
                project_id=project.project_id,
                active_workspace_id=workspace_id,
            ),
        )
        hits = _fuse_ranked_channels(channels, limit)
    return _attach_current_source_evidence(
        connection,
        workspace,
        analyzed,
        hits,
        response_reserve_bytes=response_reserve_bytes,
    )


def search_exact_source_inspection(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    *,
    scope: ProjectSearchScope,
) -> ProjectExactSearchInspection:
    """Inspect exhaustive exact source and precise syntax relations in one current-source pass."""
    if scope not in {ProjectSearchScope.ALL, ProjectSearchScope.CODE, ProjectSearchScope.DOCS}:
        return ProjectExactSearchInspection(None, None)
    needle = _exact_search_needle(query)
    if needle is None:
        return ProjectExactSearchInspection(None, None)
    needle_text, needle_kind = needle
    symbol_needle = (
        needle_text
        if scope is not ProjectSearchScope.DOCS
        and _is_identifier_exact_needle(needle_text)
        and needle_kind != "quoted_literal"
        else None
    )
    symbol_candidate_text = None if symbol_needle is None else symbol_needle.rsplit(".", 1)[-1]
    workspace = get_workspace(connection, workspace_id)
    rows = connection.execute(
        """
        SELECT
            files.relative_path,
            files.kind,
            files.size_bytes,
            files.content_sha256
        FROM indexed_files AS files
        WHERE files.workspace_id = ?
        ORDER BY files.relative_path
        """,
        (workspace_id,),
    ).fetchall()

    matched_files = 0
    matched_occurrences = 0
    matched_lines = 0
    scanned_files = 0
    scanned_bytes = 0
    non_text_files = 0
    unavailable_files = 0
    locations: list[ProjectExactSearchLocation] = []
    locations_truncated = False
    budget_exhausted = False

    precise_languages: set[str] = set()
    candidate_precise_files = 0
    parsed_precise_files = 0
    parse_failures = 0
    parse_skipped_files = 0
    matching_unsupported_files = 0
    syntax_relations: list[SyntaxRelation] = []

    for raw_path, raw_kind, raw_size, file_sha in rows:
        if (
            not isinstance(raw_path, str)
            or not isinstance(raw_kind, str)
            or isinstance(raw_size, bool)
            or not isinstance(raw_size, int)
            or raw_size < 0
            or not isinstance(file_sha, str)
        ):
            raise ProjectRetrievalError(
                "exact source coverage crossed invalid Structural Index state"
            )
        if raw_kind != IndexedFileKind.FILE.value or is_generated_text_output_path(raw_path):
            continue
        is_doc = is_document_path(raw_path)
        if scope is ProjectSearchScope.CODE and is_doc:
            continue
        if scope is ProjectSearchScope.DOCS and not is_doc:
            continue
        if budget_exhausted or scanned_bytes + raw_size > MAX_EXACT_SEARCH_SCAN_BYTES:
            budget_exhausted = True
            unavailable_files += 1
            continue
        read = read_current_exact_search_text(
            workspace,
            raw_path,
            expected_content_sha256=file_sha,
        )
        scanned_files += 1
        scanned_bytes += min(raw_size, MAX_EXACT_SEARCH_FILE_BYTES + 1)
        if read.status is ExactSearchReadStatus.NON_TEXT:
            non_text_files += 1
            continue
        if read.status is not ExactSearchReadStatus.OK or read.text is None:
            unavailable_files += 1
            continue
        file_occurrences = 0
        file_matched_lines = 0
        for line_number, line in enumerate(read.text.splitlines(), start=1):
            line_occurrences = _literal_columns(line, needle_text)
            if not line_occurrences:
                continue
            file_matched_lines += 1
            file_occurrences += len(line_occurrences)
            preview = _truncate_utf8(line.strip(), MAX_EXACT_SEARCH_PREVIEW_BYTES)
            for column in line_occurrences:
                if len(locations) < MAX_EXACT_SEARCH_LOCATIONS:
                    locations.append(
                        ProjectExactSearchLocation(
                            path=raw_path,
                            line=line_number,
                            column=column,
                            preview=preview,
                        )
                    )
                else:
                    locations_truncated = True
        if file_occurrences:
            matched_files += 1
            matched_occurrences += file_occurrences
            matched_lines += file_matched_lines
        if symbol_needle is not None and not is_doc:
            if (
                is_precise_symbol_path(raw_path)
                and symbol_candidate_text is not None
                and symbol_candidate_text in read.text
            ):
                candidate_precise_files += 1
                language = precise_symbol_language(raw_path)
                if language is None:
                    raise ProjectRetrievalError("precise symbol path has no parser language")
                precise_languages.add(language)
                analysis = analyze_precise_symbol_relations(
                    raw_path,
                    read.text,
                    symbol_needle,
                )
                if analysis.status == "ok":
                    parsed_precise_files += 1
                    syntax_relations.extend(analysis.relations)
                elif analysis.status == "too_large":
                    parse_skipped_files += 1
                else:
                    parse_failures += 1
            elif file_occurrences and not is_precise_symbol_path(raw_path):
                matching_unsupported_files += 1

    coverage = _fit_exact_coverage_to_budget(
        ProjectExactSearchCoverage(
            needle=needle_text,
            needle_kind=needle_kind,
            case_sensitive=True,
            matched_files=matched_files,
            matched_occurrences=matched_occurrences,
            matched_lines=matched_lines,
            scanned_files=scanned_files,
            scanned_bytes=scanned_bytes,
            non_text_files=non_text_files,
            unavailable_files=unavailable_files,
            complete=not budget_exhausted and unavailable_files == 0,
            locations_truncated=locations_truncated,
            locations=tuple(locations),
        )
    )
    navigation = None
    if symbol_needle is not None and (
        candidate_precise_files > 0 or matching_unsupported_files > 0
    ):
        navigation = _build_symbol_navigation(
            symbol_needle,
            syntax_relations,
            precise_languages=tuple(sorted(precise_languages)),
            candidate_precise_files=candidate_precise_files,
            parsed_precise_files=parsed_precise_files,
            parse_failures=parse_failures,
            parse_skipped_files=parse_skipped_files,
            matching_unsupported_files=matching_unsupported_files,
            source_scan_complete=(not budget_exhausted and unavailable_files == 0),
        )
    return ProjectExactSearchInspection(coverage, navigation)


def search_exact_source_coverage(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    *,
    scope: ProjectSearchScope,
) -> ProjectExactSearchCoverage | None:
    """Compatibility wrapper for callers that only need exact current-source coverage."""
    return search_exact_source_inspection(
        connection,
        workspace_id,
        query,
        scope=scope,
    ).coverage


def symbol_navigation_response_reserve(navigation: ProjectSymbolNavigation | None) -> int:
    if navigation is None:
        return 0
    encoded = json.dumps(
        project_symbol_navigation_payload(navigation),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) + 128


def exact_coverage_response_reserve(coverage: ProjectExactSearchCoverage | None) -> int:
    if coverage is None:
        return 0
    encoded = json.dumps(
        project_exact_search_coverage_payload(coverage),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(encoded) + 128


def search_tasks(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
) -> tuple[ProjectSearchHit, ...]:
    """Search durable Task history across every registered Project on this daemon."""
    normalized = _normalize_query(query)
    analyzed = analyze_search_query(normalized)
    if not analyzed.terms:
        raise SearchError("project search query has no searchable tokens")
    _validate_limit(limit)
    return _project_hits(_task_hits(connection, analyzed, limit))


def read_project_context(
    connection: sqlite3.Connection,
    workspace_id: str,
    refs: tuple[str, ...],
) -> tuple[ProjectContextItem, ...]:
    """Expand only explicitly selected refs and fail closed on cross-Project identities."""
    workspace = get_workspace(connection, workspace_id)
    project = get_project(connection, workspace.project_id)
    items: list[ProjectContextItem] = []
    for ref in refs:
        _validate_ref(ref)
        if ref.startswith("code:") or ref.startswith("doc:"):
            kind_text, _, relative_path = ref.partition(":")
            entry = get_indexed_file(connection, workspace_id, relative_path)
            if entry is None:
                raise ProjectRetrievalRefError("selected Structural Index ref is not current")
            is_doc = is_document_path(entry.relative_path)
            if (kind_text == "doc") != is_doc:
                raise ProjectRetrievalRefError(
                    "selected Structural Index ref kind does not match path"
                )
            kind = ProjectSearchKind.DOC if is_doc else ProjectSearchKind.CODE
            items.append(
                ProjectContextItem(
                    ref=ref,
                    kind=kind,
                    data={
                        "path": entry.relative_path,
                        "entry_kind": entry.kind.value,
                        "size_bytes": entry.size_bytes,
                    },
                )
            )
            continue
        if ref.startswith("knowledge:"):
            knowledge_id = ref.removeprefix("knowledge:")
            try:
                card = get_knowledge_card(connection, knowledge_id)
            except KnowledgeError as exc:
                raise ProjectRetrievalRefError("selected Knowledge ref does not exist") from exc
            if card.project_id != project.project_id:
                raise ProjectRetrievalRefError("selected Knowledge ref belongs to another Project")
            items.append(
                ProjectContextItem(
                    ref=ref,
                    kind=ProjectSearchKind.KNOWLEDGE,
                    data=_knowledge_context_data(card),
                )
            )
            continue
        if ref.startswith("task:"):
            items.append(_task_context(connection, project.project_id, ref))
            continue
        raise ProjectRetrievalRefError("selected Project context ref kind is unsupported")
    return tuple(items)


def _file_hits(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: AnalyzedSearchQuery,
    scope: IndexedPathSearchScope,
    limit: int,
) -> tuple[_RankedProjectHit, ...]:
    path_results = list(
        search_indexed_paths(
            connection,
            workspace_id,
            query.normalized,
            limit=MAX_SEARCH_LIMIT,
            scope=scope,
        )
    )
    effective_path_query = " ".join(query.terms)
    if effective_path_query.casefold() != query.normalized.casefold():
        seen_paths = {result.relative_path for result in path_results}
        for result in search_indexed_paths(
            connection,
            workspace_id,
            effective_path_query,
            limit=MAX_SEARCH_LIMIT,
            scope=scope,
        ):
            if result.relative_path not in seen_paths:
                path_results.append(result)
                seen_paths.add(result.relative_path)

    ranked_by_ref: dict[str, _RankedProjectHit] = {}
    path_quality = {
        "exact_path": _QUALITY_EXACT_PATH,
        "exact_filename": _QUALITY_EXACT_FILENAME,
        "identifier_tokens": _QUALITY_ALL_TERMS,
        "path_substring": _QUALITY_PARTIAL,
    }
    for result in path_results:
        is_doc = is_document_path(result.relative_path)
        kind = ProjectSearchKind.DOC if is_doc else ProjectSearchKind.CODE
        prefix = "doc" if is_doc else "code"
        ref = f"{prefix}:{result.relative_path}"
        ranked_by_ref[ref] = _RankedProjectHit(
            hit=ProjectSearchHit(
                ref=ref,
                kind=kind,
                title=Path(result.relative_path).name,
                location=result.relative_path,
                short_summary=None,
                match_reason=result.match_kind.value,
                freshness="indexed_snapshot",
                path=result.relative_path,
            ),
            quality=path_quality[result.match_kind.value],
            matched_terms=matching_term_count(query.terms, result.relative_path),
            lexical_score=float(path_quality[result.match_kind.value]),
            relevance_boost=_path_relevance_penalty(result.relative_path, query.terms),
        )

    for candidate in _indexed_content_hits(connection, workspace_id, query, scope, limit):
        previous = ranked_by_ref.get(candidate.hit.ref)
        if previous is None or _ranked_hit_key(candidate) < _ranked_hit_key(previous):
            ranked_by_ref[candidate.hit.ref] = candidate

    if scope is IndexedPathSearchScope.CODE:
        for candidate in _indexed_code_unit_hits(connection, workspace_id, query, limit):
            previous = ranked_by_ref.get(candidate.hit.ref)
            if previous is None or _ranked_hit_key(candidate) < _ranked_hit_key(previous):
                ranked_by_ref[candidate.hit.ref] = candidate

    ranked = sorted(ranked_by_ref.values(), key=_ranked_hit_key)
    return tuple(ranked[:limit])


def _indexed_code_unit_hits(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: AnalyzedSearchQuery,
    limit: int,
) -> tuple[_RankedProjectHit, ...]:
    candidate_limit = min(
        _FILE_CANDIDATE_LIMIT,
        max(24, limit * 8, len(query.terms) * 16),
    )
    rows = connection.execute(
        """
        SELECT
            units.relative_path,
            units.name,
            units.qualified_name,
            units.symbol_kind,
            units.line,
            bm25(indexed_code_unit_search, 8.0, 6.0, 5.0, 2.0),
            manifests.content_sha256,
            files.kind,
            files.content_sha256
        FROM indexed_code_unit_search
        JOIN indexed_code_units AS units
            ON units.id = indexed_code_unit_search.rowid
        JOIN indexed_code_unit_files AS manifests
            ON manifests.workspace_id = units.workspace_id
           AND manifests.relative_path = units.relative_path
        JOIN indexed_files AS files
            ON files.workspace_id = units.workspace_id
           AND files.relative_path = units.relative_path
        WHERE indexed_code_unit_search MATCH ?
          AND units.workspace_id = ?
          AND manifests.status = 'ok'
        ORDER BY bm25(indexed_code_unit_search, 8.0, 6.0, 5.0, 2.0),
                 units.relative_path, units.line, units.column
        LIMIT ?
        """,
        (query.all_fts_expression, workspace_id, candidate_limit),
    ).fetchall()
    ranked_by_ref: dict[str, _RankedProjectHit] = {}
    for row in rows:
        (
            relative_path,
            name,
            qualified_name,
            symbol_kind,
            line,
            raw_score,
            manifest_sha256,
            raw_kind,
            indexed_sha256,
        ) = row
        if (
            not isinstance(relative_path, str)
            or not isinstance(name, str)
            or not isinstance(qualified_name, str)
            or not isinstance(symbol_kind, str)
            or isinstance(line, bool)
            or not isinstance(line, int)
            or line <= 0
            or not isinstance(raw_score, (int, float))
            or not isinstance(manifest_sha256, str)
            or not isinstance(indexed_sha256, str)
            or raw_kind != IndexedFileKind.FILE.value
            or is_document_path(relative_path)
            or is_generated_text_output_path(relative_path)
        ):
            raise ProjectRetrievalError(
                "indexed code-unit search crossed authoritative index state"
            )
        if manifest_sha256 != indexed_sha256:
            continue
        phrase_match = contains_term_phrase(query.terms, name) or contains_term_phrase(
            query.terms, qualified_name
        )
        quality = _QUALITY_TITLE_OR_IDENTIFIER_PHRASE if phrase_match else _QUALITY_ALL_TERMS
        reason = (
            "code unit definition phrase" if phrase_match else "code unit definition (all terms)"
        )
        ref = f"code:{relative_path}"
        candidate = _RankedProjectHit(
            hit=ProjectSearchHit(
                ref=ref,
                kind=ProjectSearchKind.CODE,
                title=Path(relative_path).name,
                location=relative_path,
                short_summary=_truncate_utf8(
                    f"{symbol_kind} {qualified_name}",
                    _SUMMARY_MAX_BYTES,
                ),
                match_reason=reason,
                freshness="indexed_snapshot",
                path=relative_path,
            ),
            quality=quality,
            matched_terms=len(query.terms),
            lexical_score=float(raw_score),
            relevance_boost=_path_relevance_penalty(relative_path, query.terms) - 1,
        )
        previous = ranked_by_ref.get(ref)
        if previous is None or _ranked_hit_key(candidate) < _ranked_hit_key(previous):
            ranked_by_ref[ref] = candidate
    return tuple(sorted(ranked_by_ref.values(), key=_ranked_hit_key))


def _indexed_content_hits(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: AnalyzedSearchQuery,
    scope: IndexedPathSearchScope,
    limit: int,
) -> tuple[_RankedProjectHit, ...]:
    corpus = "docs" if scope is IndexedPathSearchScope.DOCS else "code"
    candidate_limit = min(
        _FILE_CANDIDATE_LIMIT,
        max(24, limit * 8, len(query.terms) * 16),
    )
    ranked_by_ref: dict[str, _RankedProjectHit] = {}
    rows = connection.execute(
        """
        SELECT
            documents.relative_path,
            documents.corpus,
            documents.content_sha256,
            documents.title,
            documents.path_tokens,
            documents.identifier_tokens,
            bm25(indexed_content_search, 8.0, 6.0, 5.0, 1.0),
            files.kind,
            files.size_bytes,
            files.content_sha256
        FROM indexed_content_search
        JOIN indexed_search_documents AS documents
            ON documents.id = indexed_content_search.rowid
        JOIN indexed_files AS files
            ON files.workspace_id = documents.workspace_id
           AND files.relative_path = documents.relative_path
        WHERE indexed_content_search MATCH ?
          AND documents.workspace_id = ?
          AND documents.corpus = ?
        ORDER BY bm25(indexed_content_search, 8.0, 6.0, 5.0, 1.0),
                 documents.relative_path
        LIMIT ?
        """,
        (query.all_fts_expression, workspace_id, corpus, candidate_limit),
    ).fetchall()
    for row in rows:
        candidate = _indexed_content_row(row, query, corpus)
        if candidate is None:
            continue
        previous = ranked_by_ref.get(candidate.hit.ref)
        if previous is None or _ranked_hit_key(candidate) < _ranked_hit_key(previous):
            ranked_by_ref[candidate.hit.ref] = candidate
    return tuple(sorted(ranked_by_ref.values(), key=_ranked_hit_key))


def _indexed_content_row(
    row: tuple[object, ...],
    query: AnalyzedSearchQuery,
    corpus: str,
) -> _RankedProjectHit | None:
    (
        relative_path,
        stored_corpus,
        document_sha256,
        title,
        path_tokens,
        normalized_identifiers,
        raw_score,
        raw_kind,
        size_bytes,
        indexed_sha256,
    ) = row
    if (
        not isinstance(relative_path, str)
        or stored_corpus != corpus
        or not isinstance(document_sha256, str)
        or not isinstance(title, str)
        or not isinstance(path_tokens, str)
        or not isinstance(normalized_identifiers, str)
        or not isinstance(raw_score, (int, float))
        or not isinstance(raw_kind, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or indexed_sha256 != document_sha256
        or is_document_path(relative_path) != (corpus == "docs")
    ):
        raise ProjectRetrievalError("indexed content search crossed authoritative index state")
    try:
        IndexedFileKind(raw_kind)
    except ValueError as exc:
        raise ProjectRetrievalError(
            "indexed content search returned an invalid entry kind"
        ) from exc
    if is_generated_text_output_path(relative_path):
        return None

    prefix = "doc" if corpus == "docs" else "code"
    kind = ProjectSearchKind.DOC if corpus == "docs" else ProjectSearchKind.CODE
    exact_filename_stem = analyze_search_query(Path(title).stem).terms == query.terms
    phrase_match = contains_term_phrase(query.terms, title) or contains_term_phrase(
        query.terms, normalized_identifiers
    )
    matched_terms = len(query.terms)
    if exact_filename_stem:
        quality = _QUALITY_EXACT_FILENAME_STEM
        reason = "exact filename stem"
    elif phrase_match:
        quality = _QUALITY_TITLE_OR_IDENTIFIER_PHRASE
        reason = "normalized identifier/title phrase"
    else:
        quality = _QUALITY_ALL_TERMS
        reason = "lexical content (all terms)"
    return _RankedProjectHit(
        hit=ProjectSearchHit(
            ref=f"{prefix}:{relative_path}",
            kind=kind,
            title=title,
            location=relative_path,
            short_summary=None,
            match_reason=reason,
            freshness="indexed_snapshot",
            path=relative_path,
        ),
        quality=quality,
        matched_terms=matched_terms,
        lexical_score=float(raw_score),
        relevance_boost=_path_relevance_penalty(relative_path, query.terms),
    )


def _knowledge_hits(
    connection: sqlite3.Connection,
    project_id: str,
    query: AnalyzedSearchQuery,
    limit: int,
    *,
    active_workspace_id: str,
    include_unanchored_agent_asserted: bool = False,
) -> tuple[_RankedProjectHit, ...]:
    candidate_limit = _candidate_limit(limit)
    rows = connection.execute(
        """
        SELECT knowledge_id, bm25(knowledge_search, 0.0, 0.0, 5.0, 1.0) AS score
        FROM knowledge_search
        WHERE knowledge_search MATCH ? AND project_id = ?
        ORDER BY score, knowledge_id
        LIMIT ?
        """,
        (query.fts_expression, project_id, candidate_limit),
    ).fetchall()
    ranked: list[_RankedProjectHit] = []
    seen: set[str] = set()
    for knowledge_id, raw_score in rows:
        if not isinstance(knowledge_id, str) or not isinstance(raw_score, (int, float)):
            raise ProjectRetrievalError("Knowledge search index returned invalid persisted types")
        if knowledge_id in seen:
            continue
        seen.add(knowledge_id)
        card = get_knowledge_card(connection, knowledge_id)
        if card.project_id != project_id:
            raise ProjectRetrievalError("Knowledge search index crossed Project ownership")
        if not _knowledge_applies_to_workspace(
            connection,
            card,
            active_workspace_id,
            include_unanchored_agent_asserted=include_unanchored_agent_asserted,
        ):
            continue
        stale = card.freshness is KnowledgeFreshness.NEEDS_REVALIDATION
        location = card.anchors[0].relative_path if card.anchors else f"project:{project_id[:12]}"
        anchor_text = " ".join(
            f"{anchor.relative_path} {anchor.symbol or ''}" for anchor in card.anchors
        )
        matched_terms = matching_term_count(query.terms, card.title, card.body, anchor_text)
        if contains_term_phrase(query.terms, card.title):
            quality = _QUALITY_TITLE_OR_IDENTIFIER_PHRASE
            match_reason = "Knowledge title phrase"
        elif matched_terms == len(query.terms):
            quality = _QUALITY_ALL_TERMS
            match_reason = "Knowledge title/body (all terms)"
        else:
            quality = _QUALITY_PARTIAL
            match_reason = "Knowledge title/body"
        if stale:
            quality += _QUALITY_STALE_OFFSET
        ranked.append(
            _RankedProjectHit(
                hit=ProjectSearchHit(
                    ref=f"knowledge:{card.knowledge_id}",
                    kind=ProjectSearchKind.KNOWLEDGE,
                    title=card.title,
                    location=location,
                    short_summary=_truncate_utf8(card.body, _SUMMARY_MAX_BYTES),
                    match_reason=match_reason,
                    freshness=card.freshness.value,
                ),
                quality=quality,
                matched_terms=matched_terms,
                lexical_score=float(raw_score),
            )
        )
    ranked.sort(key=_ranked_hit_key)
    return tuple(ranked[:limit])


def _knowledge_applies_to_workspace(
    connection: sqlite3.Connection,
    card: KnowledgeCardRecord,
    active_workspace_id: str,
    *,
    include_unanchored_agent_asserted: bool,
) -> bool:
    if card.source_type is not KnowledgeSourceType.AGENT_ASSERTED:
        return True
    if not card.anchors:
        return include_unanchored_agent_asserted
    anchors_match = True
    for anchor in card.anchors:
        indexed = get_indexed_file(connection, active_workspace_id, anchor.relative_path)
        if (
            indexed is None
            or indexed.kind.value != anchor.fingerprint_kind.value
            or indexed.content_sha256 != anchor.content_sha256
        ):
            anchors_match = False
            break
    if anchors_match:
        return True
    return include_unanchored_agent_asserted and all(
        anchor.workspace_id == active_workspace_id for anchor in card.anchors
    )


def _task_hits(
    connection: sqlite3.Connection,
    query: AnalyzedSearchQuery,
    limit: int,
    *,
    project_id: str | None = None,
    active_workspace_id: str | None = None,
) -> tuple[_RankedProjectHit, ...]:
    sql = """
        SELECT
            fragment_ref,
            task_id,
            workspace_id,
            project_id,
            title,
            body,
            bm25(task_search, 0.0, 0.0, 0.0, 0.0, 5.0, 1.0) AS score
        FROM task_search
        WHERE task_search MATCH ?
    """
    params: list[object] = [query.fts_expression]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY score, rowid LIMIT ?"
    params.append(_candidate_limit(limit))
    rows = connection.execute(sql, params).fetchall()
    current = (
        None if active_workspace_id is None else get_relevant_task(connection, active_workspace_id)
    )
    best: dict[str, tuple[str, str, str, float, int, int]] = {}
    for (
        fragment_ref,
        task_id,
        workspace_id,
        indexed_project_id,
        title,
        body,
        raw_score,
    ) in rows:
        if (
            not isinstance(fragment_ref, str)
            or not isinstance(task_id, str)
            or not isinstance(workspace_id, str)
            or not isinstance(indexed_project_id, str)
            or not isinstance(title, str)
            or not isinstance(body, str)
            or not isinstance(raw_score, (int, float))
        ):
            raise ProjectRetrievalError("Task search index returned invalid persisted types")
        matched_terms = matching_term_count(query.terms, title, body)
        if contains_term_phrase(query.terms, title):
            quality = _QUALITY_TITLE_OR_IDENTIFIER_PHRASE
        elif matched_terms == len(query.terms):
            quality = _QUALITY_ALL_TERMS
        else:
            quality = _QUALITY_PARTIAL
        candidate = (
            fragment_ref,
            workspace_id,
            indexed_project_id,
            float(raw_score),
            quality,
            matched_terms,
        )
        previous = best.get(task_id)
        if previous is None or (quality, -matched_terms, float(raw_score), fragment_ref) < (
            previous[4],
            -previous[5],
            previous[3],
            previous[0],
        ):
            best[task_id] = candidate

    ranked: list[_RankedProjectHit] = []
    for (
        task_id,
        (fragment_ref, workspace_id, indexed_project_id, score, quality, matched_terms),
    ) in best.items():
        task = get_task(connection, task_id)
        owner = get_workspace(connection, task.workspace_id)
        if owner.project_id != indexed_project_id or workspace_id != task.workspace_id:
            raise ProjectRetrievalError("Task search index crossed Project ownership")
        if project_id is not None and owner.project_id != project_id:
            raise ProjectRetrievalError("Task search index crossed Project ownership")
        result_ref, reason, summary, location = _task_fragment_projection(
            connection, task_id, fragment_ref
        )
        ranked.append(
            _RankedProjectHit(
                hit=ProjectSearchHit(
                    ref=result_ref,
                    kind=ProjectSearchKind.TASK,
                    title=task.title,
                    location=location,
                    short_summary=summary,
                    match_reason=reason,
                    freshness="durable_history",
                ),
                quality=quality,
                matched_terms=matched_terms,
                lexical_score=score,
                relevance_boost=(0 if current is not None and current.task_id == task_id else 1),
            )
        )
    ranked.sort(key=_ranked_hit_key)
    return tuple(ranked[:limit])


def _task_fragment_projection(
    connection: sqlite3.Connection,
    task_id: str,
    fragment_ref: str,
) -> tuple[str, str, str | None, str]:
    if fragment_ref == f"task:{task_id}":
        row = connection.execute(
            """
            SELECT summary
            FROM task_checkpoints
            WHERE task_id = ?
            ORDER BY task_revision DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        summary = None if row is None else _require_text(row[0], "Task checkpoint summary")
        return (
            f"task:{task_id}",
            "Task title",
            None if summary is None else _truncate_utf8(summary, _SUMMARY_MAX_BYTES),
            f"task:{task_id}",
        )
    if fragment_ref == f"meta:{task_id}":
        task = get_task(connection, task_id)
        values = [value for value in (task.jira_url, task.operator_status) if value is not None]
        summary = " · ".join(str(value) for value in values) or None
        return (
            f"task:{task_id}",
            "Task Jira link/operator status",
            summary,
            task.jira_url or f"task:{task_id}",
        )
    if fragment_ref == f"baseline:{task_id}":
        row = connection.execute(
            "SELECT branch FROM task_baselines WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None or (row[0] is not None and not isinstance(row[0], str)):
            raise ProjectRetrievalError("Task search baseline ownership mismatch")
        branch = row[0]
        return (
            f"task:{task_id}",
            "Task Git branch",
            branch,
            f"branch:{branch}" if branch is not None else f"task:{task_id}",
        )
    if fragment_ref.startswith("checkpoint:"):
        checkpoint_id = fragment_ref.removeprefix("checkpoint:")
        checkpoint = get_task_checkpoint(connection, checkpoint_id)
        if checkpoint.task_id != task_id:
            raise ProjectRetrievalError("Task search checkpoint ownership mismatch")
        return (
            f"task:{task_id}#checkpoint:{checkpoint_id}",
            "Task checkpoint summary/next step/branch",
            _truncate_utf8(checkpoint.summary, _SUMMARY_MAX_BYTES),
            f"task:{task_id} · revision {checkpoint.task_revision}",
        )
    if fragment_ref.startswith("event:"):
        event_id = _parse_positive_int(fragment_ref.removeprefix("event:"), "Task event id")
        row = connection.execute(
            """
            SELECT task_id, task_revision, event_type, operator_feedback,
                   operator_comment, jira_url, operator_status
            FROM task_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if (
            row is None
            or row[0] != task_id
            or row[2]
            not in {
                "operator_feedback",
                "operator_comment",
                "jira_link_updated",
                "operator_status_updated",
            }
        ):
            raise ProjectRetrievalError("Task search event ownership mismatch")
        event_type = _require_text(row[2], "Task event type")
        raw_summary = next((value for value in row[3:] if isinstance(value, str)), None)
        if raw_summary is None:
            raise ProjectRetrievalError("Task search event has no searchable payload")
        revision = row[1]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ProjectRetrievalError("Task search event revision is invalid")
        return (
            f"task:{task_id}#event:{event_id}",
            event_type.replace("_", " "),
            _truncate_utf8(raw_summary, _SUMMARY_MAX_BYTES),
            f"task:{task_id} · revision {revision}",
        )
    raise ProjectRetrievalError("Task search fragment ref is invalid")


def _task_context(
    connection: sqlite3.Connection,
    project_id: str,
    ref: str,
) -> ProjectContextItem:
    task_part, separator, fragment = ref.partition("#")
    task_id = task_part.removeprefix("task:")
    try:
        task = get_task(connection, task_id)
    except TaskNotFoundError as exc:
        raise ProjectRetrievalRefError("selected Task ref does not exist") from exc
    workspace = get_workspace(connection, task.workspace_id)
    if workspace.project_id != project_id:
        raise ProjectRetrievalRefError("selected Task ref belongs to another Project")

    stack_hints = get_task_stack_hints(connection, task.task_id)
    data: dict[str, object] = {
        "task_id": task.task_id,
        "workspace_id": task.workspace_id,
        "title": task.title,
        "state": task.state.value,
        "wait_reason": None if task.wait_reason is None else task.wait_reason.value,
        "jira_url": task.jira_url,
        "operator_status": (None if task.operator_status is None else task.operator_status.value),
        "revision": task.revision,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "stack_hints": list(stack_hints[:_CONTEXT_STACK_HINT_LIMIT]),
        "stack_hint_count": len(stack_hints),
        "stack_hints_truncated": len(stack_hints) > _CONTEXT_STACK_HINT_LIMIT,
    }
    if not separator:
        history: list[dict[str, object]] = []
        for event in list_task_events(connection, task.task_id, limit=_CONTEXT_HISTORY_LIMIT):
            item: dict[str, object] = {
                "event_type": event.event_type.value,
                "task_revision": event.task_revision,
                "created_at": event.created_at,
            }
            if event.checkpoint_id is not None:
                try:
                    checkpoint = get_task_checkpoint(connection, event.checkpoint_id)
                except TaskCheckpointError as exc:
                    raise ProjectRetrievalRefError(
                        "selected Task history checkpoint does not exist"
                    ) from exc
                item["summary"] = _truncate_utf8(checkpoint.summary, _CONTEXT_TEXT_MAX_BYTES)
                item["next_step"] = (
                    None
                    if checkpoint.next_step is None
                    else _truncate_utf8(checkpoint.next_step, _CONTEXT_TEXT_MAX_BYTES)
                )
                verification = list_checkpoint_verification(connection, checkpoint.checkpoint_id)
                item["verification"] = [
                    {"name": record.name, "status": record.status.value}
                    for record in verification[:_CONTEXT_VERIFICATION_LIMIT]
                ]
                item["verification_count"] = len(verification)
                item["verification_truncated"] = len(verification) > _CONTEXT_VERIFICATION_LIMIT
            if event.operator_feedback is not None:
                item["operator_feedback"] = _truncate_utf8(
                    event.operator_feedback, _CONTEXT_TEXT_MAX_BYTES
                )
            if event.operator_comment is not None:
                item["operator_comment"] = _truncate_utf8(
                    event.operator_comment, _CONTEXT_TEXT_MAX_BYTES
                )
            if event.jira_url is not None:
                item["jira_url"] = _truncate_utf8(event.jira_url, _CONTEXT_TEXT_MAX_BYTES)
            if event.operator_status is not None:
                item["operator_status"] = event.operator_status.value
            history.append(item)
        data["recent_history"] = history
    elif fragment.startswith("checkpoint:"):
        checkpoint_id = fragment.removeprefix("checkpoint:")
        try:
            checkpoint = get_task_checkpoint(connection, checkpoint_id)
        except TaskCheckpointError as exc:
            raise ProjectRetrievalRefError("selected Task checkpoint ref does not exist") from exc
        if checkpoint.task_id != task.task_id:
            raise ProjectRetrievalRefError("selected Task checkpoint ref does not belong to Task")
        changed_paths = _bounded_paths(checkpoint.changed_paths)
        summary = _truncate_utf8(checkpoint.summary, _CONTEXT_TEXT_MAX_BYTES)
        next_step = (
            None
            if checkpoint.next_step is None
            else _truncate_utf8(checkpoint.next_step, _CONTEXT_TEXT_MAX_BYTES)
        )
        verification_records = list_checkpoint_verification(connection, checkpoint.checkpoint_id)
        data["selected_checkpoint"] = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "task_revision": checkpoint.task_revision,
            "state": checkpoint.state.value,
            "wait_reason": None if checkpoint.wait_reason is None else checkpoint.wait_reason.value,
            "summary": summary,
            "summary_truncated": summary != checkpoint.summary,
            "next_step": next_step,
            "next_step_truncated": next_step != checkpoint.next_step,
            "created_at": checkpoint.created_at,
            "changed_paths": list(changed_paths),
            "changed_path_count": len(checkpoint.changed_paths),
            "changed_paths_truncated": len(changed_paths) < len(checkpoint.changed_paths),
            "verification": [
                {
                    "name": record.name,
                    "status": record.status.value,
                    "evidence": _truncate_utf8(
                        record.evidence, _CONTEXT_VERIFICATION_EVIDENCE_MAX_BYTES
                    ),
                    "evidence_truncated": (
                        len(record.evidence.encode("utf-8"))
                        > _CONTEXT_VERIFICATION_EVIDENCE_MAX_BYTES
                    ),
                    "source": record.source.value,
                }
                for record in verification_records[:_CONTEXT_VERIFICATION_LIMIT]
            ],
            "verification_count": len(verification_records),
            "verification_truncated": len(verification_records) > _CONTEXT_VERIFICATION_LIMIT,
        }
    elif fragment.startswith("event:"):
        event_id = _parse_positive_int(fragment.removeprefix("event:"), "selected Task event id")
        row = connection.execute(
            """
            SELECT task_id, task_revision, event_type, operator_feedback,
                   operator_comment, jira_url, operator_status, created_at
            FROM task_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None or row[0] != task.task_id:
            raise ProjectRetrievalRefError("selected Task event ref does not belong to Task")
        event_type = _require_text(row[2], "selected Task event type")
        payload_names = (
            ("operator_feedback", row[3]),
            ("operator_comment", row[4]),
            ("jira_url", row[5]),
            ("operator_status", row[6]),
        )
        payload = next(
            ((name, value) for name, value in payload_names if isinstance(value, str)),
            None,
        )
        if (
            event_type
            not in {
                "operator_feedback",
                "operator_comment",
                "jira_link_updated",
                "operator_status_updated",
            }
            or payload is None
        ):
            raise ProjectRetrievalRefError("selected Task event ref is not searchable history")
        payload_name, payload_value = payload
        selected_event: dict[str, object] = {
            "event_id": event_id,
            "task_revision": row[1],
            "event_type": event_type,
            payload_name: _truncate_utf8(payload_value, _CONTEXT_TEXT_MAX_BYTES),
            f"{payload_name}_truncated": (
                len(payload_value.encode("utf-8")) > _CONTEXT_TEXT_MAX_BYTES
            ),
            "created_at": row[7],
        }
        data["selected_event"] = selected_event
    else:
        raise ProjectRetrievalRefError("selected Task fragment ref is unsupported")
    return ProjectContextItem(ref=ref, kind=ProjectSearchKind.TASK, data=data)


def _knowledge_context_data(card: KnowledgeCardRecord) -> dict[str, object]:
    body = _truncate_utf8(card.body, _CONTEXT_TEXT_MAX_BYTES)
    anchors: list[dict[str, object]] = []
    anchor_bytes = 0
    for anchor in card.anchors[:_CONTEXT_KNOWLEDGE_ANCHOR_LIMIT]:
        fields = (anchor.workspace_id, anchor.relative_path, anchor.symbol or "")
        size = sum(len(value.encode("utf-8")) for value in fields)
        if anchor_bytes + size > _CONTEXT_KNOWLEDGE_ANCHOR_BYTES:
            break
        anchors.append(
            {
                "workspace_id": anchor.workspace_id,
                "path": anchor.relative_path,
                "symbol": anchor.symbol,
            }
        )
        anchor_bytes += size
    return {
        "knowledge_id": card.knowledge_id,
        "knowledge_kind": card.kind.value,
        "title": card.title,
        "body": body,
        "body_truncated": body != card.body,
        "freshness": card.freshness.value,
        "historical_clue": card.freshness is KnowledgeFreshness.NEEDS_REVALIDATION,
        "source_type": card.source_type.value,
        "source_task_id": card.source_task_id,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        "anchors": anchors,
        "anchor_count": len(card.anchors),
        "anchors_truncated": len(anchors) < len(card.anchors),
    }


def _bounded_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    bounded: list[str] = []
    used_bytes = 0
    for path in paths[:_CONTEXT_CHANGED_PATH_LIMIT]:
        size = len(path.encode("utf-8"))
        if used_bytes + size > _CONTEXT_CHANGED_PATH_BYTES:
            break
        bounded.append(path)
        used_bytes += size
    return tuple(bounded)


def _normalize_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise SearchError("project search query must be non-empty text")
    normalized = query.strip()
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SearchError("project search query must be valid UTF-8 text") from exc
    if size > MAX_SEARCH_QUERY_BYTES:
        raise SearchError(f"project search query exceeds {MAX_SEARCH_QUERY_BYTES} UTF-8 bytes")
    return normalized


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise SearchError(f"project search limit must be between 1 and {MAX_SEARCH_LIMIT}")


def _ranked_hit_key(item: _RankedProjectHit) -> tuple[int, int, int, float, str]:
    return (
        item.quality,
        -item.matched_terms,
        item.relevance_boost,
        item.lexical_score,
        item.hit.ref,
    )


def _path_relevance_penalty(relative_path: str, query_terms: tuple[str, ...]) -> int:
    lowered = relative_path.casefold()
    name = lowered.rsplit("/", 1)[-1]
    parts = lowered.split("/")
    penalty = 0
    if not any(term in {"test", "tests", "testing"} for term in query_terms):
        penalty += int(
            "tests" in parts
            or "test" in parts
            or name.startswith(("test_", "test-"))
            or name.endswith(("_test.py", "-test.py", ".test.js", ".test.ts"))
        )
    if not any(term.startswith(("archiv", "архив")) for term in query_terms):
        penalty += int(any(part in {"archive", "archives", "archived"} for part in parts[:-1]))
    return penalty


def _attach_current_source_evidence(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    query: AnalyzedSearchQuery,
    hits: tuple[ProjectSearchHit, ...],
    *,
    response_reserve_bytes: int = 0,
) -> tuple[ProjectSearchHit, ...]:
    annotated: list[ProjectSearchHit] = []
    reread_budget = MAX_SEARCH_EVIDENCE_HITS
    for hit in hits:
        if hit.kind not in {ProjectSearchKind.CODE, ProjectSearchKind.DOC} or hit.path is None:
            annotated.append(hit)
            continue
        indexed_sha = _indexed_content_sha256(connection, workspace.workspace_id, hit.path)
        if indexed_sha is None:
            annotated.append(replace(hit, evidence=None, evidence_reason=EVIDENCE_REASON_PATH_ONLY))
            continue
        if reread_budget <= 0:
            annotated.append(
                replace(hit, evidence=None, evidence_reason=EVIDENCE_REASON_RESPONSE_BUDGET)
            )
            continue
        reread_budget -= 1
        read = read_current_search_text(
            workspace,
            hit.path,
            expected_content_sha256=indexed_sha,
        )
        if read.status is SearchEvidenceReadStatus.CHANGED_SINCE_INDEX:
            annotated.append(
                replace(
                    hit,
                    short_summary=None,
                    evidence=None,
                    evidence_reason=EVIDENCE_REASON_CHANGED_SINCE_INDEX,
                )
            )
            continue
        if read.status is not SearchEvidenceReadStatus.OK or read.text is None:
            annotated.append(
                replace(hit, evidence=None, evidence_reason=EVIDENCE_REASON_NOT_RELOCATED)
            )
            continue
        evidence = _relocate_search_evidence(read.text, query.terms)
        if evidence is None:
            annotated.append(
                replace(hit, evidence=None, evidence_reason=EVIDENCE_REASON_NOT_RELOCATED)
            )
            continue
        annotated.append(replace(hit, evidence=evidence, evidence_reason=None))
    return _fit_search_hits_to_response_budget(
        annotated,
        query.normalized,
        response_reserve_bytes=response_reserve_bytes,
    )


def _indexed_content_sha256(
    connection: sqlite3.Connection,
    workspace_id: str,
    relative_path: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT documents.content_sha256, files.content_sha256, files.kind
        FROM indexed_search_documents AS documents
        JOIN indexed_files AS files
          ON files.workspace_id = documents.workspace_id
         AND files.relative_path = documents.relative_path
        WHERE documents.workspace_id = ? AND documents.relative_path = ?
        """,
        (workspace_id, relative_path),
    ).fetchone()
    if row is None:
        return None
    document_sha256, file_sha256, raw_kind = row
    if (
        not isinstance(document_sha256, str)
        or not isinstance(file_sha256, str)
        or document_sha256 != file_sha256
        or not isinstance(raw_kind, str)
    ):
        raise ProjectRetrievalError("indexed content search crossed authoritative index state")
    try:
        kind = IndexedFileKind(raw_kind)
    except ValueError as exc:
        raise ProjectRetrievalError(
            "indexed content search returned an invalid entry kind"
        ) from exc
    if kind is not IndexedFileKind.FILE:
        return None
    return document_sha256


def _relocate_search_evidence(text: str, terms: tuple[str, ...]) -> ProjectSearchEvidence | None:
    present_terms = tuple(term for term in terms if matching_term_count((term,), text) == 1)
    if not present_terms:
        return None
    lines = text.splitlines()
    if not lines:
        return None

    line_terms = tuple(
        frozenset(term for term in present_terms if matching_term_count((term,), line) == 1)
        for line in lines
    )
    counts = {term: 0 for term in present_terms}
    covered = 0
    left = 0
    best: tuple[int, int, int] | None = None
    for right, matched in enumerate(line_terms):
        for term in matched:
            if counts[term] == 0:
                covered += 1
            counts[term] += 1
        while covered == len(present_terms) and left <= right:
            width = right - left + 1
            if width <= MAX_SEARCH_EVIDENCE_SNIPPET_LINES:
                candidate = (width, left, right)
                if best is None or candidate < best:
                    best = candidate
            for term in line_terms[left]:
                counts[term] -= 1
                if counts[term] == 0:
                    covered -= 1
            left += 1
    if best is None:
        return None

    _, match_start, match_end = best
    start = match_start
    end = match_end
    while end - start + 1 < MAX_SEARCH_EVIDENCE_SNIPPET_LINES:
        grew = False
        if start > 0:
            expanded = "\n".join(lines[start - 1 : end + 1])
            if len(expanded.encode("utf-8")) <= MAX_SEARCH_EVIDENCE_SNIPPET_BYTES:
                start -= 1
                grew = True
        if end - start + 1 >= MAX_SEARCH_EVIDENCE_SNIPPET_LINES:
            break
        if end + 1 < len(lines):
            expanded = "\n".join(lines[start : end + 2])
            if len(expanded.encode("utf-8")) <= MAX_SEARCH_EVIDENCE_SNIPPET_BYTES:
                end += 1
                grew = True
        if not grew:
            break

    snippet = "\n".join(lines[start : end + 1])
    truncated = start > 0 or end < len(lines) - 1
    if len(snippet.encode("utf-8")) > MAX_SEARCH_EVIDENCE_SNIPPET_BYTES:
        snippet = _truncate_utf8(snippet, MAX_SEARCH_EVIDENCE_SNIPPET_BYTES)
        truncated = True
        if matching_term_count(present_terms, snippet) < len(present_terms):
            return None
    return ProjectSearchEvidence(
        start_line=start + 1,
        end_line=end + 1,
        snippet=snippet,
        truncated=truncated,
    )


def _fit_search_hits_to_response_budget(
    hits: list[ProjectSearchHit],
    query: str,
    *,
    response_reserve_bytes: int = 0,
) -> tuple[ProjectSearchHit, ...]:
    fitted = list(hits)
    while True:
        encoded = json.dumps(
            {
                "query": query,
                "scope": "all",
                "results": [project_search_hit_payload(hit) for hit in fitted],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if (
            len(encoded) + _SEARCH_EVIDENCE_ENVELOPE_RESERVE_BYTES + response_reserve_bytes
            <= PROJECT_SEARCH_MAX_BYTES
        ):
            return tuple(fitted)
        trimmed = False
        for index in range(len(fitted) - 1, -1, -1):
            hit = fitted[index]
            if hit.evidence is None:
                continue
            fitted[index] = replace(
                hit, evidence=None, evidence_reason=EVIDENCE_REASON_RESPONSE_BUDGET
            )
            trimmed = True
            break
        if not trimmed:
            return tuple(fitted)


def project_exact_search_coverage_payload(
    coverage: ProjectExactSearchCoverage,
) -> dict[str, object]:
    return coverage.to_wire()


def project_symbol_navigation_payload(
    navigation: ProjectSymbolNavigation,
) -> dict[str, object]:
    return navigation.to_wire()


def _build_symbol_navigation(
    needle: str,
    syntax_relations: list[SyntaxRelation],
    *,
    precise_languages: tuple[str, ...],
    candidate_precise_files: int,
    parsed_precise_files: int,
    parse_failures: int,
    parse_skipped_files: int,
    matching_unsupported_files: int,
    source_scan_complete: bool,
) -> ProjectSymbolNavigation:
    relations = tuple(
        sorted(
            (_project_symbol_relation(relation) for relation in syntax_relations),
            key=_project_symbol_relation_key,
        )
    )
    definition_count = sum(relation.kind == "definition" for relation in relations)
    call_count = sum(relation.kind == "call" for relation in relations)
    test_call_count = sum(relation.kind == "call" and relation.in_test for relation in relations)
    import_count = sum(relation.kind == "import" for relation in relations)
    inheritance_count = sum(relation.kind == "inheritance" for relation in relations)
    truncated = len(relations) > MAX_SYMBOL_NAVIGATION_RELATIONS
    navigation = ProjectSymbolNavigation(
        needle=needle,
        precise_languages=precise_languages,
        candidate_precise_files=candidate_precise_files,
        parsed_precise_files=parsed_precise_files,
        parse_failures=parse_failures,
        parse_skipped_files=parse_skipped_files,
        matching_unsupported_files=matching_unsupported_files,
        definition_count=definition_count,
        call_count=call_count,
        test_call_count=test_call_count,
        import_count=import_count,
        inheritance_count=inheritance_count,
        precise_classification_complete=(
            source_scan_complete
            and candidate_precise_files == parsed_precise_files
            and parse_failures == 0
            and parse_skipped_files == 0
            and matching_unsupported_files == 0
        ),
        relations_truncated=truncated,
        evidence_truncated=False,
        relations=relations[:MAX_SYMBOL_NAVIGATION_RELATIONS],
    )
    return _fit_symbol_navigation_to_budget(navigation)


def _project_symbol_relation_key(
    relation: ProjectSymbolRelation,
) -> tuple[int, str, int, int, str, str]:
    priority = {
        "definition": 0,
        "call": 2 if relation.in_test else 1,
        "inheritance": 3,
        "import": 4,
    }
    return (
        priority.get(relation.kind, 9),
        relation.path,
        relation.line,
        relation.column,
        relation.scope or "",
        relation.target,
    )


def _project_symbol_relation(relation: SyntaxRelation) -> ProjectSymbolRelation:
    evidence = relation.evidence
    return ProjectSymbolRelation(
        kind=relation.kind,
        path=relation.path,
        line=relation.line,
        column=relation.column,
        scope=(
            None
            if relation.scope is None
            else _truncate_utf8(relation.scope, MAX_SYMBOL_RELATION_TEXT_BYTES)
        ),
        target=_truncate_utf8(relation.target, MAX_SYMBOL_RELATION_TEXT_BYTES),
        symbol_kind=relation.symbol_kind,
        in_test=relation.in_test,
        evidence=ProjectSearchEvidence(
            start_line=evidence.start_line,
            end_line=evidence.end_line,
            snippet=evidence.snippet,
            truncated=evidence.truncated,
        ),
    )


def _fit_symbol_navigation_to_budget(
    navigation: ProjectSymbolNavigation,
) -> ProjectSymbolNavigation:
    fitted = navigation
    while True:
        encoded = json.dumps(
            project_symbol_navigation_payload(fitted),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= MAX_SYMBOL_NAVIGATION_BYTES:
            return fitted
        relations = list(fitted.relations)
        evidence_index = next(
            (
                index
                for index in range(len(relations) - 1, -1, -1)
                if relations[index].evidence is not None
            ),
            None,
        )
        if evidence_index is not None:
            relations[evidence_index] = replace(relations[evidence_index], evidence=None)
            fitted = replace(
                fitted,
                evidence_truncated=True,
                relations=tuple(relations),
            )
            continue
        if relations:
            fitted = replace(
                fitted,
                relations_truncated=True,
                relations=tuple(relations[:-1]),
            )
            continue
        return fitted


def _is_identifier_exact_needle(value: str) -> bool:
    if (
        re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
            value,
        )
        is None
    ):
        return False
    return not keyword.iskeyword(value.rsplit(".", 1)[-1])


def _exact_search_needle(query: str) -> tuple[str, str] | None:
    normalized = query.strip()
    for match in re.finditer(r"`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'", normalized):
        candidate = next(value for value in match.groups() if value is not None).strip()
        if _valid_exact_needle(candidate):
            return candidate, "quoted_literal"

    raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", normalized)
    for candidate in raw_tokens:
        if (
            "_" in candidate
            or "." in candidate
            or any(left.islower() and right.isupper() for left, right in pairwise(candidate))
        ) and _valid_exact_needle(candidate):
            return candidate, "identifier"

    if not any(character.isspace() for character in normalized) and _valid_exact_needle(normalized):
        return normalized, "single_term"
    return None


def _valid_exact_needle(value: str) -> bool:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_EXACT_SEARCH_NEEDLE_BYTES
    except UnicodeEncodeError:
        return False


def _literal_columns(line: str, needle: str) -> tuple[int, ...]:
    columns: list[int] = []
    start = 0
    while True:
        index = line.find(needle, start)
        if index < 0:
            return tuple(columns)
        columns.append(index + 1)
        start = index + max(1, len(needle))


def _fit_exact_coverage_to_budget(
    coverage: ProjectExactSearchCoverage,
) -> ProjectExactSearchCoverage:
    fitted = coverage
    while fitted.locations:
        encoded = json.dumps(
            project_exact_search_coverage_payload(fitted),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= MAX_EXACT_SEARCH_COVERAGE_BYTES:
            return fitted
        fitted = replace(
            fitted,
            locations=fitted.locations[:-1],
            locations_truncated=True,
        )
    return fitted


def project_search_hit_payload(hit: ProjectSearchHit) -> dict[str, object]:
    payload: dict[str, object] = {
        "ref": hit.ref,
        "kind": hit.kind.value,
        "title": hit.title,
        "location": hit.location,
        "short_summary": hit.short_summary,
        "match_reason": hit.match_reason,
        "freshness": hit.freshness,
        "evidence": None if hit.evidence is None else hit.evidence.to_wire(),
        "evidence_reason": hit.evidence_reason,
    }
    if hit.path is not None:
        payload["path"] = hit.path
    return payload


def _project_hits(items: tuple[_RankedProjectHit, ...]) -> tuple[ProjectSearchHit, ...]:
    return tuple(item.hit for item in items)


def _fuse_ranked_channels(
    channels: tuple[tuple[_RankedProjectHit, ...], ...],
    limit: int,
) -> tuple[ProjectSearchHit, ...]:
    """Fuse comparable match-quality tiers, then interleave uncalibrated channel ranks."""
    fused: list[tuple[int, int, int, int, str, ProjectSearchHit]] = []
    for channel_index, channel in enumerate(channels):
        for channel_rank, item in enumerate(channel):
            fused.append(
                (
                    item.quality,
                    -item.matched_terms,
                    channel_rank,
                    channel_index,
                    item.hit.ref,
                    item.hit,
                )
            )
    fused.sort(key=lambda item: item[:-1])
    return tuple(item[-1] for item in fused[:limit])


def _candidate_limit(limit: int) -> int:
    return min(_MAX_CANDIDATE_LIMIT, max(_DEFAULT_CANDIDATE_LIMIT, limit * 24))


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    payload = value.encode("utf-8")
    if len(payload) <= maximum_bytes:
        return value
    truncated = payload[: max(0, maximum_bytes - 3)]
    while True:
        try:
            return truncated.decode("utf-8") + "..."
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def _validate_ref(ref: str) -> None:
    if not isinstance(ref, str) or not ref or "\x00" in ref:
        raise ProjectRetrievalRefError("Project context ref must be non-empty text")
    try:
        size = len(ref.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProjectRetrievalRefError("Project context ref must be valid UTF-8") from exc
    if size > MAX_PROJECT_CONTEXT_REF_BYTES:
        raise ProjectRetrievalRefError("Project context ref exceeds byte limit")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectRetrievalError(f"{label} has invalid persisted text")
    return value


def _parse_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ProjectRetrievalRefError(f"{label} is invalid") from exc
    if parsed <= 0:
        raise ProjectRetrievalRefError(f"{label} is invalid")
    return parsed
