from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic

from harness.git_workspace import _git_environment, inspect_git_workspace_runtime_identity
from harness.index import list_indexed_files
from harness.registry import WorkspaceRecord, get_workspace

_BASELINE_TIMEOUT_SECONDS = 5.0
_HASH_CHUNK_BYTES = 128 * 1024


class TaskBaselineError(RuntimeError):
    """Base class for mechanical Task baseline capture failures."""


class TaskBaselineChangedError(TaskBaselineError):
    """Raised when Workspace Git/filesystem state changes during baseline capture."""


class TaskBaselineTimeoutError(TaskBaselineError):
    """Raised when bounded Task baseline capture exceeds its execution deadline."""


@dataclass(frozen=True, slots=True)
class TaskBaselineDirtyPath:
    """One pre-existing dirty Workspace path captured without storing source text."""

    relative_path: str
    original_relative_path: str | None
    status_code: str
    state_sha256: str


@dataclass(frozen=True, slots=True)
class TaskBaselineSnapshot:
    """Mechanical Workspace state captured before a new Task is persisted."""

    workspace_id: str
    head: str | None
    branch: str | None
    captured_at: str
    index_generation: int | None
    index_last_reconciled_at: str | None
    index_file_count: int
    index_snapshot_sha256: str
    dirty_paths: tuple[TaskBaselineDirtyPath, ...]


@dataclass(frozen=True, slots=True)
class TaskBaselineRecord:
    """Durable one-to-one baseline attached to one Harness Task."""

    task_id: str
    snapshot: TaskBaselineSnapshot


def capture_workspace_task_baseline(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> TaskBaselineSnapshot:
    """Capture one stable Git/index baseline for a registered Workspace."""
    deadline = monotonic() + _BASELINE_TIMEOUT_SECONDS
    workspace = get_workspace(connection, workspace_id)
    identity_before = inspect_git_workspace_runtime_identity(workspace.workspace_root)
    _require_registered_identity(workspace, identity_before.layout.workspace_root, identity_before.layout.git_common_dir)

    index_generation, index_last_reconciled_at = _read_index_reconciliation_state(
        connection, workspace_id
    )
    indexed_files = list_indexed_files(connection, workspace_id)
    index_snapshot_sha256 = _index_snapshot_sha256(indexed_files)

    first_git = _capture_git_state(workspace.workspace_root, deadline=deadline)
    second_git = _capture_git_state(workspace.workspace_root, deadline=deadline)
    if first_git != second_git:
        raise TaskBaselineChangedError("Workspace Git state changed during Task baseline capture")

    identity_after = inspect_git_workspace_runtime_identity(workspace.workspace_root)
    if identity_after != identity_before:
        raise TaskBaselineChangedError("Workspace Git identity changed during Task baseline capture")
    current_workspace = get_workspace(connection, workspace_id)
    if current_workspace != workspace:
        raise TaskBaselineChangedError("Workspace registry identity changed during Task baseline capture")

    return TaskBaselineSnapshot(
        workspace_id=workspace.workspace_id,
        head=first_git.head,
        branch=first_git.branch,
        captured_at=_utc_timestamp(now),
        index_generation=index_generation,
        index_last_reconciled_at=index_last_reconciled_at,
        index_file_count=len(indexed_files),
        index_snapshot_sha256=index_snapshot_sha256,
        dirty_paths=first_git.dirty_paths,
    )


def persist_task_baseline(
    connection: sqlite3.Connection,
    task_id: str,
    snapshot: TaskBaselineSnapshot,
) -> TaskBaselineRecord:
    """Persist one already-captured baseline inside the caller's Task transaction."""
    connection.execute(
        """
        INSERT INTO task_baselines(
            task_id,
            head,
            branch,
            captured_at,
            index_generation,
            index_last_reconciled_at,
            index_file_count,
            index_snapshot_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            snapshot.head,
            snapshot.branch,
            snapshot.captured_at,
            snapshot.index_generation,
            snapshot.index_last_reconciled_at,
            snapshot.index_file_count,
            snapshot.index_snapshot_sha256,
        ),
    )
    for dirty_path in snapshot.dirty_paths:
        connection.execute(
            """
            INSERT INTO task_baseline_dirty_paths(
                task_id,
                relative_path,
                original_relative_path,
                status_code,
                state_sha256
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                dirty_path.relative_path,
                dirty_path.original_relative_path,
                dirty_path.status_code,
                dirty_path.state_sha256,
            ),
        )
    return TaskBaselineRecord(task_id=task_id, snapshot=snapshot)


def get_task_baseline(connection: sqlite3.Connection, task_id: str) -> TaskBaselineRecord:
    """Load the durable mechanical baseline for one Task."""
    task_row = connection.execute(
        "SELECT workspace_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task_row is None:
        raise TaskBaselineError(f"task does not exist: {task_id}")
    workspace_id = task_row[0]
    if not isinstance(workspace_id, str) or not workspace_id:
        raise TaskBaselineError("task row has invalid persisted Workspace identity")

    row = connection.execute(
        """
        SELECT
            head,
            branch,
            captured_at,
            index_generation,
            index_last_reconciled_at,
            index_file_count,
            index_snapshot_sha256
        FROM task_baselines
        WHERE task_id = ?
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise TaskBaselineError(f"task baseline does not exist: {task_id}")

    dirty_rows = connection.execute(
        """
        SELECT relative_path, original_relative_path, status_code, state_sha256
        FROM task_baseline_dirty_paths
        WHERE task_id = ?
        ORDER BY relative_path
        """,
        (task_id,),
    ).fetchall()
    dirty_paths = tuple(_dirty_path_from_row(dirty_row) for dirty_row in dirty_rows)
    return TaskBaselineRecord(
        task_id=task_id,
        snapshot=_snapshot_from_row(workspace_id, row, dirty_paths),
    )


@dataclass(frozen=True, slots=True)
class _GitBaselineState:
    head: str | None
    branch: str | None
    dirty_paths: tuple[TaskBaselineDirtyPath, ...]


def _capture_git_state(workspace_root: Path, *, deadline: float) -> _GitBaselineState:
    head = _git_head(workspace_root, deadline=deadline)
    branch = _git_branch(workspace_root, deadline=deadline)
    status = _git_status(workspace_root, deadline=deadline)
    dirty_paths = _parse_dirty_paths(workspace_root, status, deadline=deadline)
    return _GitBaselineState(head=head, branch=branch, dirty_paths=dirty_paths)


def _git_head(workspace_root: Path, *, deadline: float) -> str | None:
    result = _run_git(
        workspace_root,
        "rev-parse",
        "--verify",
        "--quiet",
        "HEAD",
        deadline=deadline,
        accepted_returncodes=(0, 1),
    )
    if result.returncode == 1:
        if result.stdout:
            raise TaskBaselineError("Git returned unexpected output while resolving unborn HEAD")
        return None
    return _decode_single_line(result.stdout, "HEAD")


def _git_branch(workspace_root: Path, *, deadline: float) -> str | None:
    result = _run_git(
        workspace_root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        deadline=deadline,
        accepted_returncodes=(0, 1),
    )
    if result.returncode == 1:
        if result.stdout:
            raise TaskBaselineError("Git returned unexpected output while resolving detached HEAD")
        return None
    return _decode_single_line(result.stdout, "branch")


def _git_status(workspace_root: Path, *, deadline: float) -> bytes:
    result = _run_git(
        workspace_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        deadline=deadline,
        accepted_returncodes=(0,),
    )
    return result.stdout


def _run_git(
    workspace_root: Path,
    *arguments: str,
    deadline: float,
    accepted_returncodes: tuple[int, ...],
) -> subprocess.CompletedProcess[bytes]:
    remaining = _remaining_seconds(deadline)
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise TaskBaselineTimeoutError("Task baseline Git inspection timed out") from exc
    except FileNotFoundError as exc:
        raise TaskBaselineError("Git executable is not available for Task baseline capture") from exc
    except OSError as exc:
        raise TaskBaselineError(f"Git could not inspect Task baseline at {workspace_root}") from exc
    if result.returncode not in accepted_returncodes:
        detail = os.fsdecode(result.stderr).strip()
        message = f"Git Task baseline inspection failed at {workspace_root}"
        if detail:
            message = f"{message}: {detail}"
        raise TaskBaselineError(message)
    return result


def _decode_single_line(raw: bytes, label: str) -> str:
    value = os.fsdecode(raw)
    if value.endswith("\n"):
        value = value[:-1]
    if value.endswith("\r"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise TaskBaselineError(f"Git returned invalid {label} metadata")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TaskBaselineError(f"Git returned non-UTF-8 {label} metadata") from exc
    return value


def _parse_dirty_paths(
    workspace_root: Path,
    status: bytes,
    *,
    deadline: float,
) -> tuple[TaskBaselineDirtyPath, ...]:
    tokens = status.split(b"\0")
    dirty_paths: list[TaskBaselineDirtyPath] = []
    index = 0
    seen_paths: set[str] = set()
    while index < len(tokens):
        _require_deadline(deadline)
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4 or token[2:3] != b" ":
            raise TaskBaselineError("Git returned malformed porcelain status")
        raw_code = token[:2]
        try:
            status_code = raw_code.decode("ascii")
        except UnicodeDecodeError as exc:
            raise TaskBaselineError("Git returned non-ASCII porcelain status code") from exc
        relative_path = _decode_relative_path(token[3:])
        original_relative_path: str | None = None
        if b"R" in raw_code or b"C" in raw_code:
            if index >= len(tokens) or not tokens[index]:
                raise TaskBaselineError("Git returned incomplete rename/copy status")
            original_relative_path = _decode_relative_path(tokens[index])
            index += 1
        if relative_path in seen_paths:
            raise TaskBaselineError(f"Git returned duplicate dirty path: {relative_path!r}")
        seen_paths.add(relative_path)
        dirty_paths.append(
            TaskBaselineDirtyPath(
                relative_path=relative_path,
                original_relative_path=original_relative_path,
                status_code=status_code,
                state_sha256=_dirty_path_state_sha256(
                    workspace_root,
                    relative_path,
                    original_relative_path,
                    status_code,
                    deadline=deadline,
                ),
            )
        )
    return tuple(sorted(dirty_paths, key=lambda item: item.relative_path))


def _decode_relative_path(raw_path: bytes) -> str:
    value = os.fsdecode(raw_path)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TaskBaselineError("Workspace dirty path cannot be persisted as UTF-8") from exc
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise TaskBaselineError(f"Git returned unsafe dirty path: {value!r}")
    return value


def _dirty_path_state_sha256(
    workspace_root: Path,
    relative_path: str,
    original_relative_path: str | None,
    status_code: str,
    *,
    deadline: float,
) -> str:
    digest = hashlib.sha256()
    _digest_field(digest, status_code)
    _digest_field(digest, relative_path)
    _digest_field(digest, original_relative_path or "")
    path = workspace_root / relative_path
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TaskBaselineChangedError(
            f"dirty path parent cannot be resolved safely: {relative_path}"
        ) from exc
    if not parent.is_relative_to(workspace_root):
        raise TaskBaselineError(f"dirty path escapes Workspace through symlink: {relative_path}")

    try:
        before = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return digest.hexdigest()
    except OSError as exc:
        raise TaskBaselineError(f"dirty path cannot be inspected: {relative_path}") from exc

    if stat.S_ISLNK(before.st_mode):
        try:
            target = os.readlink(path)
            after = path.lstat()
        except OSError as exc:
            raise TaskBaselineChangedError(f"dirty symlink changed during capture: {relative_path}") from exc
        _require_stable_stat(relative_path, before, after)
        digest.update(b"symlink\0")
        digest.update(os.fsencode(target))
        return digest.hexdigest()

    if stat.S_ISREG(before.st_mode):
        digest.update(b"file\0")
        try:
            with path.open("rb") as stream:
                opened_before = os.fstat(stream.fileno())
                _require_stable_stat(relative_path, before, opened_before)
                while True:
                    _require_deadline(deadline)
                    chunk = stream.read(_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                opened_after = os.fstat(stream.fileno())
            current = path.lstat()
        except FileNotFoundError as exc:
            raise TaskBaselineChangedError(f"dirty file changed during capture: {relative_path}") from exc
        except OSError as exc:
            raise TaskBaselineError(f"dirty file cannot be read safely: {relative_path}") from exc
        _require_stable_stat(relative_path, opened_before, opened_after)
        _require_stable_stat(relative_path, opened_after, current)
        return digest.hexdigest()

    digest.update(b"other\0")
    _digest_field(digest, str(before.st_mode))
    _digest_field(digest, str(before.st_size))
    _digest_field(digest, str(before.st_mtime_ns))
    return digest.hexdigest()


def _require_stable_stat(relative_path: str, before: os.stat_result, after: os.stat_result) -> None:
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise TaskBaselineChangedError(f"dirty path changed during capture: {relative_path}")


def _index_snapshot_sha256(indexed_files: tuple[object, ...]) -> str:
    digest = hashlib.sha256()
    for record in indexed_files:
        relative_path = getattr(record, "relative_path", None)
        kind = getattr(record, "kind", None)
        size_bytes = getattr(record, "size_bytes", None)
        content_sha256 = getattr(record, "content_sha256", None)
        if (
            not isinstance(relative_path, str)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(content_sha256, str)
        ):
            raise TaskBaselineError("Structural Index row cannot be fingerprinted for Task baseline")
        kind_value = getattr(kind, "value", None)
        if not isinstance(kind_value, str):
            raise TaskBaselineError("Structural Index kind cannot be fingerprinted for Task baseline")
        _digest_field(digest, relative_path)
        _digest_field(digest, kind_value)
        _digest_field(digest, str(size_bytes))
        _digest_field(digest, content_sha256)
    return digest.hexdigest()


def _digest_field(digest: object, value: str) -> None:
    raw = value.encode("utf-8")
    update = getattr(digest, "update")
    update(len(raw).to_bytes(8, "big"))
    update(raw)


def _read_index_reconciliation_state(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> tuple[int | None, str | None]:
    row = connection.execute(
        """
        SELECT generation, last_reconciled_at
        FROM workspace_index_state
        WHERE workspace_id = ?
        """,
        (workspace_id,),
    ).fetchone()
    if row is None:
        return None, None
    generation, last_reconciled_at = row
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or not isinstance(last_reconciled_at, str)
        or not last_reconciled_at
    ):
        raise TaskBaselineError("Workspace index reconciliation metadata is invalid")
    return generation, last_reconciled_at


def _snapshot_from_row(
    workspace_id: str,
    row: tuple[object, ...],
    dirty_paths: tuple[TaskBaselineDirtyPath, ...],
) -> TaskBaselineSnapshot:
    (
        head,
        branch,
        captured_at,
        index_generation,
        index_last_reconciled_at,
        index_file_count,
        index_snapshot_sha256,
    ) = row
    if (
        (head is not None and (not isinstance(head, str) or not head))
        or (branch is not None and (not isinstance(branch, str) or not branch))
        or not isinstance(captured_at, str)
        or not captured_at
        or isinstance(index_file_count, bool)
        or not isinstance(index_file_count, int)
        or index_file_count < 0
        or not isinstance(index_snapshot_sha256, str)
        or len(index_snapshot_sha256) != 64
    ):
        raise TaskBaselineError("task baseline row has invalid persisted types")
    if index_generation is None or index_last_reconciled_at is None:
        if index_generation is not None or index_last_reconciled_at is not None:
            raise TaskBaselineError("task baseline has incomplete index reconciliation metadata")
        generation = None
        reconciled_at = None
    else:
        if (
            isinstance(index_generation, bool)
            or not isinstance(index_generation, int)
            or index_generation <= 0
            or not isinstance(index_last_reconciled_at, str)
            or not index_last_reconciled_at
        ):
            raise TaskBaselineError("task baseline has invalid index reconciliation metadata")
        generation = index_generation
        reconciled_at = index_last_reconciled_at
    return TaskBaselineSnapshot(
        workspace_id=workspace_id,
        head=head,
        branch=branch,
        captured_at=captured_at,
        index_generation=generation,
        index_last_reconciled_at=reconciled_at,
        index_file_count=index_file_count,
        index_snapshot_sha256=index_snapshot_sha256,
        dirty_paths=dirty_paths,
    )


def _dirty_path_from_row(row: tuple[object, ...]) -> TaskBaselineDirtyPath:
    relative_path, original_relative_path, status_code, state_sha256 = row
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or (original_relative_path is not None and not isinstance(original_relative_path, str))
        or not isinstance(status_code, str)
        or len(status_code) != 2
        or not isinstance(state_sha256, str)
        or len(state_sha256) != 64
    ):
        raise TaskBaselineError("task baseline dirty-path row has invalid persisted types")
    return TaskBaselineDirtyPath(
        relative_path=relative_path,
        original_relative_path=original_relative_path,
        status_code=status_code,
        state_sha256=state_sha256,
    )


def _require_registered_identity(
    workspace: WorkspaceRecord,
    workspace_root: Path,
    git_common_dir: Path,
) -> None:
    if workspace.workspace_root != workspace_root or workspace.git_common_dir != git_common_dir:
        raise TaskBaselineError("registered Workspace Git identity changed before baseline capture")


def _utc_timestamp(now: datetime | None) -> str:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise TaskBaselineError("Task baseline timestamp requires a timezone-aware datetime")
    return current.astimezone(UTC).isoformat(timespec="microseconds")


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TaskBaselineTimeoutError("Task baseline capture deadline exceeded")
    return remaining


def _require_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise TaskBaselineTimeoutError("Task baseline capture deadline exceeded")
