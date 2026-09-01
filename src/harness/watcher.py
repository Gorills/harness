from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from queue import Empty, SimpleQueue
from threading import Event, Lock
from time import monotonic

from harness.git_workspace import GitWorkspaceError, _git_environment
from harness.host_integration_state import HostIntegrationStateError
from harness.index import (
    MAX_INCREMENTAL_SCAN_PATHS,
    IndexedFileRecord,
    IndexingError,
    list_indexed_files,
    scan_workspace,
    scan_workspace_paths,
)
from harness.registry import RegistryError, WorkspaceRecord, list_workspaces
from harness.skill_runtime import (
    SkillRuntimeError,
    active_skill_profiles_for_runtime,
    reconcile_workspace_skills,
)
from harness.storage import connect_database

DEFAULT_WATCH_POLL_SECONDS = 0.5
DEFAULT_WATCH_DEBOUNCE_SECONDS = 0.3
DEFAULT_WATCH_FULL_RECONCILE_SECONDS = 300.0
DEFAULT_WATCH_RETRY_SECONDS = 1.0
DEFAULT_WATCH_TOKEN_DEADLINE_SECONDS = 5.0
DEFAULT_WATCH_SCAN_DEADLINE_SECONDS = 30.0
WATCH_METADATA_FILE_SAMPLE_LIMIT = 128
WATCH_IDLE_WORKSPACE_SAMPLE_LIMIT = 2


class WorkspaceWatchError(RuntimeError):
    """Raised when a filesystem-change hint cannot be sampled safely."""


@dataclass(frozen=True, slots=True)
class WorkspaceGitSnapshot:
    """Bounded Git confirmation state sampled only after a cheap metadata invalidation."""

    token: str
    head: bytes
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceMetadataSnapshot:
    """Subprocess-free global and bounded file-metadata watcher hints."""

    global_token: str
    file_token: str


@dataclass(frozen=True, slots=True)
class IdleWorkspaceMetadata:
    """One idle poll sample: Git control plus one directory shard and one file shard."""

    git_token: str
    directory_token: str
    file_token: str


@dataclass(slots=True)
class _WorkspaceWatchState:
    workspace: WorkspaceRecord
    metadata_git_token: str | None
    metadata_directory_tokens: dict[int, str]
    metadata_file_tokens: dict[int, str]
    metadata_directory_shard_count: int
    metadata_shard_count: int
    next_directory_shard: int
    next_metadata_shard: int
    metadata_directories: tuple[str, ...]
    indexed_files: tuple[IndexedFileRecord, ...]
    git_snapshot: WorkspaceGitSnapshot | None
    pending_since: float | None
    last_reconciled_at: float
    retry_after: float
    force_full_reconcile: bool


class WorkspaceWatcher:
    """Coalesce Workspace change hints into authoritative deterministic scans."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        scan_lock: Lock,
        *,
        debounce_seconds: float = DEFAULT_WATCH_DEBOUNCE_SECONDS,
        full_reconcile_seconds: float = DEFAULT_WATCH_FULL_RECONCILE_SECONDS,
        retry_seconds: float = DEFAULT_WATCH_RETRY_SECONDS,
        token_deadline_seconds: float = DEFAULT_WATCH_TOKEN_DEADLINE_SECONDS,
        scan_deadline_seconds: float = DEFAULT_WATCH_SCAN_DEADLINE_SECONDS,
        invalidations: SimpleQueue[str] | None = None,
        skill_profiles_provider: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        for name, value in (
            ("debounce_seconds", debounce_seconds),
            ("full_reconcile_seconds", full_reconcile_seconds),
            ("retry_seconds", retry_seconds),
            ("token_deadline_seconds", token_deadline_seconds),
            ("scan_deadline_seconds", scan_deadline_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self._connection = connection
        self._scan_lock = scan_lock
        self._debounce_seconds = debounce_seconds
        self._full_reconcile_seconds = full_reconcile_seconds
        self._retry_seconds = retry_seconds
        self._token_deadline_seconds = token_deadline_seconds
        self._scan_deadline_seconds = scan_deadline_seconds
        self._invalidations = invalidations
        self._skill_profiles_provider = skill_profiles_provider
        self._forced_reconcile_ids: set[str] = set()
        self._states: dict[str, _WorkspaceWatchState] = {}
        self._next_idle_index = 0

    def poll(
        self,
        *,
        now: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int:
        """Sample registered Workspaces once and reconcile any settled invalidations."""
        sampled_at = monotonic() if now is None else now
        if stop_requested is not None and stop_requested():
            return 0
        self._drain_invalidations(sampled_at)
        workspaces = list_workspaces(self._connection)
        active_ids = {workspace.workspace_id for workspace in workspaces}
        for workspace_id in tuple(self._states):
            if workspace_id not in active_ids:
                del self._states[workspace_id]

        reconciled = 0
        established: list[tuple[WorkspaceRecord, _WorkspaceWatchState]] = []
        for workspace in workspaces:
            if stop_requested is not None and stop_requested():
                return reconciled
            state = self._states.get(workspace.workspace_id)
            if state is None:
                state = self._initial_state(workspace, sampled_at)
                if workspace.workspace_id in self._forced_reconcile_ids:
                    self._forced_reconcile_ids.discard(workspace.workspace_id)
                self._states[workspace.workspace_id] = state
                continue
            if state.workspace != workspace:
                self._states[workspace.workspace_id] = self._initial_state(
                    workspace,
                    sampled_at,
                )
                continue
            established.append((workspace, state))

        if established:
            sample_limit = min(WATCH_IDLE_WORKSPACE_SAMPLE_LIMIT, len(established))
            start = self._next_idle_index % len(established)
            for offset in range(sample_limit):
                workspace, state = established[(start + offset) % len(established)]
                self._sample_change(workspace, state, sampled_at)
            self._next_idle_index = start + sample_limit

        for workspace, state in established:
            if stop_requested is not None and stop_requested():
                return reconciled
            if sampled_at - state.last_reconciled_at >= self._full_reconcile_seconds:
                if state.pending_since is None:
                    state.pending_since = sampled_at

            if state.pending_since is None:
                continue
            if sampled_at < state.retry_after:
                continue
            if sampled_at - state.pending_since < self._debounce_seconds:
                continue

            if not self._scan_lock.acquire(blocking=False):
                continue
            try:
                try:
                    trigger_snapshot = read_workspace_change_snapshot(
                        workspace,
                        deadline=monotonic() + self._token_deadline_seconds,
                    )
                    incremental_paths = _incremental_paths(
                        state.git_snapshot,
                        trigger_snapshot,
                        force_full=state.force_full_reconcile,
                    )
                    if _defer_unbounded_full_scan(
                        state,
                        trigger_snapshot,
                        incremental_paths,
                        sampled_at,
                        self._full_reconcile_seconds,
                    ):
                        state.pending_since = None
                        state.retry_after = 0.0
                        state.git_snapshot = trigger_snapshot
                        continue
                    scan_performed = incremental_paths is None or bool(incremental_paths)
                    if incremental_paths is None:
                        scan_workspace(
                            self._connection,
                            workspace.workspace_id,
                            deadline=monotonic() + self._scan_deadline_seconds,
                        )
                    elif incremental_paths:
                        scan_workspace_paths(
                            self._connection,
                            workspace.workspace_id,
                            incremental_paths,
                            deadline=monotonic() + self._scan_deadline_seconds,
                        )
                except _WATCH_RETRY_ERRORS:
                    state.retry_after = sampled_at + self._retry_seconds
                    return reconciled

                try:
                    if scan_performed and self._skill_profiles_provider is not None:
                        profiles = self._skill_profiles_provider()
                        if profiles:
                            reconcile_workspace_skills(
                                self._connection,
                                workspace.workspace_id,
                                profiles,
                            )
                except _SKILL_RECONCILIATION_ERRORS:
                    # The authoritative index is current even when an ownership collision or
                    # malformed host-intent file prevents projection. Doctor reports the stale
                    # skill integration, while a later invalidation/full pass retries it without
                    # hot-looping the full Workspace scan.
                    pass
            finally:
                self._scan_lock.release()

            if scan_performed:
                reconciled += 1
            state.last_reconciled_at = sampled_at
            state.pending_since = None
            state.retry_after = 0.0
            state.force_full_reconcile = False
            if stop_requested is not None and stop_requested():
                return reconciled
            try:
                post_snapshot = read_workspace_change_snapshot(
                    workspace,
                    deadline=monotonic() + self._token_deadline_seconds,
                )
                self._reset_metadata_state(workspace, state)
            except WorkspaceWatchError:
                _clear_idle_metadata(state)
                state.git_snapshot = None
                state.pending_since = sampled_at
                state.retry_after = sampled_at + self._retry_seconds
                state.force_full_reconcile = True
                return reconciled
            state.git_snapshot = post_snapshot
            if post_snapshot.token != trigger_snapshot.token:
                if trigger_snapshot.head != post_snapshot.head:
                    state.pending_since = sampled_at
                    state.force_full_reconcile = True
                elif not _oversized_dirty_set(trigger_snapshot, post_snapshot):
                    state.pending_since = sampled_at
            return reconciled

        return reconciled

    def _drain_invalidations(self, sampled_at: float) -> None:
        if self._invalidations is None:
            return
        while True:
            try:
                workspace_id = self._invalidations.get_nowait()
            except Empty:
                return
            state = self._states.get(workspace_id)
            if state is None:
                self._forced_reconcile_ids.add(workspace_id)
            else:
                state.pending_since = sampled_at
                state.retry_after = 0.0
                state.force_full_reconcile = True

    def _initial_state(self, workspace: WorkspaceRecord, sampled_at: float) -> _WorkspaceWatchState:
        indexed_files = list_indexed_files(self._connection, workspace.workspace_id)
        file_shard_count = _metadata_shard_count(len(indexed_files))
        try:
            directories = list_workspace_metadata_directories(
                workspace.workspace_root,
                deadline=monotonic() + self._token_deadline_seconds,
            )
            idle = read_idle_workspace_metadata(
                workspace,
                _metadata_shard(indexed_files, 0),
                _metadata_shard(directories, 0),
                deadline=monotonic() + self._token_deadline_seconds,
            )
        except WorkspaceWatchError:
            directories = ()
            idle = None
        directory_shard_count = _metadata_shard_count(len(directories))
        return _WorkspaceWatchState(
            workspace=workspace,
            metadata_git_token=None if idle is None else idle.git_token,
            metadata_directory_tokens={} if idle is None else {0: idle.directory_token},
            metadata_file_tokens={} if idle is None else {0: idle.file_token},
            metadata_directory_shard_count=directory_shard_count,
            metadata_shard_count=file_shard_count,
            next_directory_shard=0 if directory_shard_count == 1 else 1,
            next_metadata_shard=0 if file_shard_count == 1 else 1,
            metadata_directories=directories,
            indexed_files=indexed_files,
            git_snapshot=None,
            pending_since=sampled_at,
            last_reconciled_at=sampled_at,
            retry_after=0.0,
            force_full_reconcile=True,
        )

    def _sample_change(
        self,
        workspace: WorkspaceRecord,
        state: _WorkspaceWatchState,
        sampled_at: float,
    ) -> None:
        file_shard_count = _metadata_shard_count(len(state.indexed_files))
        directory_shard_count = _metadata_shard_count(len(state.metadata_directories))
        file_shard_index = state.next_metadata_shard % file_shard_count
        directory_shard_index = state.next_directory_shard % directory_shard_count
        try:
            idle = read_idle_workspace_metadata(
                workspace,
                _metadata_shard(state.indexed_files, file_shard_index),
                _metadata_shard(state.metadata_directories, directory_shard_index),
                deadline=monotonic() + self._token_deadline_seconds,
            )
        except WorkspaceWatchError:
            _clear_idle_metadata(state)
            if state.pending_since is None:
                state.pending_since = sampled_at
            state.force_full_reconcile = True
            return

        changed = (
            state.metadata_git_token is not None and idle.git_token != state.metadata_git_token
        )
        if state.metadata_shard_count != file_shard_count:
            state.metadata_file_tokens.clear()
            state.metadata_shard_count = file_shard_count
            changed = True
        if state.metadata_directory_shard_count != directory_shard_count:
            state.metadata_directory_tokens.clear()
            state.metadata_directory_shard_count = directory_shard_count
            changed = True
        prior_file_token = state.metadata_file_tokens.get(file_shard_index)
        if prior_file_token is not None and idle.file_token != prior_file_token:
            changed = True
        prior_directory_token = state.metadata_directory_tokens.get(directory_shard_index)
        if prior_directory_token is not None and idle.directory_token != prior_directory_token:
            changed = True
        state.metadata_git_token = idle.git_token
        state.metadata_file_tokens[file_shard_index] = idle.file_token
        state.metadata_directory_tokens[directory_shard_index] = idle.directory_token
        state.next_metadata_shard = (file_shard_index + 1) % file_shard_count
        state.next_directory_shard = (directory_shard_index + 1) % directory_shard_count
        if not changed:
            return
        state.pending_since = sampled_at

    def _reset_metadata_state(
        self,
        workspace: WorkspaceRecord,
        state: _WorkspaceWatchState,
    ) -> None:
        indexed_files = list_indexed_files(self._connection, workspace.workspace_id)
        state.indexed_files = indexed_files
        file_shard_count = _metadata_shard_count(len(indexed_files))
        directories = list_workspace_metadata_directories(
            workspace.workspace_root,
            deadline=monotonic() + self._token_deadline_seconds,
        )
        directory_shard_count = _metadata_shard_count(len(directories))
        deadline = monotonic() + self._token_deadline_seconds
        idle = read_idle_workspace_metadata(
            workspace,
            _metadata_shard(indexed_files, 0),
            _metadata_shard(directories, 0),
            deadline=deadline,
        )
        file_tokens = {0: idle.file_token}
        for shard_index in range(1, file_shard_count):
            file_tokens[shard_index] = _file_metadata_token(
                workspace,
                _metadata_shard(indexed_files, shard_index),
                deadline=deadline,
            )
        directory_tokens = {0: idle.directory_token}
        for shard_index in range(1, directory_shard_count):
            directory_tokens[shard_index] = _directory_metadata_token(
                workspace,
                _metadata_shard(directories, shard_index),
                deadline=deadline,
            )
        state.metadata_git_token = idle.git_token
        state.metadata_file_tokens = file_tokens
        state.metadata_directory_tokens = directory_tokens
        state.metadata_shard_count = file_shard_count
        state.metadata_directory_shard_count = directory_shard_count
        state.next_metadata_shard = 0 if file_shard_count == 1 else 1
        state.next_directory_shard = 0 if directory_shard_count == 1 else 1
        state.metadata_directories = directories


def run_workspace_watcher(
    database_path: Path,
    stop_event: Event,
    scan_lock: Lock,
    *,
    poll_seconds: float = DEFAULT_WATCH_POLL_SECONDS,
    debounce_seconds: float = DEFAULT_WATCH_DEBOUNCE_SECONDS,
    full_reconcile_seconds: float = DEFAULT_WATCH_FULL_RECONCILE_SECONDS,
    retry_seconds: float = DEFAULT_WATCH_RETRY_SECONDS,
    token_deadline_seconds: float = DEFAULT_WATCH_TOKEN_DEADLINE_SECONDS,
    scan_deadline_seconds: float = DEFAULT_WATCH_SCAN_DEADLINE_SECONDS,
    invalidations: SimpleQueue[str] | None = None,
) -> None:
    """Run the per-daemon Workspace watcher until ``stop_event`` is set."""
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    connection = connect_database(database_path)
    try:

        def skill_profiles() -> tuple[str, ...]:
            return active_skill_profiles_for_runtime(database_path)

        watcher = WorkspaceWatcher(
            connection,
            scan_lock,
            debounce_seconds=debounce_seconds,
            full_reconcile_seconds=full_reconcile_seconds,
            retry_seconds=retry_seconds,
            token_deadline_seconds=token_deadline_seconds,
            scan_deadline_seconds=scan_deadline_seconds,
            invalidations=invalidations,
            skill_profiles_provider=skill_profiles,
        )
        while not stop_event.is_set():
            try:
                watcher.poll(stop_requested=stop_event.is_set)
            except _WATCH_RETRY_ERRORS:
                pass
            stop_event.wait(poll_seconds)
    finally:
        connection.close()


def read_workspace_change_snapshot(
    workspace: WorkspaceRecord, *, deadline: float
) -> WorkspaceGitSnapshot:
    """Return Git confirmation state after a cheap Workspace metadata invalidation."""
    _require_watch_deadline(deadline)
    status = _git_status_bytes(workspace.workspace_root, deadline=deadline)
    head = _git_head_bytes(workspace.workspace_root, deadline=deadline)
    dirty_paths = _dirty_paths_from_status(status)

    digest = hashlib.sha256()
    digest.update(b"harness-workspace-watch-v1\0")
    digest.update(len(head).to_bytes(4, "big"))
    digest.update(head)
    digest.update(status)
    _digest_entry_identity(digest, workspace.workspace_root, ".harnessignore")
    for relative_path in sorted(dirty_paths, key=os.fsencode):
        _require_watch_deadline(deadline)
        _digest_entry_identity(digest, workspace.workspace_root, relative_path)
    _require_watch_deadline(deadline)
    return WorkspaceGitSnapshot(
        token=digest.hexdigest(),
        head=head,
        dirty_paths=tuple(sorted(dirty_paths, key=os.fsencode)),
    )


def read_workspace_change_token(workspace: WorkspaceRecord, *, deadline: float) -> str:
    """Return the compatibility Git confirmation token used by focused tests and diagnostics."""
    return read_workspace_change_snapshot(workspace, deadline=deadline).token


def read_workspace_metadata_token(
    workspace: WorkspaceRecord,
    indexed_files: Sequence[IndexedFileRecord],
    *,
    deadline: float,
    directory_paths: Sequence[str] | None = None,
) -> str:
    """Return one combined subprocess-free metadata hint for benchmarks/diagnostics."""
    snapshot = read_workspace_metadata_snapshot(
        workspace,
        indexed_files,
        deadline=deadline,
        directory_paths=directory_paths,
    )
    digest = hashlib.sha256()
    digest.update(snapshot.global_token.encode("ascii"))
    digest.update(snapshot.file_token.encode("ascii"))
    return digest.hexdigest()


def read_workspace_metadata_snapshot(
    workspace: WorkspaceRecord,
    indexed_files: Sequence[IndexedFileRecord],
    *,
    deadline: float,
    directory_paths: Sequence[str] | None = None,
) -> WorkspaceMetadataSnapshot:
    """Return global topology/Git and selected-file metadata hints without subprocesses."""
    _require_watch_deadline(deadline)
    directories = (
        list_workspace_metadata_directories(workspace.workspace_root, deadline=deadline)
        if directory_paths is None
        else directory_paths
    )
    idle = read_idle_workspace_metadata(workspace, indexed_files, directories, deadline=deadline)
    global_digest = hashlib.sha256()
    global_digest.update(b"harness-workspace-metadata-global-v1\0")
    global_digest.update(idle.git_token.encode("ascii"))
    global_digest.update(idle.directory_token.encode("ascii"))
    _require_watch_deadline(deadline)
    return WorkspaceMetadataSnapshot(
        global_token=global_digest.hexdigest(),
        file_token=idle.file_token,
    )


def read_idle_workspace_metadata(
    workspace: WorkspaceRecord,
    indexed_files: Sequence[IndexedFileRecord],
    directory_paths: Sequence[str],
    *,
    deadline: float,
) -> IdleWorkspaceMetadata:
    """Sample Git control plus the supplied directory and file shards without subprocesses."""
    _require_watch_deadline(deadline)
    return IdleWorkspaceMetadata(
        git_token=_git_control_token(workspace, deadline=deadline),
        directory_token=_directory_metadata_token(workspace, directory_paths, deadline=deadline),
        file_token=_file_metadata_token(workspace, indexed_files, deadline=deadline),
    )


def _file_metadata_token(
    workspace: WorkspaceRecord,
    indexed_files: Sequence[IndexedFileRecord],
    *,
    deadline: float,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"harness-workspace-metadata-files-v1\0")
    for record in indexed_files:
        if record.workspace_id != workspace.workspace_id:
            raise WorkspaceWatchError("Workspace watcher received a foreign indexed file")
        _require_watch_deadline(deadline)
        _digest_entry_identity(digest, workspace.workspace_root, record.relative_path)
    return digest.hexdigest()


def _directory_metadata_token(
    workspace: WorkspaceRecord,
    directory_paths: Sequence[str],
    *,
    deadline: float,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"harness-workspace-metadata-directories-v1\0")
    _digest_workspace_directories(
        digest,
        workspace.workspace_root,
        directory_paths,
        deadline=deadline,
    )
    return digest.hexdigest()


def _git_control_token(workspace: WorkspaceRecord, *, deadline: float) -> str:
    digest = hashlib.sha256()
    digest.update(b"harness-workspace-metadata-git-v1\0")
    _digest_git_control_state(digest, workspace, deadline=deadline)
    return digest.hexdigest()


def _clear_idle_metadata(state: _WorkspaceWatchState) -> None:
    state.metadata_git_token = None
    state.metadata_directory_tokens.clear()
    state.metadata_file_tokens.clear()


def _metadata_shard_count(indexed_file_count: int) -> int:
    return max(
        1,
        (indexed_file_count + WATCH_METADATA_FILE_SAMPLE_LIMIT - 1)
        // WATCH_METADATA_FILE_SAMPLE_LIMIT,
    )


def _metadata_shard[T](items: Sequence[T], shard_index: int) -> Sequence[T]:
    start = shard_index * WATCH_METADATA_FILE_SAMPLE_LIMIT
    return items[start : start + WATCH_METADATA_FILE_SAMPLE_LIMIT]


def _incremental_paths(
    previous: WorkspaceGitSnapshot | None,
    current: WorkspaceGitSnapshot,
    *,
    force_full: bool,
) -> tuple[str, ...] | None:
    if force_full or previous is None or previous.head != current.head:
        return None
    if previous.token == current.token:
        return ()
    paths = tuple(sorted(set(previous.dirty_paths) | set(current.dirty_paths), key=os.fsencode))
    if not paths or ".harnessignore" in paths or len(paths) > MAX_INCREMENTAL_SCAN_PATHS:
        return None
    return paths


def _oversized_dirty_set(*snapshots: WorkspaceGitSnapshot) -> bool:
    return all(len(snapshot.dirty_paths) > MAX_INCREMENTAL_SCAN_PATHS for snapshot in snapshots)


def _defer_unbounded_full_scan(
    state: _WorkspaceWatchState,
    trigger_snapshot: WorkspaceGitSnapshot,
    incremental_paths: tuple[str, ...] | None,
    sampled_at: float,
    full_reconcile_seconds: float,
) -> bool:
    if incremental_paths is not None or state.force_full_reconcile:
        return False
    previous = state.git_snapshot
    if previous is None or previous.head != trigger_snapshot.head:
        return False
    if not _oversized_dirty_set(previous, trigger_snapshot):
        return False
    return sampled_at - state.last_reconciled_at < full_reconcile_seconds


_WATCH_DIRECTORY_EXCLUDES = frozenset(
    {".git", "node_modules", "vendor", "dist", "build", "target", "caches"}
)


def list_workspace_metadata_directories(
    workspace_root: Path,
    *,
    deadline: float,
) -> tuple[str, ...]:
    try:
        root_stat = workspace_root.lstat()
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect the Workspace root") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceWatchError("Workspace watcher root is not a real directory")
    directories: list[str] = [""]
    pending: list[tuple[str, Path]] = [("", workspace_root)]
    while pending:
        _require_watch_deadline(deadline)
        relative, directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            if relative and _is_inaccessible_watch_entry(exc):
                continue
            raise WorkspaceWatchError("Workspace watcher could not enumerate directories") from exc
        for entry in entries:
            _require_watch_deadline(deadline)
            if entry.name in _WATCH_DIRECTORY_EXCLUDES:
                continue
            child_relative = entry.name if not relative else f"{relative}/{entry.name}"
            child_path = Path(entry.path)
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                if _is_inaccessible_watch_entry(exc):
                    continue
                raise WorkspaceWatchError(
                    "Workspace watcher could not inspect a directory"
                ) from exc
            if not is_directory:
                continue
            directories.append(child_relative)
            pending.append((child_relative, child_path))
    return tuple(sorted(directories, key=os.fsencode))


def _digest_workspace_directories(
    digest: hashlib._Hash,
    workspace_root: Path,
    directory_paths: Sequence[str],
    *,
    deadline: float,
) -> None:
    for relative in directory_paths:
        _require_watch_deadline(deadline)
        path = workspace_root if not relative else workspace_root / relative
        _digest_absolute_identity(digest, relative or ".", path)


def _digest_git_control_state(
    digest: hashlib._Hash,
    workspace: WorkspaceRecord,
    *,
    deadline: float,
) -> None:
    git_dir = _workspace_git_dir(workspace)
    for label, path in (
        ("git/HEAD", git_dir / "HEAD"),
        ("git/index", git_dir / "index"),
        ("git/packed-refs", workspace.git_common_dir / "packed-refs"),
        ("git/info/exclude", workspace.git_common_dir / "info" / "exclude"),
    ):
        _require_watch_deadline(deadline)
        _digest_absolute_identity(digest, label, path)
    _digest_control_tree(
        digest,
        workspace.git_common_dir / "refs",
        label="git/refs",
        deadline=deadline,
    )


def _workspace_git_dir(workspace: WorkspaceRecord) -> Path:
    dotgit = workspace.workspace_root / ".git"
    try:
        if dotgit.is_dir():
            return dotgit.resolve(strict=True)
        raw = dotgit.read_bytes()
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not resolve the Git directory") from exc
    prefix = b"gitdir: "
    if not raw.startswith(prefix) or b"\x00" in raw or len(raw) > 4096:
        raise WorkspaceWatchError("Workspace watcher found a malformed .git file")
    value = os.fsdecode(raw[len(prefix) :].strip())
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace.workspace_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceWatchError("Workspace watcher could not resolve the Git directory") from exc
    if not resolved.is_dir():
        raise WorkspaceWatchError("Workspace watcher Git directory is not a directory")
    if not resolved.is_relative_to(workspace.git_common_dir):
        raise WorkspaceWatchError(
            "Workspace watcher Git directory escaped the registered common dir"
        )
    return resolved


def _digest_control_tree(
    digest: hashlib._Hash,
    root: Path,
    *,
    label: str,
    deadline: float,
) -> None:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        _digest_missing(digest, label)
        return
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect Git refs") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceWatchError("Workspace watcher Git refs path is not a real directory")
    _digest_stat(digest, label, root_stat)
    pending: list[tuple[str, Path]] = [(label, root)]
    while pending:
        _require_watch_deadline(deadline)
        parent_label, directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            if parent_label != label and _is_inaccessible_watch_entry(exc):
                continue
            raise WorkspaceWatchError("Workspace watcher could not enumerate Git refs") from exc
        for entry in sorted(entries, key=lambda item: os.fsencode(item.name)):
            entry_label = f"{parent_label}/{entry.name}"
            path = Path(entry.path)
            _digest_absolute_identity(digest, entry_label, path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append((entry_label, path))
            except OSError as exc:
                raise WorkspaceWatchError("Workspace watcher could not inspect Git refs") from exc


def _digest_absolute_identity(digest: hashlib._Hash, label: str, path: Path) -> None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        _digest_missing(digest, label)
        return
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect metadata") from exc
    _digest_stat(digest, label, entry)


def _digest_missing(digest: hashlib._Hash, label: str) -> None:
    raw_label = os.fsencode(label)
    digest.update(len(raw_label).to_bytes(4, "big"))
    digest.update(raw_label)
    digest.update(b"missing\0")


def _digest_stat(digest: hashlib._Hash, label: str, entry: os.stat_result) -> None:
    raw_label = os.fsencode(label)
    digest.update(len(raw_label).to_bytes(4, "big"))
    digest.update(raw_label)
    digest.update(b"present\0")
    for value in (
        stat.S_IFMT(entry.st_mode),
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    ):
        digest.update(str(value).encode("ascii"))
        digest.update(b"\0")


def _git_head_bytes(workspace_root: Path, *, deadline: float) -> bytes:
    environment = _git_environment()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=environment,
            timeout=_remaining_watch_seconds(deadline),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceWatchError("Workspace watcher Git HEAD lookup timed out") from exc
    except FileNotFoundError as exc:
        raise WorkspaceWatchError("Git executable is not available for Workspace watcher") from exc
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect Git HEAD") from exc
    if result.returncode == 0:
        return result.stdout.strip()
    # An unborn branch is a valid Workspace state; Git status still carries the branch identity.
    if result.returncode == 1 and not result.stdout:
        return b"unborn"
    raise WorkspaceWatchError("Workspace watcher could not inspect Git HEAD")


def _git_status_bytes(workspace_root: Path, *, deadline: float) -> bytes:
    environment = _git_environment()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--branch",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=environment,
            timeout=_remaining_watch_seconds(deadline),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceWatchError("Workspace watcher Git status timed out") from exc
    except FileNotFoundError as exc:
        raise WorkspaceWatchError("Git executable is not available for Workspace watcher") from exc
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect Git status") from exc
    if result.returncode != 0:
        raise WorkspaceWatchError("Workspace watcher could not inspect Git status")
    return result.stdout


def _dirty_paths_from_status(status: bytes) -> tuple[str, ...]:
    tokens = status.split(b"\0")
    dirty_paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token or token.startswith(b"## "):
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise WorkspaceWatchError("Workspace watcher received malformed Git status")
        status_code = token[:2]
        dirty_paths.append(_decode_status_path(token[3:]))
        if b"R" in status_code or b"C" in status_code:
            if index >= len(tokens) or not tokens[index]:
                raise WorkspaceWatchError("Workspace watcher received incomplete rename status")
            dirty_paths.append(_decode_status_path(tokens[index]))
            index += 1
    return tuple(set(dirty_paths))


def _decode_status_path(raw_path: bytes) -> str:
    value = os.fsdecode(raw_path)
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise WorkspaceWatchError("Workspace watcher received unsafe Git status path")
    return value


def _is_inaccessible_watch_entry(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM}


def _digest_entry_identity(digest: hashlib._Hash, workspace_root: Path, relative_path: str) -> None:
    path = workspace_root / relative_path
    raw_path = os.fsencode(relative_path)
    digest.update(len(raw_path).to_bytes(4, "big"))
    digest.update(raw_path)
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except (OSError, RuntimeError) as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect changed path") from exc
    if not parent.is_relative_to(workspace_root):
        raise WorkspaceWatchError("Workspace watcher path escapes through a symlinked parent")
    try:
        entry = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise WorkspaceWatchError("Workspace watcher could not inspect changed path") from exc

    digest.update(b"present\0")
    for value in (
        stat.S_IFMT(entry.st_mode),
        entry.st_dev,
        entry.st_ino,
        entry.st_size,
        entry.st_mtime_ns,
        entry.st_ctime_ns,
    ):
        digest.update(str(value).encode("ascii"))
        digest.update(b"\0")
    if stat.S_ISLNK(entry.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise WorkspaceWatchError(
                "Workspace watcher could not inspect changed symlink"
            ) from exc
        raw_target = os.fsencode(target)
        digest.update(len(raw_target).to_bytes(4, "big"))
        digest.update(raw_target)


def _remaining_watch_seconds(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise WorkspaceWatchError("Workspace watcher sampling deadline exceeded")
    return remaining


def _require_watch_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise WorkspaceWatchError("Workspace watcher sampling deadline exceeded")


_WATCH_RETRY_ERRORS = (
    WorkspaceWatchError,
    IndexingError,
    RegistryError,
    GitWorkspaceError,
    sqlite3.DatabaseError,
    OSError,
)

_SKILL_RECONCILIATION_ERRORS = (
    HostIntegrationStateError,
    SkillRuntimeError,
)
