from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
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
MAX_INDEXED_IDENTIFIER_TOKENS_BYTES = 256 * 1024


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

    connection.execute("BEGIN IMMEDIATE")
    try:
        _require_scan_deadline(deadline)
        current_workspace = get_workspace(connection, workspace_id)
        if current_workspace != workspace:
            raise WorkspaceIndexMismatchError("workspace registry identity changed during scan")

        existing = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
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

        reconcile_knowledge_staleness(
            connection,
            workspace_id,
            {
                relative_path: (record.kind.value, record.content_sha256)
                for relative_path, record in snapshot.items()
            },
            eligible_knowledge_ids=eligible_knowledge_ids,
        )
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
        record,
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


def _read_stable_search_text(
    workspace: WorkspaceRecord,
    record: IndexedFileRecord,
    *,
    deadline: float | None,
) -> str | None:
    path = workspace.workspace_root / record.relative_path
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_relative_to(workspace.workspace_root):
            raise WorkspaceIndexMismatchError(
                f"Workspace path escapes through a symlinked parent: {record.relative_path}"
            )
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise IndexingError(f"Workspace changed while scanning: {record.relative_path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(workspace.workspace_root):
            raise WorkspaceIndexMismatchError(
                f"Workspace file resolves outside root: {record.relative_path}"
            )
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            _require_stable_entry(record.relative_path, before, opened_before)
            payload = stream.read(MAX_INDEXED_SEARCH_BODY_BYTES + 1)
            opened_after = os.fstat(stream.fileno())
        _require_stable_entry(record.relative_path, opened_before, opened_after)
        current = path.lstat()
        _require_stable_entry(record.relative_path, opened_after, current)
    except FileNotFoundError as exc:
        raise IndexingError(f"Workspace changed while scanning: {record.relative_path}") from exc
    except OSError as exc:
        raise IndexingError(
            f"Workspace search text could not be read: {record.relative_path}"
        ) from exc

    _require_scan_deadline(deadline)
    if len(payload) > MAX_INDEXED_SEARCH_BODY_BYTES:
        raise IndexingError(f"Workspace changed while scanning: {record.relative_path}")
    if hashlib.sha256(payload).hexdigest() != record.content_sha256:
        raise IndexingError(f"Workspace changed while scanning: {record.relative_path}")
    if b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


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


def _candidate_paths(
    workspace_root: Path,
    harnessignore_rules: bytes | None,
    *,
    deadline: float | None,
) -> tuple[str, ...]:
    exclude_arguments = [f"--exclude={pattern}" for pattern in _DEFAULT_EXCLUDES]
    if harnessignore_rules is None:
        return _candidate_paths_from_git(
            workspace_root,
            exclude_arguments,
            deadline=deadline,
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
            )
    except OSError as exc:
        raise IndexingError("Workspace .harnessignore snapshot could not be prepared") from exc


def _candidate_paths_from_git(
    workspace_root: Path,
    exclude_arguments: list[str],
    *,
    deadline: float | None,
) -> tuple[str, ...]:
    candidates = _git_ls_files(
        workspace_root,
        "--cached",
        "--others",
        "--exclude-standard",
        *exclude_arguments,
        deadline=deadline,
    )
    excluded_tracked = set(
        _git_ls_files(
            workspace_root,
            "--cached",
            "--ignored",
            *exclude_arguments,
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
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=_git_environment(),
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
