from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from harness.git_workspace import _git_environment, inspect_git_workspace
from harness.knowledge import (
    reconcile_knowledge_staleness,
    snapshot_fresh_anchored_knowledge_ids,
)
from harness.registry import WorkspaceRecord, get_workspace
from harness.search_text import (
    identifier_expansion,
    identifier_tokens,
    is_document_path,
    is_generated_text_output_path,
)
from harness.symbol_navigation import (
    MAX_SYMBOL_PARSE_BYTES,
    SyntaxRelation,
    analyze_precise_code_units,
    precise_symbol_language,
)

_DEFAULT_EXCLUDES = (
    "node_modules/",
    "vendor/",
    "dist/",
    "build/",
    "target/",
    "caches/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
)
_HASH_CHUNK_BYTES = 128 * 1024
MAX_INDEXED_SEARCH_BODY_BYTES = 1024 * 1024
MAX_EXACT_SEARCH_FILE_BYTES = 8 * 1024 * 1024
MAX_INDEXED_IDENTIFIER_TOKENS_BYTES = 256 * 1024
MAX_INDEXED_CODE_UNITS_PER_FILE = 4096
MAX_INDEXED_CODE_UNIT_NAME_BYTES = 512
MAX_INDEXED_CODE_UNIT_QUALIFIED_NAME_BYTES = 1024
MAX_INCREMENTAL_SCAN_PATHS = 256


class IndexingError(RuntimeError):
    """Base class for deterministic Structural Index failures."""


class WorkspaceIndexMismatchError(IndexingError):
    """Raised when the registered Workspace no longer matches its Git worktree identity."""


class ScanDeadlineExceededError(IndexingError):
    """Raised when a bounded daemon-backed scan exceeds its execution deadline."""


class IndexedFileKind(StrEnum):
    """Filesystem entry kinds stored by the first Structural Index slice."""

    FILE = "file"
    SYMLINK = "symlink"


class IndexReconcileKind(StrEnum):
    """Kind of last successful Structural Index reconciliation."""

    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True, slots=True)
class IndexedFileRecord:
    """One rebuildable filesystem entry in the Workspace Structural Index."""

    workspace_id: str
    relative_path: str
    kind: IndexedFileKind
    size_bytes: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class IndexedSearchDocument:
    """One bounded local text projection used only by the rebuildable lexical index."""

    relative_path: str
    corpus: str
    content_sha256: str
    title: str
    path_tokens: str
    identifier_tokens: str
    body: str


@dataclass(frozen=True, slots=True)
class IndexFreshnessInspection:
    """Read-only comparison of persisted and live deterministic Workspace inventory."""

    workspace_id: str
    persisted_file_count: int
    live_file_count: int
    is_fresh: bool


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Compact reconciliation result for one deterministic Workspace scan."""

    workspace_id: str
    file_count: int
    added: int
    updated: int
    removed: int


def inspect_workspace_index_freshness(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    deadline: float | None = None,
) -> IndexFreshnessInspection:
    """Compare persisted and live deterministic inventory without mutating derived state."""
    _require_scan_deadline(deadline)
    workspace = get_workspace(connection, workspace_id)
    _require_registered_layout(workspace, deadline=deadline)
    persisted = list_indexed_files(connection, workspace_id)
    live_by_path = _build_snapshot(workspace, deadline=deadline)
    live = tuple(live_by_path[path] for path in sorted(live_by_path))
    _require_registered_layout(workspace, deadline=deadline)
    if get_workspace(connection, workspace_id) != workspace:
        raise IndexingError("Workspace registry identity changed during index inspection")
    if list_indexed_files(connection, workspace_id) != persisted:
        raise IndexingError("Workspace Structural Index changed during freshness inspection")
    return IndexFreshnessInspection(
        workspace_id=workspace_id,
        persisted_file_count=len(persisted),
        live_file_count=len(live),
        is_fresh=live == persisted,
    )


def scan_workspace(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    deadline: float | None = None,
) -> ScanResult:
    """Reconcile the rebuildable file inventory for one registered Workspace."""
    _require_scan_deadline(deadline)
    workspace = get_workspace(connection, workspace_id)
    _require_registered_layout(workspace, deadline=deadline)
    eligible_knowledge_ids = snapshot_fresh_anchored_knowledge_ids(connection, workspace_id)
    snapshot = _build_snapshot(workspace, deadline=deadline)
    _require_scan_deadline(deadline)
    _require_registered_layout(workspace, deadline=deadline)

    return _persist_snapshot(
        connection,
        workspace,
        snapshot,
        eligible_knowledge_ids=eligible_knowledge_ids,
        deadline=deadline,
        kind=IndexReconcileKind.FULL,
    )


def scan_workspace_paths(
    connection: sqlite3.Connection,
    workspace_id: str,
    relative_paths: Sequence[str],
    *,
    deadline: float | None = None,
) -> ScanResult:
    """Reconcile a bounded set of Git-observed paths without hashing the whole Workspace."""
    _require_scan_deadline(deadline)
    selected_paths = _normalize_incremental_paths(relative_paths)
    workspace = get_workspace(connection, workspace_id)
    _require_registered_layout(workspace, deadline=deadline)
    eligible_knowledge_ids = snapshot_fresh_anchored_knowledge_ids(connection, workspace_id)
    existing = {
        record.relative_path: record for record in list_indexed_files(connection, workspace_id)
    }
    harnessignore_rules = _read_harnessignore_rules(workspace.workspace_root)
    candidates = _candidate_paths(
        workspace.workspace_root,
        harnessignore_rules,
        deadline=deadline,
        pathspecs=selected_paths,
    )
    changed_snapshot: dict[str, IndexedFileRecord] = {}
    for relative_path in candidates:
        _require_scan_deadline(deadline)
        record = _inspect_entry(workspace, relative_path, deadline=deadline)
        if record is not None:
            changed_snapshot[relative_path] = record
    if _read_harnessignore_rules(workspace.workspace_root) != harnessignore_rules:
        raise IndexingError("Workspace changed while scanning: .harnessignore")
    _require_registered_layout(workspace, deadline=deadline)

    snapshot = dict(existing)
    for relative_path in selected_paths:
        snapshot.pop(relative_path, None)
    snapshot.update(changed_snapshot)
    return _persist_snapshot(
        connection,
        workspace,
        snapshot,
        eligible_knowledge_ids=eligible_knowledge_ids,
        deadline=deadline,
        kind=IndexReconcileKind.INCREMENTAL,
        expected_existing=existing,
    )


def _persist_snapshot(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    snapshot: dict[str, IndexedFileRecord],
    *,
    eligible_knowledge_ids: frozenset[str],
    deadline: float | None,
    kind: IndexReconcileKind,
    expected_existing: dict[str, IndexedFileRecord] | None = None,
) -> ScanResult:
    workspace_id = workspace.workspace_id
    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_scan_deadline(deadline)
        current_workspace = get_workspace(connection, workspace_id)
        if current_workspace != workspace:
            raise WorkspaceIndexMismatchError("workspace registry identity changed during scan")

        existing = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        if expected_existing is not None and existing != expected_existing:
            raise IndexingError("Workspace Structural Index changed during incremental scan")
        added = 0
        updated = 0
        for relative_path, record in snapshot.items():
            _require_scan_deadline(deadline)
            prior = existing.get(relative_path)
            if prior == record:
                continue
            if prior is None:
                added += 1
            else:
                updated += 1
            connection.execute(
                """
                INSERT INTO indexed_files(
                    workspace_id, relative_path, kind, size_bytes, content_sha256
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, relative_path) DO UPDATE SET
                    kind = excluded.kind,
                    size_bytes = excluded.size_bytes,
                    content_sha256 = excluded.content_sha256
                """,
                (
                    record.workspace_id,
                    record.relative_path,
                    record.kind.value,
                    record.size_bytes,
                    record.content_sha256,
                ),
            )

        stale_paths = sorted(set(existing) - set(snapshot))
        for relative_path in stale_paths:
            _require_scan_deadline(deadline)
            connection.execute(
                "DELETE FROM indexed_files WHERE workspace_id = ? AND relative_path = ?",
                (workspace_id, relative_path),
            )

        _reconcile_search_documents(
            connection,
            workspace,
            snapshot,
            deadline=deadline,
        )
        _reconcile_code_units(
            connection,
            workspace,
            snapshot,
            deadline=deadline,
        )

        reconcile_knowledge_staleness(
            connection,
            workspace_id,
            {
                relative_path: (record.kind.value, record.content_sha256)
                for relative_path, record in snapshot.items()
            },
            eligible_knowledge_ids=eligible_knowledge_ids,
        )
        _write_index_reconcile_provenance(connection, workspace_id, kind=kind)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    return ScanResult(
        workspace_id=workspace_id,
        file_count=len(snapshot),
        added=added,
        updated=updated,
        removed=len(stale_paths),
    )


def _write_index_reconcile_provenance(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    kind: IndexReconcileKind,
) -> None:
    connection.execute(
        """
        INSERT INTO workspace_index_reconcile(
            workspace_id, index_revision, last_successful_reconcile_at, last_reconcile_kind
        ) VALUES (?, 1, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            index_revision = workspace_index_reconcile.index_revision + 1,
            last_successful_reconcile_at = excluded.last_successful_reconcile_at,
            last_reconcile_kind = excluded.last_reconcile_kind
        """,
        (workspace_id, _utc_timestamp(), kind.value),
    )


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _normalize_incremental_paths(relative_paths: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(relative_paths))
    if not selected:
        raise ValueError("incremental Workspace scan requires at least one path")
    if len(selected) > MAX_INCREMENTAL_SCAN_PATHS:
        raise ValueError(
            f"incremental Workspace scan accepts at most {MAX_INCREMENTAL_SCAN_PATHS} paths"
        )
    for relative_path in selected:
        path = Path(relative_path)
        if not relative_path or "\x00" in relative_path or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe incremental Workspace path: {relative_path!r}")
    return tuple(sorted(selected))


def list_workspace_candidate_paths(
    workspace: WorkspaceRecord,
    *,
    deadline: float | None = None,
) -> tuple[str, ...]:
    """Return the exact Git/.harnessignore path set eligible for the Structural Index."""
    _require_scan_deadline(deadline)
    _require_registered_layout(workspace, deadline=deadline)
    harnessignore_rules = _read_harnessignore_rules(workspace.workspace_root)
    paths = _candidate_paths(
        workspace.workspace_root,
        harnessignore_rules,
        deadline=deadline,
    )
    _require_registered_layout(workspace, deadline=deadline)
    return paths


def list_indexed_files(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> tuple[IndexedFileRecord, ...]:
    """Return the current rebuildable file inventory in stable path order."""
    rows = connection.execute(
        """
        SELECT workspace_id, relative_path, kind, size_bytes, content_sha256
        FROM indexed_files
        WHERE workspace_id = ?
        ORDER BY relative_path
        """,
        (workspace_id,),
    ).fetchall()
    return tuple(_record_from_row(row) for row in rows)


def get_indexed_file(
    connection: sqlite3.Connection,
    workspace_id: str,
    relative_path: str,
) -> IndexedFileRecord | None:
    """Return one exact current index entry by Workspace-relative path."""
    row = connection.execute(
        """
        SELECT workspace_id, relative_path, kind, size_bytes, content_sha256
        FROM indexed_files
        WHERE workspace_id = ? AND relative_path = ?
        """,
        (workspace_id, relative_path),
    ).fetchone()
    return None if row is None else _record_from_row(row)


def _require_registered_layout(
    workspace: WorkspaceRecord, *, deadline: float | None = None
) -> None:
    layout = inspect_git_workspace(workspace.workspace_root, deadline=deadline)
    if (
        layout.workspace_root != workspace.workspace_root
        or layout.git_common_dir != workspace.git_common_dir
    ):
        raise WorkspaceIndexMismatchError(
            f"registered workspace Git identity changed: {workspace.workspace_root}"
        )


def _build_snapshot(
    workspace: WorkspaceRecord,
    *,
    deadline: float | None,
) -> dict[str, IndexedFileRecord]:
    _require_scan_deadline(deadline)
    harnessignore_rules = _read_harnessignore_rules(workspace.workspace_root)
    relative_paths = _candidate_paths(
        workspace.workspace_root,
        harnessignore_rules,
        deadline=deadline,
    )
    snapshot: dict[str, IndexedFileRecord] = {}
    for relative_path in relative_paths:
        _require_scan_deadline(deadline)
        record = _inspect_entry(workspace, relative_path, deadline=deadline)
        if record is not None:
            snapshot[relative_path] = record
    _require_scan_deadline(deadline)
    if _read_harnessignore_rules(workspace.workspace_root) != harnessignore_rules:
        raise IndexingError("Workspace changed while scanning: .harnessignore")
    return snapshot


def _build_search_document(
    workspace: WorkspaceRecord,
    record: IndexedFileRecord,
    *,
    deadline: float | None,
) -> IndexedSearchDocument | None:
    _require_scan_deadline(deadline)
    if (
        record.kind is not IndexedFileKind.FILE
        or record.size_bytes > MAX_INDEXED_SEARCH_BODY_BYTES
        or is_generated_text_output_path(record.relative_path)
    ):
        return None
    body = _read_stable_search_text(
        workspace,
        record.relative_path,
        expected_content_sha256=record.content_sha256,
        deadline=deadline,
    )
    if body is None:
        return None
    return IndexedSearchDocument(
        relative_path=record.relative_path,
        corpus="docs" if is_document_path(record.relative_path) else "code",
        content_sha256=record.content_sha256,
        title=Path(record.relative_path).name,
        path_tokens=" ".join(identifier_tokens(record.relative_path)),
        identifier_tokens=identifier_expansion(
            body,
            maximum_bytes=MAX_INDEXED_IDENTIFIER_TOKENS_BYTES,
        ),
        body=body,
    )


class SearchEvidenceReadStatus(StrEnum):
    """Outcome of a current-source search reread that must not fail the whole query."""

    OK = "ok"
    CHANGED_SINCE_INDEX = "changed_since_index"
    UNAVAILABLE = "current_match_not_relocated"


@dataclass(frozen=True, slots=True)
class SearchEvidenceRead:
    """Bounded live UTF-8 text, or a reason that evidence must stay null."""

    status: SearchEvidenceReadStatus
    text: str | None = None


class ExactSearchReadStatus(StrEnum):
    """Outcome of a bounded exact-search source read."""

    OK = "ok"
    CHANGED_SINCE_INDEX = "changed_since_index"
    NON_TEXT = "non_text"
    TOO_LARGE = "too_large"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ExactSearchRead:
    """Current bounded UTF-8 source for exhaustive local literal matching."""

    status: ExactSearchReadStatus
    text: str | None = None


def read_current_search_text(
    workspace: WorkspaceRecord,
    relative_path: str,
    *,
    expected_content_sha256: str,
    deadline: float | None = None,
) -> SearchEvidenceRead:
    """Reread one indexed path with the same stable-entry invariants as content indexing.

    SHA mismatch is a locator-only miss for search; indexing still raises via
    ``_read_stable_search_text``.
    """
    try:
        payload = _read_stable_regular_file_bytes(
            workspace,
            relative_path,
            deadline=deadline,
        )
        _require_scan_deadline(deadline)
        if len(payload) > MAX_INDEXED_SEARCH_BODY_BYTES:
            return SearchEvidenceRead(SearchEvidenceReadStatus.UNAVAILABLE)
        return _decode_indexed_search_payload(payload, expected_content_sha256)
    except IndexingError:
        return SearchEvidenceRead(SearchEvidenceReadStatus.UNAVAILABLE)


def read_current_exact_search_text(
    workspace: WorkspaceRecord,
    relative_path: str,
    *,
    expected_content_sha256: str,
    deadline: float | None = None,
) -> ExactSearchRead:
    """Read one current regular file for bounded exact matching without exposing its body."""
    try:
        payload = _read_stable_regular_file_bytes(
            workspace,
            relative_path,
            deadline=deadline,
            maximum_bytes=MAX_EXACT_SEARCH_FILE_BYTES,
        )
    except IndexingError:
        return ExactSearchRead(ExactSearchReadStatus.UNAVAILABLE)
    if len(payload) > MAX_EXACT_SEARCH_FILE_BYTES:
        return ExactSearchRead(ExactSearchReadStatus.TOO_LARGE)
    if hashlib.sha256(payload).hexdigest() != expected_content_sha256:
        return ExactSearchRead(ExactSearchReadStatus.CHANGED_SINCE_INDEX)
    if b"\x00" in payload:
        return ExactSearchRead(ExactSearchReadStatus.NON_TEXT)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ExactSearchRead(ExactSearchReadStatus.NON_TEXT)
    return ExactSearchRead(ExactSearchReadStatus.OK, text)


def _read_stable_search_text(
    workspace: WorkspaceRecord,
    relative_path: str,
    *,
    expected_content_sha256: str,
    deadline: float | None,
) -> str | None:
    payload = _read_stable_regular_file_bytes(
        workspace,
        relative_path,
        deadline=deadline,
    )
    _require_scan_deadline(deadline)
    if len(payload) > MAX_INDEXED_SEARCH_BODY_BYTES:
        raise IndexingError(f"Workspace changed while scanning: {relative_path}")
    decoded = _decode_indexed_search_payload(payload, expected_content_sha256)
    if decoded.status is SearchEvidenceReadStatus.CHANGED_SINCE_INDEX:
        raise IndexingError(f"Workspace changed while scanning: {relative_path}")
    if decoded.status is SearchEvidenceReadStatus.UNAVAILABLE:
        return None
    return decoded.text


def _decode_indexed_search_payload(
    payload: bytes, expected_content_sha256: str
) -> SearchEvidenceRead:
    if hashlib.sha256(payload).hexdigest() != expected_content_sha256:
        return SearchEvidenceRead(SearchEvidenceReadStatus.CHANGED_SINCE_INDEX)
    if b"\x00" in payload:
        return SearchEvidenceRead(SearchEvidenceReadStatus.UNAVAILABLE)
    try:
        return SearchEvidenceRead(SearchEvidenceReadStatus.OK, payload.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return SearchEvidenceRead(SearchEvidenceReadStatus.UNAVAILABLE)


def _read_stable_regular_file_bytes(
    workspace: WorkspaceRecord,
    relative_path: str,
    *,
    deadline: float | None,
    maximum_bytes: int = MAX_INDEXED_SEARCH_BODY_BYTES,
) -> bytes:
    path = workspace.workspace_root / relative_path
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_relative_to(workspace.workspace_root):
            raise WorkspaceIndexMismatchError(
                f"Workspace path escapes through a symlinked parent: {relative_path}"
            )
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise IndexingError(f"Workspace changed while scanning: {relative_path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(workspace.workspace_root):
            raise WorkspaceIndexMismatchError(
                f"Workspace file resolves outside root: {relative_path}"
            )
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            _require_stable_entry(relative_path, before, opened_before)
            payload = stream.read(maximum_bytes + 1)
            opened_after = os.fstat(stream.fileno())
        _require_stable_entry(relative_path, opened_before, opened_after)
        current = path.lstat()
        _require_stable_entry(relative_path, opened_after, current)
    except FileNotFoundError as exc:
        raise IndexingError(f"Workspace changed while scanning: {relative_path}") from exc
    except OSError as exc:
        raise IndexingError(f"Workspace search text could not be read: {relative_path}") from exc
    return payload


def _reconcile_search_documents(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    snapshot: dict[str, IndexedFileRecord],
    *,
    deadline: float | None,
) -> None:
    rows = connection.execute(
        """
        SELECT id, relative_path, corpus, content_sha256
        FROM indexed_search_documents
        WHERE workspace_id = ?
        ORDER BY relative_path
        """,
        (workspace.workspace_id,),
    ).fetchall()
    existing: dict[str, tuple[int, str, str]] = {}
    for document_id, relative_path, corpus, content_sha256 in rows:
        if (
            isinstance(document_id, bool)
            or not isinstance(document_id, int)
            or document_id <= 0
            or not isinstance(relative_path, str)
            or relative_path in existing
            or corpus not in {"code", "docs"}
            or not isinstance(content_sha256, str)
            or not _is_sha256(content_sha256)
        ):
            raise IndexingError("indexed search document row has invalid persisted types")
        existing[relative_path] = (document_id, corpus, content_sha256)

    indexed_rowids: set[int] = set()
    for (raw_rowid,) in connection.execute(
        """
        SELECT indexed_content_search.rowid
        FROM indexed_content_search
        JOIN indexed_search_documents AS documents
            ON documents.id = indexed_content_search.rowid
        WHERE documents.workspace_id = ?
        """,
        (workspace.workspace_id,),
    ).fetchall():
        if isinstance(raw_rowid, bool) or not isinstance(raw_rowid, int) or raw_rowid <= 0:
            raise IndexingError("indexed content search returned an invalid row identity")
        indexed_rowids.add(raw_rowid)

    searchable_paths: set[str] = set()
    for record in snapshot.values():
        _require_scan_deadline(deadline)
        prior = existing.get(record.relative_path)
        expected_corpus = "docs" if is_document_path(record.relative_path) else "code"
        if (
            record.kind is IndexedFileKind.FILE
            and record.size_bytes <= MAX_INDEXED_SEARCH_BODY_BYTES
            and not is_generated_text_output_path(record.relative_path)
            and prior is not None
            and prior[0] in indexed_rowids
            and prior[1:] == (expected_corpus, record.content_sha256)
        ):
            searchable_paths.add(record.relative_path)
            continue
        document = _build_search_document(workspace, record, deadline=deadline)
        if document is None:
            continue
        relative_path = document.relative_path
        searchable_paths.add(relative_path)
        prior = existing.get(relative_path)
        if (
            prior is not None
            and prior[0] in indexed_rowids
            and prior[1:] == (document.corpus, document.content_sha256)
        ):
            continue
        if prior is None:
            row = connection.execute(
                """
                INSERT INTO indexed_search_documents(
                    workspace_id, relative_path, corpus, content_sha256,
                    title, path_tokens, identifier_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    workspace.workspace_id,
                    document.relative_path,
                    document.corpus,
                    document.content_sha256,
                    document.title,
                    document.path_tokens,
                    document.identifier_tokens,
                ),
            ).fetchone()
            if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
                raise IndexingError("indexed search document identity was not persisted")
            document_id = row[0]
        else:
            document_id = prior[0]
            connection.execute(
                "DELETE FROM indexed_content_search WHERE rowid = ?",
                (document_id,),
            )
            connection.execute(
                """
                UPDATE indexed_search_documents
                SET corpus = ?, content_sha256 = ?, title = ?,
                    path_tokens = ?, identifier_tokens = ?
                WHERE id = ?
                """,
                (
                    document.corpus,
                    document.content_sha256,
                    document.title,
                    document.path_tokens,
                    document.identifier_tokens,
                    document_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO indexed_content_search(
                rowid, title, path_tokens, identifier_tokens, body
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                document_id,
                document.title,
                document.path_tokens,
                document.identifier_tokens,
                document.body,
            ),
        )

    for relative_path in sorted(set(existing) - searchable_paths):
        _require_scan_deadline(deadline)
        connection.execute(
            """
            DELETE FROM indexed_search_documents
            WHERE workspace_id = ? AND relative_path = ?
            """,
            (workspace.workspace_id, relative_path),
        )


def _reconcile_code_units(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    snapshot: dict[str, IndexedFileRecord],
    *,
    deadline: float | None,
) -> None:
    rows = connection.execute(
        """
        SELECT relative_path, content_sha256, language, status
        FROM indexed_code_unit_files
        WHERE workspace_id = ?
        ORDER BY relative_path
        """,
        (workspace.workspace_id,),
    ).fetchall()
    existing: dict[str, tuple[str, str, str]] = {}
    valid_statuses = {"ok", "parse_error", "too_large", "non_text", "unit_limit"}
    for relative_path, content_sha256, language, status in rows:
        if (
            not isinstance(relative_path, str)
            or relative_path in existing
            or not isinstance(content_sha256, str)
            or not _is_sha256(content_sha256)
            or not isinstance(language, str)
            or not isinstance(status, str)
            or status not in valid_statuses
        ):
            raise IndexingError("indexed code-unit manifest row has invalid persisted types")
        existing[relative_path] = (content_sha256, language, status)

    unit_ids_by_path: dict[str, set[int]] = {}
    for raw_id, relative_path in connection.execute(
        """
        SELECT id, relative_path
        FROM indexed_code_units
        WHERE workspace_id = ?
        ORDER BY relative_path, position
        """,
        (workspace.workspace_id,),
    ).fetchall():
        if (
            isinstance(raw_id, bool)
            or not isinstance(raw_id, int)
            or raw_id <= 0
            or not isinstance(relative_path, str)
        ):
            raise IndexingError("indexed code-unit row has invalid persisted types")
        unit_ids_by_path.setdefault(relative_path, set()).add(raw_id)

    fts_ids_by_path: dict[str, set[int]] = {}
    for raw_id, relative_path in connection.execute(
        """
        SELECT units.id, units.relative_path
        FROM indexed_code_unit_search
        JOIN indexed_code_units AS units ON units.id = indexed_code_unit_search.rowid
        WHERE units.workspace_id = ?
        ORDER BY units.relative_path, units.position
        """,
        (workspace.workspace_id,),
    ).fetchall():
        if (
            isinstance(raw_id, bool)
            or not isinstance(raw_id, int)
            or raw_id <= 0
            or not isinstance(relative_path, str)
        ):
            raise IndexingError("indexed code-unit search returned an invalid row identity")
        fts_ids_by_path.setdefault(relative_path, set()).add(raw_id)

    eligible_paths: set[str] = set()
    for record in snapshot.values():
        _require_scan_deadline(deadline)
        language = precise_symbol_language(record.relative_path)
        if record.kind is not IndexedFileKind.FILE or language is None:
            continue
        eligible_paths.add(record.relative_path)
        prior = existing.get(record.relative_path)
        unit_ids = unit_ids_by_path.get(record.relative_path, set())
        fts_ids = fts_ids_by_path.get(record.relative_path, set())
        if _code_unit_manifest_is_current(
            prior,
            content_sha256=record.content_sha256,
            language=language,
            unit_ids=unit_ids,
            fts_ids=fts_ids,
        ):
            continue

        if record.size_bytes > MAX_SYMBOL_PARSE_BYTES:
            _replace_code_unit_file(
                connection,
                workspace.workspace_id,
                record,
                language=language,
                status="too_large",
                definitions=(),
            )
            continue

        text = _read_stable_search_text(
            workspace,
            record.relative_path,
            expected_content_sha256=record.content_sha256,
            deadline=deadline,
        )
        definitions: tuple[SyntaxRelation, ...]
        if text is None:
            status = "non_text"
            definitions = ()
        else:
            analysis = analyze_precise_code_units(record.relative_path, text)
            if analysis.language != language:
                raise IndexingError("precise code-unit parser language changed during indexing")
            if analysis.status == "ok":
                definitions = analysis.relations
                status = "ok"
                if not _code_unit_definitions_fit_bounds(definitions):
                    definitions = ()
                    status = "unit_limit"
            elif analysis.status == "too_large":
                definitions = ()
                status = "too_large"
            else:
                definitions = ()
                status = "parse_error"

        _replace_code_unit_file(
            connection,
            workspace.workspace_id,
            record,
            language=language,
            status=status,
            definitions=definitions,
        )

    for relative_path in sorted(set(existing) - eligible_paths):
        _require_scan_deadline(deadline)
        connection.execute(
            """
            DELETE FROM indexed_code_unit_files
            WHERE workspace_id = ? AND relative_path = ?
            """,
            (workspace.workspace_id, relative_path),
        )


def _code_unit_manifest_is_current(
    prior: tuple[str, str, str] | None,
    *,
    content_sha256: str,
    language: str,
    unit_ids: set[int],
    fts_ids: set[int],
) -> bool:
    if prior is None or prior[:2] != (content_sha256, language):
        return False
    status = prior[2]
    if status == "ok":
        return unit_ids == fts_ids
    return not unit_ids and not fts_ids


def _code_unit_definitions_fit_bounds(definitions: Sequence[SyntaxRelation]) -> bool:
    if len(definitions) > MAX_INDEXED_CODE_UNITS_PER_FILE:
        return False
    for definition in definitions:
        target = definition.target
        symbol_kind = definition.symbol_kind
        if definition.kind != "definition" or not target or symbol_kind is None:
            raise IndexingError("precise code-unit parser returned a non-definition relation")
        name = _code_unit_name(target)
        if (
            len(name.encode("utf-8")) > MAX_INDEXED_CODE_UNIT_NAME_BYTES
            or len(target.encode("utf-8")) > MAX_INDEXED_CODE_UNIT_QUALIFIED_NAME_BYTES
        ):
            return False
    return True


def _replace_code_unit_file(
    connection: sqlite3.Connection,
    workspace_id: str,
    record: IndexedFileRecord,
    *,
    language: str,
    status: str,
    definitions: Sequence[SyntaxRelation],
) -> None:
    connection.execute(
        "DELETE FROM indexed_code_unit_files WHERE workspace_id = ? AND relative_path = ?",
        (workspace_id, record.relative_path),
    )
    connection.execute(
        """
        INSERT INTO indexed_code_unit_files(
            workspace_id, relative_path, content_sha256, language, status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (workspace_id, record.relative_path, record.content_sha256, language, status),
    )
    if status != "ok":
        if definitions:
            raise IndexingError("non-ok code-unit manifest cannot persist definitions")
        return
    for position, definition in enumerate(definitions):
        target = definition.target
        symbol_kind = definition.symbol_kind
        line = definition.line
        column = definition.column
        if symbol_kind is None or line <= 0 or column <= 0:
            raise IndexingError("precise code-unit definition has invalid persisted fields")
        name = _code_unit_name(target)
        row = connection.execute(
            """
            INSERT INTO indexed_code_units(
                workspace_id, relative_path, position, name, qualified_name,
                symbol_kind, line, column
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                workspace_id,
                record.relative_path,
                position,
                name,
                target,
                symbol_kind,
                line,
                column,
            ),
        ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise IndexingError("indexed code-unit identity was not persisted")
        normalized_tokens = " ".join(dict.fromkeys(identifier_tokens(target)))
        connection.execute(
            """
            INSERT INTO indexed_code_unit_search(
                rowid, name, qualified_name, identifier_tokens, symbol_kind
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (row[0], name, target, normalized_tokens, symbol_kind),
        )


def _code_unit_name(qualified_name: str) -> str:
    return qualified_name.replace("::", ".").rsplit(".", 1)[-1]


def _candidate_paths(
    workspace_root: Path,
    harnessignore_rules: bytes | None,
    *,
    deadline: float | None,
    pathspecs: Sequence[str] = (),
) -> tuple[str, ...]:
    exclude_arguments = [f"--exclude={pattern}" for pattern in _DEFAULT_EXCLUDES]
    if harnessignore_rules is None:
        return _candidate_paths_from_git(
            workspace_root,
            exclude_arguments,
            deadline=deadline,
            pathspecs=pathspecs,
        )

    try:
        with TemporaryDirectory(prefix="harness-ignore-") as temporary_directory:
            _require_scan_deadline(deadline)
            exclude_file = Path(temporary_directory) / "rules"
            exclude_file.write_bytes(harnessignore_rules)
            exclude_arguments.append(f"--exclude-from={exclude_file}")
            return _candidate_paths_from_git(
                workspace_root,
                exclude_arguments,
                deadline=deadline,
                pathspecs=pathspecs,
            )
    except OSError as exc:
        raise IndexingError("Workspace .harnessignore snapshot could not be prepared") from exc


def _candidate_paths_from_git(
    workspace_root: Path,
    exclude_arguments: list[str],
    *,
    deadline: float | None,
    pathspecs: Sequence[str],
) -> tuple[str, ...]:
    path_arguments = ("--", *pathspecs) if pathspecs else ()
    candidates = _git_ls_files(
        workspace_root,
        "--cached",
        "--others",
        "--exclude-standard",
        *exclude_arguments,
        *path_arguments,
        deadline=deadline,
    )
    excluded_tracked = set(
        _git_ls_files(
            workspace_root,
            "--cached",
            "--ignored",
            *exclude_arguments,
            *path_arguments,
            deadline=deadline,
        )
    )
    return tuple(sorted(set(candidates) - excluded_tracked))


def _read_harnessignore_rules(workspace_root: Path) -> bytes | None:
    harnessignore = workspace_root / ".harnessignore"
    try:
        before = harnessignore.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IndexingError("Workspace .harnessignore could not be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        return None

    try:
        with harnessignore.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            _require_stable_entry(".harnessignore", before, opened_before)
            rules = stream.read()
            opened_after = os.fstat(stream.fileno())
        _require_stable_entry(".harnessignore", opened_before, opened_after)
        current = harnessignore.lstat()
        _require_stable_entry(".harnessignore", opened_after, current)
    except FileNotFoundError as exc:
        raise IndexingError("Workspace changed while scanning: .harnessignore") from exc
    except OSError as exc:
        raise IndexingError("Workspace .harnessignore could not be read safely") from exc
    return rules


def _git_ls_files(
    workspace_root: Path,
    *arguments: str,
    deadline: float | None,
) -> tuple[str, ...]:
    environment = _git_environment()
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=environment,
            timeout=_remaining_scan_seconds(deadline),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScanDeadlineExceededError("Workspace scan deadline exceeded") from exc
    except FileNotFoundError as exc:
        raise IndexingError("Git executable is not available for Workspace scan") from exc
    except OSError as exc:
        raise IndexingError(f"Git could not enumerate Workspace files: {workspace_root}") from exc
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip()
        message = f"Git could not enumerate Workspace files: {workspace_root}"
        if detail:
            message = f"{message}: {detail}"
        raise IndexingError(message)

    paths: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        _require_scan_deadline(deadline)
        if not raw_path:
            continue
        relative_path = os.fsdecode(raw_path)
        try:
            relative_path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise IndexingError(
                "Workspace contains a path that cannot be persisted as UTF-8"
            ) from exc
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise IndexingError(f"Git returned an unsafe Workspace path: {relative_path!r}")
        paths.append(relative_path)
    return tuple(paths)


def _inspect_entry(
    workspace: WorkspaceRecord,
    relative_path: str,
    *,
    deadline: float | None,
) -> IndexedFileRecord | None:
    _require_scan_deadline(deadline)
    path = workspace.workspace_root / relative_path
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_relative_to(workspace.workspace_root):
            raise WorkspaceIndexMismatchError(
                f"Workspace path escapes through a symlinked parent: {relative_path}"
            )
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(path)
            after = path.lstat()
            _require_stable_entry(relative_path, before, after)
            digest = hashlib.sha256(b"symlink\0" + os.fsencode(target)).hexdigest()
            return IndexedFileRecord(
                workspace_id=workspace.workspace_id,
                relative_path=relative_path,
                kind=IndexedFileKind.SYMLINK,
                size_bytes=before.st_size,
                content_sha256=digest,
            )
        if not stat.S_ISREG(before.st_mode):
            return None

        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(workspace.workspace_root):
            raise WorkspaceIndexMismatchError(
                f"Workspace file resolves outside root: {relative_path}"
            )
        digest, opened_before, opened_after = _hash_regular_file(
            path,
            relative_path=relative_path,
            expected_before=before,
            deadline=deadline,
        )
        _require_stable_entry(relative_path, opened_before, opened_after)
        current = path.lstat()
        _require_stable_entry(relative_path, opened_after, current)
        return IndexedFileRecord(
            workspace_id=workspace.workspace_id,
            relative_path=relative_path,
            kind=IndexedFileKind.FILE,
            size_bytes=opened_before.st_size,
            content_sha256=digest,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IndexingError(f"Workspace entry could not be inspected: {relative_path}") from exc


def _hash_regular_file(
    path: Path,
    *,
    relative_path: str,
    expected_before: os.stat_result,
    deadline: float | None,
) -> tuple[str, os.stat_result, os.stat_result]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened_before = os.fstat(stream.fileno())
        _require_stable_entry(relative_path, expected_before, opened_before)
        while True:
            _require_scan_deadline(deadline)
            chunk = stream.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        opened_after = os.fstat(stream.fileno())
    return digest.hexdigest(), opened_before, opened_after


def _require_stable_entry(
    relative_path: str,
    before: os.stat_result,
    after: os.stat_result,
) -> None:
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise IndexingError(f"Workspace changed while scanning: {relative_path}")


def _remaining_scan_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ScanDeadlineExceededError("Workspace scan deadline exceeded")
    return remaining


def _require_scan_deadline(deadline: float | None) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise ScanDeadlineExceededError("Workspace scan deadline exceeded")


def _record_from_row(row: tuple[object, ...]) -> IndexedFileRecord:
    workspace_id, relative_path, kind, size_bytes, content_sha256 = row
    if (
        not isinstance(workspace_id, str)
        or not isinstance(relative_path, str)
        or not isinstance(kind, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or not isinstance(content_sha256, str)
        or not _is_sha256(content_sha256)
    ):
        raise IndexingError("indexed file row has invalid persisted types")
    try:
        file_kind = IndexedFileKind(kind)
    except ValueError as exc:
        raise IndexingError(f"indexed file row has unsupported kind: {kind!r}") from exc
    return IndexedFileRecord(
        workspace_id=workspace_id,
        relative_path=relative_path,
        kind=file_kind,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
