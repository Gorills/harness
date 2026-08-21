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

from harness.git_workspace import _git_environment, inspect_git_workspace
from harness.registry import WorkspaceRecord, get_workspace

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


class IndexingError(RuntimeError):
    """Base class for deterministic Structural Index failures."""


class WorkspaceIndexMismatchError(IndexingError):
    """Raised when the registered Workspace no longer matches its Git worktree identity."""


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
class ScanResult:
    """Compact reconciliation result for one deterministic Workspace scan."""

    workspace_id: str
    file_count: int
    added: int
    updated: int
    removed: int


def scan_workspace(connection: sqlite3.Connection, workspace_id: str) -> ScanResult:
    """Reconcile the rebuildable file inventory for one registered Workspace."""
    workspace = get_workspace(connection, workspace_id)
    _require_registered_layout(workspace)
    snapshot = _build_snapshot(workspace)
    _require_registered_layout(workspace)

    connection.execute("BEGIN IMMEDIATE")
    try:
        current_workspace = get_workspace(connection, workspace_id)
        if current_workspace != workspace:
            raise WorkspaceIndexMismatchError("workspace registry identity changed during scan")

        existing = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        added = 0
        updated = 0
        for relative_path, record in snapshot.items():
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
            connection.execute(
                "DELETE FROM indexed_files WHERE workspace_id = ? AND relative_path = ?",
                (workspace_id, relative_path),
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


def _require_registered_layout(workspace: WorkspaceRecord) -> None:
    layout = inspect_git_workspace(workspace.workspace_root)
    if (
        layout.workspace_root != workspace.workspace_root
        or layout.git_common_dir != workspace.git_common_dir
    ):
        raise WorkspaceIndexMismatchError(
            f"registered workspace Git identity changed: {workspace.workspace_root}"
        )


def _build_snapshot(workspace: WorkspaceRecord) -> dict[str, IndexedFileRecord]:
    harnessignore_rules = _read_harnessignore_rules(workspace.workspace_root)
    relative_paths = _candidate_paths(workspace.workspace_root, harnessignore_rules)
    snapshot: dict[str, IndexedFileRecord] = {}
    for relative_path in relative_paths:
        record = _inspect_entry(workspace, relative_path)
        if record is not None:
            snapshot[relative_path] = record
    if _read_harnessignore_rules(workspace.workspace_root) != harnessignore_rules:
        raise IndexingError("Workspace changed while scanning: .harnessignore")
    return snapshot


def _candidate_paths(
    workspace_root: Path,
    harnessignore_rules: bytes | None,
) -> tuple[str, ...]:
    exclude_arguments = [f"--exclude={pattern}" for pattern in _DEFAULT_EXCLUDES]
    if harnessignore_rules is None:
        return _candidate_paths_from_git(workspace_root, exclude_arguments)

    try:
        with TemporaryDirectory(prefix="harness-ignore-") as temporary_directory:
            exclude_file = Path(temporary_directory) / "rules"
            exclude_file.write_bytes(harnessignore_rules)
            exclude_arguments.append(f"--exclude-from={exclude_file}")
            return _candidate_paths_from_git(workspace_root, exclude_arguments)
    except OSError as exc:
        raise IndexingError("Workspace .harnessignore snapshot could not be prepared") from exc


def _candidate_paths_from_git(
    workspace_root: Path,
    exclude_arguments: list[str],
) -> tuple[str, ...]:
    candidates = _git_ls_files(
        workspace_root,
        "--cached",
        "--others",
        "--exclude-standard",
        *exclude_arguments,
    )
    excluded_tracked = set(
        _git_ls_files(
            workspace_root,
            "--cached",
            "--ignored",
            *exclude_arguments,
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


def _git_ls_files(workspace_root: Path, *arguments: str) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=_git_environment(),
        )
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
) -> IndexedFileRecord | None:
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
) -> tuple[str, os.stat_result, os.stat_result]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened_before = os.fstat(stream.fileno())
        _require_stable_entry(relative_path, expected_before, opened_before)
        while chunk := stream.read(_HASH_CHUNK_BYTES):
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
        or len(content_sha256) != 64
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
