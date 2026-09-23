from __future__ import annotations

import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING

from harness.git_workspace import (
    GitWorkspaceError,
    _git_environment,
    inspect_git_working_tree_status,
    inspect_workspace_runtime_identity,
    layout_has_git,
)
from harness.registry import get_workspace, workspace_layout_compatible

if TYPE_CHECKING:
    from harness.knowledge import KnowledgeCardRecord

MAX_APPLICABILITY_PATHS = 256
MAX_APPLICABILITY_FILE_BYTES = 8 * 1024 * 1024
MAX_APPLICABILITY_READ_BYTES = 32 * 1024 * 1024


class GitApplicabilityError(GitWorkspaceError):
    """A consistent, bounded active-Workspace applicability proof was unavailable."""


class _FingerprintBudgetExceeded(GitApplicabilityError):
    """Fingerprint bytes exceeded the bounded evidence budget."""


@dataclass(frozen=True, slots=True)
class _Scope:
    workspace_id: str
    has_git: bool
    head: str | None
    branch: str | None
    baseline_head: str | None
    checkpoint_id: str | None = None
    clean: bool = False
    changed: bool = False
    complete: bool = False


class WorkspaceApplicability:
    """Request-local Git/content proofs; callers retain ownership of SQLite transactions."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.connection = connection
        self.workspace_id = workspace_id
        self.workspace = get_workspace(connection, workspace_id)
        self._deadline = monotonic() + timeout_seconds
        self._identity = inspect_workspace_runtime_identity(
            self.workspace.workspace_root, deadline=self._deadline
        )
        if not workspace_layout_compatible(self.workspace, self._identity.layout):
            raise GitApplicabilityError("registered Workspace identity changed")
        self.has_git = layout_has_git(self._identity.layout)
        self._status = (
            inspect_git_working_tree_status(
                self.workspace.workspace_root, deadline=self._deadline, untracked_files="all"
            )
            if self.has_git
            else None
        )
        self.head = None if self._status is None else self._status.head
        self.branch = None if self._status is None else self._status.branch
        self._ancestry: dict[str, bool] = {}
        self._tasks: dict[str, bool] = {}
        self._checkpoints: dict[str, bool] = {}
        self._paths: dict[str, tuple[str, str | None] | None] = {}
        self._read_bytes = 0

    def _require_time(self) -> None:
        if monotonic() >= self._deadline:
            raise GitApplicabilityError("Workspace applicability deadline exceeded")

    def validate(self) -> None:
        """Reject branch, HEAD, registry, identity, or consulted-file movement during this read."""
        self._require_time()
        if get_workspace(self.connection, self.workspace_id) != self.workspace:
            raise GitApplicabilityError("Workspace registry changed during applicability read")
        if (
            inspect_workspace_runtime_identity(
                self.workspace.workspace_root, deadline=self._deadline
            )
            != self._identity
        ):
            raise GitApplicabilityError("Workspace identity changed during applicability read")
        status = (
            inspect_git_working_tree_status(
                self.workspace.workspace_root, deadline=self._deadline, untracked_files="all"
            )
            if self.has_git
            else None
        )
        if status != self._status:
            raise GitApplicabilityError("Workspace Git state changed during applicability read")
        for path, expected in self._paths.items():
            if self._fingerprint(path, charge=False) != expected:
                raise GitApplicabilityError("Workspace evidence changed during applicability read")
        self._require_time()

    def ancestor(self, head: str | None) -> bool:
        self._require_time()
        if head is None or self.head is None:
            return False
        if head == self.head:
            return True
        if head not in self._ancestry:
            if len(head) not in (40, 64) or any(c not in "0123456789abcdef" for c in head):
                return False
            try:
                result = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", head, self.head],
                    cwd=self.workspace.workspace_root,
                    env=_git_environment(),
                    capture_output=True,
                    timeout=max(0.001, self._deadline - monotonic()),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise GitApplicabilityError("Git ancestry proof unavailable") from exc
            # Missing legacy objects are unavailable evidence, never a positive proof.
            self._ancestry[head] = result.returncode == 0
        return self._ancestry[head]

    def fingerprint(self, path: str) -> tuple[str, str | None] | None:
        self._require_time()
        if path not in self._paths:
            if len(self._paths) >= MAX_APPLICABILITY_PATHS:
                raise GitApplicabilityError("Workspace applicability path budget exceeded")
            self._paths[path] = self._fingerprint(path)
        return self._paths[path]

    def _fingerprint(self, path: str, *, charge: bool = True) -> tuple[str, str | None] | None:
        from harness.knowledge import KnowledgeError, _capture_anchor_fingerprint

        self._require_time()
        relative = PurePosixPath(path)
        if not path or relative.is_absolute() or ".." in relative.parts or "\x00" in path:
            raise GitApplicabilityError("invalid persisted applicability path")
        target = self.workspace.workspace_root / path
        try:
            if not target.parent.resolve().is_relative_to(self.workspace.workspace_root):
                return None
            info = target.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                return None
            if info.st_size > MAX_APPLICABILITY_FILE_BYTES:
                return None
            if charge:
                self._read_bytes += info.st_size
                if self._read_bytes > MAX_APPLICABILITY_READ_BYTES:
                    raise _FingerprintBudgetExceeded("Workspace applicability byte budget exceeded")
            kind, digest = _capture_anchor_fingerprint(
                self.workspace.workspace_root, path, deadline=self._deadline
            )
            return kind.value, digest
        except FileNotFoundError:
            return "deleted", None
        except (OSError, KnowledgeError):
            return None

    def _origin(self, task_id: str) -> _Scope | None:
        row = self.connection.execute(
            """
            SELECT t.workspace_id, b.head, b.branch, o.has_git, o.head, o.branch
            FROM tasks t LEFT JOIN task_baselines b ON b.task_id = t.id
            LEFT JOIN task_git_origins o ON o.task_id = t.id WHERE t.id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        owner = get_workspace(self.connection, row[0])
        if owner.project_id != self.workspace.project_id:
            return None
        workspace_id, baseline_head, baseline_branch, has_git, head, branch = row
        if has_git is None:
            # Legacy baselines carry actual historical HEAD/branch, never today's source bytes.
            has_git = baseline_head is not None or baseline_branch is not None
            head, branch = baseline_head, baseline_branch
            if not has_git and self.has_git:
                return None
        return _Scope(workspace_id, bool(has_git), head, branch, baseline_head)

    def _checkpoint_scope(self, checkpoint_id: str) -> _Scope | None:
        row = self.connection.execute(
            """
            SELECT c.task_id, c.current_head, c.current_branch, c.current_dirty_path_count,
                   EXISTS(SELECT 1 FROM task_checkpoint_changed_paths p WHERE p.checkpoint_id=c.id),
                   e.has_git, e.complete
            FROM task_checkpoints c LEFT JOIN task_git_evidence e ON e.checkpoint_id=c.id
            WHERE c.id = ?
            """,
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            return None
        origin = self._origin(row[0])
        if origin is None:
            return None
        _task_id, head, branch, dirty_count, changed, has_git, complete = row
        if has_git is None:
            has_git = origin.has_git
        return _Scope(
            origin.workspace_id,
            bool(has_git),
            head,
            branch,
            origin.baseline_head,
            checkpoint_id,
            dirty_count == 0,
            bool(changed),
            bool(complete),
        )

    def _visible(self, scope: _Scope) -> bool:
        if not scope.has_git:
            return not self.has_git and scope.workspace_id == self.workspace_id
        if not self.has_git:
            return False
        lineage = self.ancestor(scope.head)
        same_origin = scope.workspace_id == self.workspace_id and scope.branch == self.branch
        if same_origin and (lineage or (scope.head is None and self.head is None)):
            # Task continuity on its originating branch survives ordinary worktree edits.
            return True
        if scope.checkpoint_id is None:
            return False
        if scope.clean and scope.changed and lineage:
            return True
        if not scope.complete or not scope.changed:
            return False
        if scope.baseline_head is not None and not self.ancestor(scope.baseline_head):
            return False
        if scope.baseline_head is None and scope.head is not None and not lineage:
            return False
        return self._content_matches(scope)

    def _content_matches(self, scope: _Scope) -> bool:
        if not scope.complete or not scope.changed:
            return False
        rows = self.connection.execute(
            "SELECT relative_path, kind, content_sha256 FROM task_git_evidence_paths "
            "WHERE checkpoint_id = ? ORDER BY relative_path",
            (scope.checkpoint_id,),
        ).fetchall()
        return bool(rows) and all(
            self.fingerprint(path) == (kind, digest) for path, kind, digest in rows
        )

    def task_visible(self, task_id: str) -> bool:
        self._require_time()
        if task_id not in self._tasks:
            latest = self.connection.execute(
                "SELECT id FROM task_checkpoints WHERE task_id=? ORDER BY task_revision DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            scope = self._origin(task_id) if latest is None else self._checkpoint_scope(latest[0])
            self._tasks[task_id] = scope is not None and self._visible(scope)
        return self._tasks[task_id]

    def checkpoint_visible(self, checkpoint_id: str) -> bool:
        self._require_time()
        if checkpoint_id not in self._checkpoints:
            scope = self._checkpoint_scope(checkpoint_id)
            self._checkpoints[checkpoint_id] = scope is not None and self._visible(scope)
        return self._checkpoints[checkpoint_id]

    def knowledge_visible(self, card: KnowledgeCardRecord) -> bool:
        if card.project_id != self.workspace.project_id:
            return False
        anchors_match = all(
            self.fingerprint(anchor.relative_path)
            == (anchor.fingerprint_kind.value, anchor.content_sha256)
            for anchor in card.anchors
        )
        if not anchors_match:
            return False
        if card.source_type.value != "agent_asserted" and card.source_checkpoint_id is None:
            return True
        if card.source_checkpoint_id is None:
            return False
        scope = self._checkpoint_scope(card.source_checkpoint_id)
        if scope is None:
            return False
        if not card.anchors:
            if scope.changed:
                return self._content_matches(scope) and (
                    (
                        not scope.has_git
                        and not self.has_git
                        and scope.workspace_id == self.workspace_id
                    )
                    or (
                        scope.has_git
                        and self.has_git
                        and (
                            self.ancestor(scope.head)
                            or self.ancestor(scope.baseline_head)
                            or (scope.head is None and scope.baseline_head is None)
                        )
                    )
                )
            return (
                scope.workspace_id == self.workspace_id
                and scope.has_git == self.has_git
                and scope.head == self.head
                and scope.branch == self.branch
                and scope.clean
                and (self._status is None or self._status.dirty_path_count == 0)
            )
        if self.checkpoint_visible(card.source_checkpoint_id):
            return True
        # Anchored observations can precede the Task's own edits. Their source commit plus
        # exact live anchors prove applicability even when the Task itself has no change set.
        return bool(card.anchors) and scope.has_git and self.has_git and self.ancestor(scope.head)

    def initial_handoff_allowed(self, task_id: str) -> bool:
        if (
            self.connection.execute(
                "SELECT 1 FROM task_checkpoints WHERE task_id = ? LIMIT 1", (task_id,)
            ).fetchone()
            is not None
        ):
            return False
        scope = self._origin(task_id)
        return (
            scope is not None
            and scope.workspace_id == self.workspace_id
            and (
                (scope.has_git and self.has_git and self.ancestor(scope.baseline_head))
                or not scope.has_git
                or (
                    scope.has_git
                    and self.has_git
                    and scope.head is None
                    and self.head is None
                    and scope.branch == self.branch
                )
            )
        )


def capture_task_origin(connection: sqlite3.Connection, task_id: str) -> None:
    row = connection.execute(
        "SELECT t.workspace_id,b.head,b.branch FROM tasks t JOIN task_baselines b ON b.task_id=t.id "
        "WHERE t.id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise GitApplicabilityError("Task origin requires a captured baseline")
    workspace = get_workspace(connection, row[0])
    has_git = workspace.git_common_dir != workspace.workspace_root
    connection.execute(
        "INSERT INTO task_git_origins(task_id,has_git,head,branch) VALUES (?,?,?,?)",
        (task_id, int(has_git), row[1], row[2]),
    )


def persist_initial_handoff(
    connection: sqlite3.Connection, task_id: str, applicability: WorkspaceApplicability
) -> None:
    connection.execute(
        "INSERT INTO task_git_origins(task_id,has_git,head,branch) VALUES (?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET has_git=excluded.has_git,head=excluded.head,branch=excluded.branch",
        (task_id, int(applicability.has_git), applicability.head, applicability.branch),
    )


def capture_checkpoint_evidence(connection: sqlite3.Connection, checkpoint_id: str) -> None:
    row = connection.execute(
        "SELECT t.workspace_id,c.current_head,c.current_branch,c.current_dirty_path_count FROM task_checkpoints c "
        "JOIN tasks t ON t.id=c.task_id WHERE c.id=?",
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise GitApplicabilityError("Task checkpoint evidence requires a checkpoint")
    applicability = WorkspaceApplicability(connection, row[0], timeout_seconds=30.0)
    if (applicability.head, applicability.branch) != (row[1], row[2]):
        raise GitApplicabilityError("Git changed during checkpoint evidence capture")
    dirty_count = 0 if applicability._status is None else applicability._status.dirty_path_count
    if dirty_count != row[3]:
        raise GitApplicabilityError("Git dirty state changed during checkpoint evidence capture")
    paths = connection.execute(
        "SELECT relative_path FROM task_checkpoint_changed_paths WHERE checkpoint_id=? "
        "ORDER BY relative_path LIMIT ?",
        (checkpoint_id, MAX_APPLICABILITY_PATHS + 1),
    ).fetchall()
    complete = len(paths) <= MAX_APPLICABILITY_PATHS
    captured: list[tuple[str, str, str, str | None]] = []
    for (path,) in paths[:MAX_APPLICABILITY_PATHS]:
        try:
            fingerprint = applicability.fingerprint(path)
        except _FingerprintBudgetExceeded:
            complete = False
            break
        if fingerprint is None:
            complete = False
            continue
        kind, digest = fingerprint
        captured.append((checkpoint_id, path, kind, digest))
    connection.execute(
        "INSERT INTO task_git_evidence(checkpoint_id,has_git,complete) VALUES (?,?,?)",
        (checkpoint_id, int(applicability.has_git), int(complete)),
    )
    connection.executemany(
        "INSERT INTO task_git_evidence_paths(checkpoint_id,relative_path,kind,content_sha256) "
        "VALUES (?,?,?,?)",
        captured,
    )
    applicability.validate()
