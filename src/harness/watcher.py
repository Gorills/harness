from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from queue import Empty, SimpleQueue
from threading import Event, Lock
from time import monotonic

from harness.git_workspace import GitWorkspaceError, _git_environment
from harness.index import IndexingError, scan_workspace
from harness.registry import RegistryError, WorkspaceRecord, list_workspaces
from harness.storage import connect_database

DEFAULT_WATCH_POLL_SECONDS = 0.5
DEFAULT_WATCH_DEBOUNCE_SECONDS = 0.3
DEFAULT_WATCH_FULL_RECONCILE_SECONDS = 300.0
DEFAULT_WATCH_RETRY_SECONDS = 1.0
DEFAULT_WATCH_TOKEN_DEADLINE_SECONDS = 5.0
DEFAULT_WATCH_SCAN_DEADLINE_SECONDS = 30.0


class WorkspaceWatchError(RuntimeError):
    """Raised when a filesystem-change hint cannot be sampled safely."""


@dataclass(slots=True)
class _WorkspaceWatchState:
    token: str | None
    pending_since: float | None
    last_reconciled_at: float
    retry_after: float


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
        self._forced_reconcile_ids: set[str] = set()
        self._states: dict[str, _WorkspaceWatchState] = {}

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

            self._sample_change(workspace, state, sampled_at)
            if sampled_at - state.last_reconciled_at >= self._full_reconcile_seconds:
                if state.pending_since is None:
                    state.pending_since = sampled_at

            if state.pending_since is None:
                continue
            if sampled_at < state.retry_after:
                continue
            if sampled_at - state.pending_since < self._debounce_seconds:
                continue

            trigger_token = state.token
            if not self._scan_lock.acquire(blocking=False):
                continue
            try:
                scan_workspace(
                    self._connection,
                    workspace.workspace_id,
                    deadline=monotonic() + self._scan_deadline_seconds,
                )
            except _WATCH_RETRY_ERRORS:
                state.retry_after = sampled_at + self._retry_seconds
                return reconciled
            finally:
                self._scan_lock.release()

            reconciled += 1
            state.last_reconciled_at = sampled_at
            state.pending_since = None
            state.retry_after = 0.0
            if stop_requested is not None and stop_requested():
                return reconciled
            try:
                post_token = read_workspace_change_token(
                    workspace,
                    deadline=monotonic() + self._token_deadline_seconds,
                )
            except WorkspaceWatchError:
                state.token = None
                state.pending_since = sampled_at
                state.retry_after = sampled_at + self._retry_seconds
                return reconciled
            state.token = post_token
            if trigger_token is None or post_token != trigger_token:
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

    def _initial_state(self, workspace: WorkspaceRecord, sampled_at: float) -> _WorkspaceWatchState:
        try:
            token = read_workspace_change_token(
                workspace,
                deadline=monotonic() + self._token_deadline_seconds,
            )
        except WorkspaceWatchError:
            token = None
        return _WorkspaceWatchState(
            token=token,
            pending_since=sampled_at,
            last_reconciled_at=sampled_at,
            retry_after=0.0,
        )

    def _sample_change(
        self,
        workspace: WorkspaceRecord,
        state: _WorkspaceWatchState,
        sampled_at: float,
    ) -> None:
        try:
            token = read_workspace_change_token(
                workspace,
                deadline=monotonic() + self._token_deadline_seconds,
            )
        except WorkspaceWatchError:
            if state.pending_since is None:
                state.pending_since = sampled_at
            return

        if state.token is None:
            state.token = token
            if state.pending_since is None:
                state.pending_since = sampled_at
            return
        if token == state.token:
            return
        state.token = token
        state.pending_since = sampled_at


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
        watcher = WorkspaceWatcher(
            connection,
            scan_lock,
            debounce_seconds=debounce_seconds,
            full_reconcile_seconds=full_reconcile_seconds,
            retry_seconds=retry_seconds,
            token_deadline_seconds=token_deadline_seconds,
            scan_deadline_seconds=scan_deadline_seconds,
            invalidations=invalidations,
        )
        while not stop_event.is_set():
            try:
                watcher.poll(stop_requested=stop_event.is_set)
            except _WATCH_RETRY_ERRORS:
                pass
            stop_event.wait(poll_seconds)
    finally:
        connection.close()


def read_workspace_change_token(workspace: WorkspaceRecord, *, deadline: float) -> str:
    """Return a lightweight invalidation token for current Workspace/Git state.

    The token is only a watcher hint. A triggered update always calls ``scan_workspace`` so the
    filesystem and Git remain authoritative. A periodic full reconciliation repairs missed hints.
    """
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
    return digest.hexdigest()


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
