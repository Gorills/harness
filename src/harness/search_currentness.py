from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from time import monotonic

from harness.git_workspace import _git_environment
from harness.index import (
    MAX_INCREMENTAL_SCAN_PATHS,
    IndexingError,
    list_indexed_files,
    list_workspace_candidate_paths,
    scan_workspace,
    scan_workspace_paths,
)
from harness.registry import WorkspaceRecord
from harness.watcher import (
    WorkspaceGitSnapshot,
    WorkspaceWatchError,
    read_workspace_change_snapshot,
)

_SEARCH_CURRENTNESS_MAX_ATTEMPTS = 2


class SearchCurrentnessError(IndexingError):
    """Raised when Project search cannot prove that its Structural Index is current."""


@dataclass(frozen=True, slots=True)
class WorkspaceSearchCurrentness:
    """One live Workspace/index state that Project search was reconciled against."""

    change_token: str
    git_head: str
    index_revision: int


@dataclass(frozen=True, slots=True)
class _PersistedSearchState:
    git_head: str
    change_token: str
    index_revision: int
    dirty_paths: tuple[str, ...]


def ensure_workspace_search_index_current(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    scan_lock: Lock,
    *,
    deadline: float,
) -> WorkspaceSearchCurrentness:
    """Synchronously reconcile search-visible source before Project retrieval.

    The watcher remains the normal background reconciler. This boundary exists so a Project
    search never relies on watcher timing after an edit, revert, untracked-file change, ignore-rule
    change, or branch switch.
    """
    remaining = deadline - monotonic()
    if remaining <= 0 or not scan_lock.acquire(timeout=remaining):
        raise SearchCurrentnessError("Project search currentness deadline exceeded")
    try:
        for _attempt in range(_SEARCH_CURRENTNESS_MAX_ATTEMPTS):
            before = _read_snapshot(workspace, deadline)
            persisted = _read_persisted_state(connection, workspace.workspace_id)
            index_revision = _read_index_revision(connection, workspace.workspace_id)
            indexed_paths = {
                record.relative_path
                for record in list_indexed_files(connection, workspace.workspace_id)
            }
            candidate_paths = set(list_workspace_candidate_paths(workspace, deadline=deadline))
            path_delta = indexed_paths ^ candidate_paths

            if (
                persisted is not None
                and persisted.change_token == before.token
                and persisted.index_revision == index_revision
                and not path_delta
            ):
                return WorkspaceSearchCurrentness(
                    change_token=before.token,
                    git_head=_snapshot_head_text(before),
                    index_revision=index_revision,
                )

            index_changed_under_same_live_state = (
                persisted is not None
                and persisted.change_token == before.token
                and persisted.index_revision != index_revision
            )
            incremental_paths = (
                None
                if index_changed_under_same_live_state
                else _reconcile_paths(
                    workspace.workspace_root,
                    persisted,
                    before,
                    path_delta,
                    deadline=deadline,
                )
            )
            if incremental_paths is None:
                scan_workspace(connection, workspace.workspace_id, deadline=deadline)
            elif incremental_paths:
                scan_workspace_paths(
                    connection,
                    workspace.workspace_id,
                    incremental_paths,
                    deadline=deadline,
                )

            after = _read_snapshot(workspace, deadline)
            after_candidates = set(list_workspace_candidate_paths(workspace, deadline=deadline))
            after_indexed = {
                record.relative_path
                for record in list_indexed_files(connection, workspace.workspace_id)
            }
            if before.token != after.token or after_candidates != after_indexed:
                continue

            _write_persisted_state(connection, workspace.workspace_id, after)
            return WorkspaceSearchCurrentness(
                change_token=after.token,
                git_head=_snapshot_head_text(after),
                index_revision=_read_index_revision(connection, workspace.workspace_id),
            )
    finally:
        scan_lock.release()

    raise SearchCurrentnessError("Workspace changed repeatedly while preparing Project search")


def workspace_search_state_is_unchanged(
    connection: sqlite3.Connection,
    workspace: WorkspaceRecord,
    currentness: WorkspaceSearchCurrentness,
    *,
    deadline: float,
) -> bool:
    """Return whether live source and index revision still match one prepared search state."""
    snapshot = _read_snapshot(workspace, deadline)
    return (
        snapshot.token == currentness.change_token
        and _snapshot_head_text(snapshot) == currentness.git_head
        and _read_index_revision(connection, workspace.workspace_id) == currentness.index_revision
    )


def _read_snapshot(workspace: WorkspaceRecord, deadline: float) -> WorkspaceGitSnapshot:
    try:
        return read_workspace_change_snapshot(workspace, deadline=deadline)
    except WorkspaceWatchError as exc:
        raise SearchCurrentnessError(
            "Project search could not inspect current Workspace state"
        ) from exc


def _read_persisted_state(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> _PersistedSearchState | None:
    row = connection.execute(
        """
        SELECT git_head, change_token, index_revision
        FROM workspace_search_index_state
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        return None
    git_head, change_token, index_revision = row
    if (
        not isinstance(git_head, str)
        or not git_head
        or not isinstance(change_token, str)
        or len(change_token) != 64
        or isinstance(index_revision, bool)
        or not isinstance(index_revision, int)
        or index_revision <= 0
    ):
        raise sqlite3.DatabaseError("invalid persisted Project search currentness state")
    dirty_rows = connection.execute(
        """
        SELECT relative_path
        FROM workspace_search_index_dirty_paths
        WHERE workspace_id = ?
        ORDER BY relative_path
        """,
        (workspace_id,),
    ).fetchall()
    dirty_paths: list[str] = []
    for (relative_path,) in dirty_rows:
        if not isinstance(relative_path, str) or not relative_path:
            raise sqlite3.DatabaseError("invalid persisted Project search dirty path")
        dirty_paths.append(relative_path)
    return _PersistedSearchState(git_head, change_token, index_revision, tuple(dirty_paths))


def _write_persisted_state(
    connection: sqlite3.Connection,
    workspace_id: str,
    snapshot: WorkspaceGitSnapshot,
) -> None:
    git_head = _snapshot_head_text(snapshot)
    index_revision = _read_index_revision(connection, workspace_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO workspace_search_index_state(
                workspace_id, git_head, change_token, index_revision
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                git_head = excluded.git_head,
                change_token = excluded.change_token,
                index_revision = excluded.index_revision
            """,
            (workspace_id, git_head, snapshot.token, index_revision),
        )
        connection.execute(
            "DELETE FROM workspace_search_index_dirty_paths WHERE workspace_id = ?",
            (workspace_id,),
        )
        connection.executemany(
            """
            INSERT INTO workspace_search_index_dirty_paths(workspace_id, relative_path)
            VALUES (?, ?)
            """,
            ((workspace_id, path) for path in snapshot.dirty_paths),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _read_index_revision(connection: sqlite3.Connection, workspace_id: str) -> int:
    row = connection.execute(
        """
        SELECT index_revision
        FROM workspace_index_reconcile
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] <= 0:
        raise SearchCurrentnessError("Workspace Structural Index has no valid reconcile revision")
    return row[0]


def _reconcile_paths(
    workspace_root: Path,
    persisted: _PersistedSearchState | None,
    current: WorkspaceGitSnapshot,
    path_delta: set[str],
    *,
    deadline: float,
) -> tuple[str, ...] | None:
    if persisted is None:
        return None

    selected = set(path_delta)
    selected.update(persisted.dirty_paths)
    selected.update(current.dirty_paths)
    current_head = _snapshot_head_text(current)
    if persisted.git_head != current_head:
        branch_delta = _git_changed_paths(
            workspace_root,
            persisted.git_head,
            current_head,
            deadline=deadline,
        )
        if branch_delta is None:
            return None
        selected.update(branch_delta)

    if len(selected) > MAX_INCREMENTAL_SCAN_PATHS:
        return None
    return tuple(sorted(selected, key=os.fsencode))


def _git_changed_paths(
    workspace_root: Path,
    old_head: str,
    new_head: str,
    *,
    deadline: float,
) -> tuple[str, ...] | None:
    if old_head == "unborn" or new_head == "unborn":
        return None
    if not (_is_git_object_id(old_head) and _is_git_object_id(new_head)):
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise SearchCurrentnessError("Project search currentness deadline exceeded")
    environment = _git_environment()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                old_head,
                new_head,
                "--",
            ],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=environment,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise SearchCurrentnessError("Project search branch-diff deadline exceeded") from exc
    except (FileNotFoundError, OSError) as exc:
        raise SearchCurrentnessError("Project search could not inspect branch delta") from exc
    if result.returncode != 0:
        return None
    paths: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        value = os.fsdecode(raw)
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise SearchCurrentnessError("Git returned an unsafe Project search branch-diff path")
        paths.append(value)
        if len(paths) > MAX_INCREMENTAL_SCAN_PATHS:
            return None
    return tuple(sorted(set(paths), key=os.fsencode))


def _snapshot_head_text(snapshot: WorkspaceGitSnapshot) -> str:
    try:
        value = snapshot.head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SearchCurrentnessError("Git returned a non-ASCII HEAD identity") from exc
    if not value:
        raise SearchCurrentnessError("Git returned an empty HEAD identity")
    return value


def _is_git_object_id(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdefABCDEF" for character in value)
