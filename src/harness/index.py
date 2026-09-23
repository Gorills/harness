from __future__ import annotations

import fnmatch
import hashlib
import os
import sqlite3
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic

from harness.git_workspace import _git_environment, inspect_workspace_layout
from harness.knowledge import (
    reconcile_knowledge_staleness,
    snapshot_fresh_anchored_knowledge_ids,
)
from harness.registry import (
    WorkspaceRecord,
    attach_workspace_git_if_present,
    get_workspace,
    workspace_layout_compatible,
)

_DEFAULT_EXCLUDES = (
    "node_modules/",
    ".venv/",
    "venv/",
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
_FILESYSTEM_DIR_EXCLUDES = frozenset(
    {".git", "node_modules", "vendor", "dist", "build", "target", "caches"}
)
_HASH_CHUNK_BYTES = 128 * 1024
_INDEX_DELETE_BATCH_SIZE = 256
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
    workspace = attach_workspace_git_if_present(connection, workspace.workspace_id)
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
    workspace = attach_workspace_git_if_present(connection, workspace.workspace_id)
    _require_registered_layout(workspace, deadline=deadline)
    eligible_knowledge_ids = snapshot_fresh_anchored_knowledge_ids(connection, workspace_id)
    existing = {
        record.relative_path: record for record in list_indexed_files(connection, workspace_id)
    }
    harnessignore_rules = _read_harnessignore_rules(workspace.workspace_root)
    candidates = _candidate_paths(
        workspace,
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
        _delete_stale_indexed_files(
            connection,
            workspace_id,
            stale_paths,
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


def _delete_stale_indexed_files(
    connection: sqlite3.Connection,
    workspace_id: str,
    stale_paths: Sequence[str],
    *,
    deadline: float | None,
) -> None:
    """Delete stale mechanical index entries in bounded SQL batches."""
    for offset in range(0, len(stale_paths), _INDEX_DELETE_BATCH_SIZE):
        _require_scan_deadline(deadline)
        batch = stale_paths[offset : offset + _INDEX_DELETE_BATCH_SIZE]
        placeholders = ", ".join("?" for _path in batch)
        connection.execute(
            f"DELETE FROM indexed_files WHERE workspace_id = ? "
            f"AND relative_path IN ({placeholders})",
            (workspace_id, *batch),
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
    layout = inspect_workspace_layout(workspace.workspace_root, deadline=deadline)
    if not workspace_layout_compatible(workspace, layout):
        raise WorkspaceIndexMismatchError(
            f"registered workspace identity changed: {workspace.workspace_root}"
        )


def _build_snapshot(
    workspace: WorkspaceRecord,
    *,
    deadline: float | None,
) -> dict[str, IndexedFileRecord]:
    _require_scan_deadline(deadline)
    harnessignore_rules = _read_harnessignore_rules(workspace.workspace_root)
    relative_paths = _candidate_paths(
        workspace,
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


def _candidate_paths(
    workspace: WorkspaceRecord,
    harnessignore_rules: bytes | None,
    *,
    deadline: float | None,
    pathspecs: Sequence[str] = (),
) -> tuple[str, ...]:
    if workspace.git_common_dir == workspace.workspace_root:
        return _candidate_paths_from_filesystem(
            workspace.workspace_root,
            harnessignore_rules,
            deadline=deadline,
            pathspecs=pathspecs,
        )
    exclude_arguments = [f"--exclude={pattern}" for pattern in _DEFAULT_EXCLUDES]
    if harnessignore_rules is None:
        return _candidate_paths_from_git(
            workspace.workspace_root,
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
                workspace.workspace_root,
                exclude_arguments,
                deadline=deadline,
                pathspecs=pathspecs,
            )
    except OSError as exc:
        raise IndexingError("Workspace .harnessignore snapshot could not be prepared") from exc


def _candidate_paths_from_filesystem(
    workspace_root: Path,
    harnessignore_rules: bytes | None,
    *,
    deadline: float | None,
    pathspecs: Sequence[str],
) -> tuple[str, ...]:
    ignore_patterns = _harnessignore_patterns(harnessignore_rules)
    if pathspecs:
        selected: list[str] = []
        for relative_path in pathspecs:
            _require_scan_deadline(deadline)
            if _filesystem_relative_excluded(
                relative_path,
                is_dir=False,
                ignore_patterns=ignore_patterns,
            ):
                continue
            path = workspace_root / relative_path
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise IndexingError(
                    f"Workspace path could not be inspected: {relative_path}"
                ) from exc
            selected.append(relative_path)
        return tuple(sorted(selected))

    paths: list[str] = []
    pending: list[tuple[str, Path]] = [("", workspace_root)]
    while pending:
        _require_scan_deadline(deadline)
        relative, directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            if relative:
                continue
            raise IndexingError("Workspace root could not be enumerated") from exc
        for entry in entries:
            _require_scan_deadline(deadline)
            child_relative = entry.name if not relative else f"{relative}/{entry.name}"
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name in _FILESYSTEM_DIR_EXCLUDES or _filesystem_relative_excluded(
                    child_relative,
                    is_dir=True,
                    ignore_patterns=ignore_patterns,
                ):
                    continue
                pending.append((child_relative, Path(entry.path)))
                continue
            if _filesystem_relative_excluded(
                child_relative,
                is_dir=False,
                ignore_patterns=ignore_patterns,
            ):
                continue
            paths.append(child_relative)
    return tuple(sorted(paths))


def _harnessignore_patterns(harnessignore_rules: bytes | None) -> tuple[str, ...]:
    if harnessignore_rules is None:
        return ()
    try:
        text = harnessignore_rules.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IndexingError("Workspace .harnessignore is not valid UTF-8") from exc
    patterns: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return tuple(patterns)


def _filesystem_relative_excluded(
    relative_path: str,
    *,
    is_dir: bool,
    ignore_patterns: Sequence[str],
) -> bool:
    name = PurePosixPath(relative_path).name
    for glob in _DEFAULT_EXCLUDES:
        directory_only = glob.endswith("/")
        matched_pattern = glob[:-1] if directory_only else glob
        if directory_only:
            if _filesystem_directory_excluded(relative_path, matched_pattern, is_dir=is_dir):
                return True
            continue
        if fnmatch.fnmatch(name, matched_pattern):
            return True
    for pattern in ignore_patterns:
        directory_only = pattern.endswith("/")
        matched_pattern = pattern[:-1] if directory_only else pattern
        if directory_only:
            if _filesystem_directory_excluded(relative_path, matched_pattern, is_dir=is_dir):
                return True
            continue
        if fnmatch.fnmatch(relative_path, matched_pattern) or fnmatch.fnmatch(
            name, matched_pattern
        ):
            return True
    return False


def _filesystem_directory_excluded(relative_path: str, pattern: str, *, is_dir: bool) -> bool:
    path = PurePosixPath(relative_path)
    directories = (path,) if is_dir else tuple(parent for parent in path.parents if parent.name)
    return any(
        fnmatch.fnmatch(directory.as_posix(), pattern) or fnmatch.fnmatch(directory.name, pattern)
        for directory in directories
    )


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
