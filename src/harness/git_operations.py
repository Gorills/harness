from __future__ import annotations

# ruff: noqa: RUF001 -- Russian operator-facing diagnostics.
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic

from harness.git_workspace import (
    GitWorkspaceError,
    _git_environment,
    inspect_git_working_tree_status,
    inspect_git_workspace_runtime_identity,
)
from harness.registry import WorkspaceRecord, get_workspace

BRANCH_LIMIT = 12
RECENT_BRANCH_LIMIT = 5
FILE_LIMIT = 24
_OUTPUT_LIMIT = 2 * 1024 * 1024
_BRANCH_ARGUMENTS = [
    "for-each-ref",
    "--sort=-committerdate",
    "--format=%(refname)%00%(objectname)%00%(symref)%00%(worktreepath)",
    "refs/heads/",
    "refs/remotes/",
]


class GitOperationError(GitWorkspaceError):
    """An operator-readable failure; a Git failure may leave an operation in progress."""


@dataclass(frozen=True, slots=True)
class GitBranch:
    ref: str
    name: str
    oid: str
    remote: bool
    occupied: bool


@dataclass(frozen=True, slots=True)
class GitFile:
    path: str
    index_status: str
    worktree_status: str
    conflict: bool
    original_path: str | None = None

    @property
    def staged(self) -> bool:
        return self.index_status not in {" ", "?", "!"} and not self.conflict


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    workspace: WorkspaceRecord
    branch: str | None
    head: str | None
    token: str
    branches: tuple[GitBranch, ...]
    branch_count: int
    matching_branch_count: int
    files: tuple[GitFile, ...]
    file_count: int
    staged_count: int
    conflict_count: int
    operation: str | None
    query: str
    page: int
    selected_oid: str | None = None


@dataclass(frozen=True, slots=True)
class GitOperationRequest:
    action: str
    workspace_id: str
    expected_token: str
    value: str = ""


def _run(root: Path, arguments: list[str], deadline: float) -> bytes:
    timeout = deadline - monotonic()
    if timeout <= 0:
        raise GitOperationError(
            "Git не успел завершить операцию. Обновите состояние перед повтором."
        )
    environment = _git_environment()
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", GIT_MERGE_AUTOEDIT="no")
    try:
        result = subprocess.run(
            ["git", "--no-pager", *arguments],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=min(timeout, 15),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitOperationError(
            "Git не завершил операцию. Проверьте состояние перед повтором; запрос не повторяется автоматически."
        ) from exc
    if result.returncode:
        detail = os.fsdecode(result.stderr or result.stdout).strip()[:1800]
        raise GitOperationError("Git остановил операцию. " + detail)
    if len(result.stdout) > _OUTPUT_LIMIT:
        raise GitOperationError("Состояние репозитория слишком большое для панели Git.")
    return result.stdout


def _files(raw: bytes) -> tuple[GitFile, ...]:
    parts = iter(raw.split(b"\0"))
    result = []
    for part in parts:
        if not part:
            continue
        code = os.fsdecode(part[:2])
        path = os.fsdecode(part[3:])
        # Porcelain v1 -z emits the destination first, then the rename/copy source.
        original = os.fsdecode(next(parts, b"")) if "R" in code or "C" in code else None
        result.append(
            GitFile(
                path, code[0], code[1], code in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}, original
            )
        )
    return tuple(result)


def _file_metadata(root: Path, files: tuple[GitFile, ...]) -> bytes:
    result: list[tuple[str] | tuple[str, int, int, int, int, int]] = []
    for file in files:
        try:
            info = (root / file.path).lstat()
            result.append(
                (
                    file.path,
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
        except FileNotFoundError:
            result.append((file.path,))
    return repr(result).encode()


def _snapshot(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    page: int,
    deadline: float,
    *,
    selected_ref: str | None = None,
) -> GitSnapshot:
    workspace = get_workspace(connection, workspace_id)
    identity = inspect_git_workspace_runtime_identity(workspace.workspace_root, deadline=deadline)
    if identity.layout.workspace_root != workspace.workspace_root or (
        identity.layout.git_common_dir != workspace.git_common_dir
    ):
        raise GitOperationError("Папка проекта перемещена или относится к другому репозиторию.")
    root = workspace.workspace_root
    raw = _run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], deadline)
    files = _files(raw)
    status = inspect_git_working_tree_status(root, deadline=deadline)
    branch = status.branch
    refs = _run(root, _BRANCH_ARGUMENTS, deadline)
    # Ref names cannot contain newlines, but a worktree path can. Only its presence is needed.
    branches = []
    for line in refs.splitlines():
        fields = line.split(b"\0", 3)
        if len(fields) != 4:
            continue
        ref, oid, symbolic, worktree = fields
        if symbolic:
            continue
        remote = ref.startswith(b"refs/remotes/")
        prefix = b"refs/remotes/" if remote else b"refs/heads/"
        branches.append(
            GitBranch(
                os.fsdecode(ref),
                os.fsdecode(ref[len(prefix) :]),
                oid.decode("ascii"),
                remote,
                bool(worktree),
            )
        )
    head = status.head
    operation = next(
        (
            name
            for filename, name in (
                ("MERGE_HEAD", "merge"),
                ("rebase-merge", "rebase"),
                ("rebase-apply", "rebase"),
                ("CHERRY_PICK_HEAD", "cherry-pick"),
                ("REVERT_HEAD", "revert"),
                ("sequencer", "sequencer"),
            )
            if (identity.git_dir / filename).exists()
        ),
        None,
    )
    index = _run(root, ["ls-files", "--stage", "-z"], deadline)
    fingerprint = sha256(raw + refs + index + repr((identity, branch, head, operation)).encode())
    # Detect ordinary worktree edits even when porcelain keeps the same M/M status.
    metadata = _file_metadata(root, files)
    fingerprint.update(metadata)
    matches = [item for item in branches if query.casefold() in item.name.casefold()]
    if query:
        matches.sort(key=lambda item: (item.name.casefold() != query.casefold(), item.remote))
    else:
        matches.sort(key=lambda item: item.name != branch or item.remote)
    page = min(max(1, page), max(1, (len(files) + FILE_LIMIT - 1) // FILE_LIMIT))
    if inspect_git_workspace_runtime_identity(root, deadline=deadline) != identity:
        raise GitOperationError("Папка проекта изменилась во время чтения. Обновите страницу.")
    if (
        _run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], deadline) != raw
        or (inspect_git_working_tree_status(root, deadline=deadline) != status)
        or _run(root, ["ls-files", "--stage", "-z"], deadline) != index
        or (_run(root, _BRANCH_ARGUMENTS, deadline) != refs)
        or _file_metadata(root, files) != metadata
    ):
        raise GitOperationError("Git изменился во время чтения. Обновите страницу.")
    return GitSnapshot(
        workspace,
        branch,
        head,
        fingerprint.hexdigest(),
        tuple(matches[: BRANCH_LIMIT if query else RECENT_BRANCH_LIMIT]),
        len(branches),
        len(matches),
        files[(page - 1) * FILE_LIMIT : page * FILE_LIMIT],
        len(files),
        sum(file.staged for file in files),
        sum(file.conflict for file in files),
        operation,
        query,
        page,
        next((item.oid for item in branches if item.ref == selected_ref), None),
    )


def read_git_snapshot(
    connection: sqlite3.Connection, workspace_id: str, *, query: str = "", page: int = 1
) -> GitSnapshot:
    """Read Git directly; never expose branch inventories through model-facing MCP."""
    return _snapshot(connection, workspace_id, query, page, monotonic() + 10)


def run_git_operation(connection: sqlite3.Connection, request: GitOperationRequest) -> None:
    """Serialize dashboard writes and registry changes; Git owns its native file/ref locks.

    The token rejects changed HEAD, refs, index and ordinary file edits before the command.
    External editors/CLI Git do not participate in this transaction; never force their state.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        deadline = monotonic() + 20
        state = _snapshot(
            connection, request.workspace_id, "", 1, deadline, selected_ref=request.value
        )
        if state.token != request.expected_token:
            raise GitOperationError(
                "Состояние Git изменилось. Проверьте обновлённые данные и повторите действие."
            )
        root = state.workspace.workspace_root
        if state.operation and request.action not in {
            "git_abort_merge",
            "git_stage",
            "git_stage_all",
            "git_commit",
        }:
            raise GitOperationError("Сначала завершите текущую операцию Git или отмените слияние.")
        if state.operation not in {None, "merge"} and request.action not in {
            "git_stage",
            "git_stage_all",
        }:
            raise GitOperationError(
                "Завершите текущую операцию в терминале, затем обновите панель."
            )
        if request.action in {"git_switch", "git_merge"}:
            if state.selected_oid is None:
                raise GitOperationError("Ветка больше не существует. Найдите её снова.")
            if state.file_count or state.operation:
                raise GitOperationError(
                    "Сначала сделайте коммит изменений или сохраните их через Git в терминале."
                )
            if request.action == "git_merge":
                if state.branch is None or state.head is None:
                    raise GitOperationError(
                        "Для слияния сначала переключитесь на ветку с коммитами."
                    )
                source = request.value.removeprefix("refs/heads/").removeprefix("refs/remotes/")
                _run(
                    root,
                    [
                        "merge",
                        "--no-edit",
                        "--no-autostash",
                        "-m",
                        f"Merge branch '{source}'",
                        "--",
                        state.selected_oid,
                    ],
                    deadline,
                )
            elif request.value.startswith("refs/heads/"):
                _run(
                    root,
                    ["switch", "--no-guess", "--no-recurse-submodules", "--", request.value[11:]],
                    deadline,
                )
            else:
                _run(
                    root, ["switch", "--no-recurse-submodules", "--track", request.value], deadline
                )
        elif request.action == "git_create":
            name = request.value.strip()
            if name.startswith("-") or not name or "@{" in name:
                raise GitOperationError("Укажите допустимое название новой ветки.")
            _run(root, ["check-ref-format", "refs/heads/" + name], deadline)
            # A new branch starts at current HEAD and keeps the operator's local changes.
            _run(root, ["switch", "--no-recurse-submodules", "--create", name], deadline)
        elif request.action == "git_stage_all":
            _run(root, ["add", "--all", "--", "."], deadline)
        elif request.action == "git_unstage_all":
            if state.staged_count:
                if state.head:
                    _run(root, ["reset", "--", "."], deadline)
                else:
                    _run(root, ["rm", "--cached", "-r", "-f", "--", "."], deadline)
        elif request.action in {"git_stage", "git_unstage"}:
            files = _files(
                _run(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], deadline)
            )
            selected = next((file for file in files if file.path == request.value), None)
            if selected is None:
                raise GitOperationError("Файл больше не числится изменённым. Обновите страницу.")
            paths = [selected.path]
            if request.action == "git_stage":
                if selected.original_path and selected.worktree_status == "R":
                    paths.append(selected.original_path)
                _run(root, ["--literal-pathspecs", "add", "--", *paths], deadline)
            else:
                if selected.original_path and selected.index_status == "R":
                    paths.append(selected.original_path)
                if state.head:
                    _run(root, ["--literal-pathspecs", "reset", "--", *paths], deadline)
                else:
                    # --cached only removes index entries, preserving all working files.
                    _run(
                        root,
                        ["--literal-pathspecs", "rm", "--cached", "-f", "--", *paths],
                        deadline,
                    )
        elif request.action == "git_commit":
            if not request.value.strip() or len(request.value.encode("utf-8")) > 2048:
                raise GitOperationError("Добавьте сообщение коммита (до 2048 байт).")
            if state.conflict_count:
                raise GitOperationError(
                    "Сначала исправьте конфликты в файлах и подготовьте их к коммиту."
                )
            if not state.staged_count and state.operation != "merge":
                raise GitOperationError("Сначала подготовьте хотя бы один файл к коммиту.")
            if state.branch is None:
                raise GitOperationError(
                    "Сначала создайте ветку: сейчас открыт отдельный коммит (detached HEAD)."
                )
            _run(root, ["commit", "-m", request.value.strip()], deadline)
        elif request.action == "git_abort_merge":
            if state.operation != "merge":
                raise GitOperationError("Нет слияния, которое можно отменить.")
            _run(root, ["merge", "--abort"], deadline)
        else:
            raise GitOperationError("Неизвестная операция Git.")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
