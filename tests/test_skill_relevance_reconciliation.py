"""Filesystem/watcher skill reconciliation after relevance-key changes.

These tests prove enqueue and projection repair for the next host discovery
boundary (R-01). They do not prove current-session model instruction delivery
(ADR-0041).
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import subprocess
from pathlib import Path
from queue import Empty, SimpleQueue
from urllib.parse import urlencode, urlsplit

import pytest

import harness.skill_runtime as skill_runtime_module
from harness.daemon import mutate_task_checkpoint, mutate_task_start
from harness.dashboard import DashboardActionRequest, DashboardServerManager, mutate_dashboard_task
from harness.index import scan_workspace
from harness.ipc import TaskCheckpointRequestData, TaskStartRequestData
from harness.registry import create_project, register_workspace
from harness.skill_runtime import SkillRuntimeError, reconcile_workspace_skills
from harness.skills import (
    SKILL_METADATA_FILE_NAME,
    ResolvedSkill,
    load_skill_registry,
    resolve_workspace_skills,
)
from harness.storage import connect_database, initialize_database
from harness.tasks import (
    SkillRelevanceKey,
    TaskOperatorStatus,
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskWaitReason,
    get_relevant_task,
    get_task,
    skill_relevance_key,
)
from harness.workspace_resolution import WorkspaceHint

_POLYGLOT_FILES = {
    "apps/api/pyproject.toml": ('[project]\nname = "api"\ndependencies = ["fastapi"]\n'),
    "apps/mobile/package.json": json.dumps(
        {"dependencies": {"expo": "55", "react-native": "0.83"}}
    ),
}


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _registered_workspace(
    tmp_path: Path,
    files: dict[str, str] | None = None,
) -> tuple[Path, Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    for relative, content in (files or {"README.md": "project\n"}).items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, database, connection, workspace.workspace_id


def _write_skill(registry: Path, skill_id: str, *, task_hints: tuple[str, ...]) -> None:
    directory = registry / skill_id
    directory.mkdir(parents=True)
    registry.chmod(0o700)
    (directory / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Portable {skill_id} instructions.\n"
        f"---\n\n# {skill_id}\n",
        encoding="utf-8",
    )
    lines = [f"id: {skill_id}", "task_hints:"]
    lines.extend(f"  - {hint}" for hint in task_hints)
    (directory / SKILL_METADATA_FILE_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ids(resolved: tuple[ResolvedSkill, ...]) -> tuple[str, ...]:
    return tuple(item.definition.skill_id for item in resolved)


def _drain(queue: SimpleQueue[str]) -> list[str]:
    items: list[str] = []
    while True:
        try:
            items.append(queue.get_nowait())
        except Empty:
            return items


def _hints(root: Path) -> tuple[WorkspaceHint, ...]:
    return (WorkspaceHint(root, "explicit-root"),)


def _start(
    connection: sqlite3.Connection,
    root: Path,
    title: str,
    stack_hints: tuple[str, ...],
    *,
    invalidations: SimpleQueue[str],
) -> TaskRecord:
    result = mutate_task_start(
        connection,
        TaskStartRequestData(
            workspace_hints=_hints(root),
            title=title,
            stack_hints=stack_hints,
            task_id=None,
            expected_revision=None,
        ),
        watcher_invalidations=invalidations,
    )
    return get_task(connection, result.task_id)


def _checkpoint(
    connection: sqlite3.Connection,
    root: Path,
    task: TaskRecord,
    state: TaskState,
    *,
    invalidations: SimpleQueue[str],
    wait_reason: TaskWaitReason | None = None,
    summary: str = "checkpoint",
    next_step: str | None = "next",
) -> TaskRecord:
    result = mutate_task_checkpoint(
        connection,
        TaskCheckpointRequestData(
            workspace_hints=_hints(root),
            task_id=task.task_id,
            expected_revision=task.revision,
            state=state,
            summary=summary,
            next_step=next_step,
            wait_reason=wait_reason,
            verification=(),
            knowledge=(),
        ),
        watcher_invalidations=invalidations,
    )
    return get_task(connection, result.task_id)


def _wait_review(
    connection: sqlite3.Connection,
    root: Path,
    task: TaskRecord,
    *,
    invalidations: SimpleQueue[str],
) -> TaskRecord:
    return _checkpoint(
        connection,
        root,
        task,
        TaskState.WAITING,
        invalidations=invalidations,
        wait_reason=TaskWaitReason.OPERATOR_REVIEW,
        summary="Ready for review",
        next_step="Operator review",
    )


def _working_and_waiting(
    connection: sqlite3.Connection,
    root: Path,
    *,
    invalidations: SimpleQueue[str],
) -> tuple[TaskRecord, TaskRecord]:
    waiting = _start(connection, root, "Waiting backend", ("fastapi",), invalidations=invalidations)
    waiting = _wait_review(connection, root, waiting, invalidations=invalidations)
    working = _start(connection, root, "Working mobile", ("expo",), invalidations=invalidations)
    _drain(invalidations)
    return waiting, working


def _two_waiting_reviews(
    connection: sqlite3.Connection,
    root: Path,
    *,
    invalidations: SimpleQueue[str],
) -> tuple[TaskRecord, TaskRecord]:
    older = _start(connection, root, "Older backend", ("fastapi",), invalidations=invalidations)
    older = _wait_review(connection, root, older, invalidations=invalidations)
    newer = _start(connection, root, "Newer mobile", ("expo",), invalidations=invalidations)
    newer = _wait_review(connection, root, newer, invalidations=invalidations)
    _drain(invalidations)
    return older, newer


def _dashboard(
    database: Path,
    workspace_id: str,
    task: TaskRecord,
    action: str,
    *,
    invalidations: SimpleQueue[str],
    feedback: str | None = None,
    comment: str | None = None,
    jira_url: str | None = None,
    operator_status: TaskOperatorStatus | None = None,
) -> bool:
    return mutate_dashboard_task(
        database,
        DashboardActionRequest(
            action=action,
            workspace_id=workspace_id,
            task_id=task.task_id,
            expected_revision=task.revision,
            feedback=feedback,
            comment=comment,
            jira_url=jira_url,
            operator_status=operator_status,
        ),
        watcher_invalidations=invalidations,
    )


def _post(url: str, fields: dict[str, str | int], *, origin: str) -> int:
    parsed = urlsplit(url)
    assert parsed.hostname is not None and parsed.port is not None
    body = urlencode(fields).encode("ascii")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.request(
        "POST",
        parsed.path,
        body=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
        },
    )
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


def _focus_registry(tmp_path: Path) -> Path:
    registry = tmp_path / "skills"
    _write_skill(registry, "mobile", task_hints=("expo",))
    _write_skill(registry, "server", task_hints=("fastapi",))
    return registry


def test_task_start_create_enqueues_skill_reconcile(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(None, ())
        created = _start(connection, root, "New work", ("expo",), invalidations=invalidations)
        assert _drain(invalidations) == [workspace_id]
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            created.task_id, ("expo",)
        )
    finally:
        connection.close()


def test_task_start_resume_of_different_waiting_task_enqueues_skill_reconcile(
    tmp_path: Path,
) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        older, newer = _two_waiting_reviews(connection, root, invalidations=invalidations)
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            newer.task_id, ("expo",)
        )
        resumed = mutate_task_start(
            connection,
            TaskStartRequestData(
                workspace_hints=_hints(root),
                title=None,
                stack_hints=(),
                task_id=older.task_id,
                expected_revision=older.revision,
            ),
            watcher_invalidations=invalidations,
        )
        assert resumed.task_id == older.task_id
        assert resumed.state is TaskState.WORKING
        assert _drain(invalidations) == [workspace_id]
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            older.task_id, ("fastapi",)
        )
    finally:
        connection.close()


def test_task_completion_invalidates_skill_relevance(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        waiting, working = _working_and_waiting(connection, root, invalidations=invalidations)
        before = skill_relevance_key(connection, workspace_id)
        assert before == SkillRelevanceKey(working.task_id, ("expo",))

        completed = _checkpoint(
            connection,
            root,
            working,
            TaskState.COMPLETED,
            invalidations=invalidations,
            summary="Shipped mobile work",
        )

        assert completed.state is TaskState.COMPLETED
        assert _drain(invalidations) == [workspace_id]
        after = skill_relevance_key(connection, workspace_id)
        assert after == SkillRelevanceKey(waiting.task_id, ("fastapi",))
        relevant = get_relevant_task(connection, workspace_id)
        assert relevant is not None
        assert relevant.task_id == waiting.task_id
    finally:
        connection.close()


def test_task_cancel_invalidates_skill_relevance(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        waiting, working = _working_and_waiting(connection, root, invalidations=invalidations)
        changed = _dashboard(database, workspace_id, working, "cancel", invalidations=invalidations)
        assert changed is True
        assert _drain(invalidations) == [workspace_id]
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            waiting.task_id, ("fastapi",)
        )
        assert get_task(connection, working.task_id).state is TaskState.CANCELLED
    finally:
        connection.close()


def test_task_reopen_invalidates_skill_relevance(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        waiting, working = _working_and_waiting(connection, root, invalidations=invalidations)
        completed = _checkpoint(
            connection,
            root,
            working,
            TaskState.COMPLETED,
            invalidations=invalidations,
            summary="Finished",
        )
        _drain(invalidations)
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            waiting.task_id, ("fastapi",)
        )

        changed = _dashboard(
            database, workspace_id, completed, "reopen", invalidations=invalidations
        )
        assert changed is True
        assert _drain(invalidations) == [workspace_id]
        reopened = get_task(connection, completed.task_id)
        assert reopened.state is TaskState.WORKING
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            reopened.task_id, ("expo",)
        )
    finally:
        connection.close()


def test_operator_accept_invalidates_skill_relevance(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path)
    setup_queue: SimpleQueue[str] = SimpleQueue()
    try:
        older, newer = _two_waiting_reviews(connection, root, invalidations=setup_queue)
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            newer.task_id, ("expo",)
        )
    finally:
        connection.close()

    invalidations: SimpleQueue[str] = SimpleQueue()
    manager = DashboardServerManager(database, workspace_invalidations=invalidations)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        # Accept the currently relevant (newest) waiting Task so the older Task
        # with different stack_hints becomes relevant. Accept completes; it does
        # not resume the older Task to working.
        status = _post(
            url,
            {
                "action": "accept",
                "workspace_id": workspace_id,
                "task_id": newer.task_id,
                "expected_revision": newer.revision,
            },
            origin=origin,
        )
        assert status == 303
        assert _drain(invalidations) == [workspace_id]
    finally:
        manager.close()

    connection = connect_database(database)
    try:
        assert get_task(connection, newer.task_id).state is TaskState.COMPLETED
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            older.task_id, ("fastapi",)
        )
    finally:
        connection.close()


def test_feedback_relevance_change_invalidates_skills(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        older, newer = _two_waiting_reviews(connection, root, invalidations=invalidations)
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            newer.task_id, ("expo",)
        )
        changed = _dashboard(
            database,
            workspace_id,
            older,
            "feedback",
            invalidations=invalidations,
            feedback="Please continue the FastAPI work",
        )
        assert changed is True
        assert _drain(invalidations) == [workspace_id]
        resumed = get_task(connection, older.task_id)
        assert resumed.state is TaskState.WORKING
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            resumed.task_id, ("fastapi",)
        )
    finally:
        connection.close()


def test_non_relevance_metadata_mutation_does_not_invalidate_skills(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        _waiting_backend, working = _working_and_waiting(
            connection, root, invalidations=invalidations
        )
        expected = SkillRelevanceKey(working.task_id, ("expo",))
        assert skill_relevance_key(connection, workspace_id) == expected

        assert (
            _dashboard(
                database,
                workspace_id,
                working,
                "comment",
                invalidations=invalidations,
                comment="Operator note only",
            )
            is False
        )
        working = get_task(connection, working.task_id)
        assert (
            _dashboard(
                database,
                workspace_id,
                working,
                "set_jira",
                invalidations=invalidations,
                jira_url="https://jira.example/browse/HAR-42",
            )
            is False
        )
        working = get_task(connection, working.task_id)
        assert (
            _dashboard(
                database,
                workspace_id,
                working,
                "set_operator_status",
                invalidations=invalidations,
                operator_status=TaskOperatorStatus.DEPLOY_TEST,
            )
            is False
        )
        working = get_task(connection, working.task_id)
        working = _checkpoint(
            connection,
            root,
            working,
            TaskState.WORKING,
            invalidations=invalidations,
            summary="Same relevant Task, new summary",
        )
        mutate_task_start(
            connection,
            TaskStartRequestData(
                workspace_hints=_hints(root),
                title=None,
                stack_hints=(),
                task_id=working.task_id,
                expected_revision=working.revision,
            ),
            watcher_invalidations=invalidations,
        )
        waiting_relevant = _wait_review(
            connection,
            root,
            get_task(connection, working.task_id),
            invalidations=invalidations,
        )
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            waiting_relevant.task_id, ("expo",)
        )
        assert (
            _dashboard(
                database,
                workspace_id,
                waiting_relevant,
                "feedback",
                invalidations=invalidations,
                feedback="Continue the same Expo task",
            )
            is False
        )
        assert _drain(invalidations) == []
        assert skill_relevance_key(connection, workspace_id) == expected
        assert get_task(connection, _waiting_backend.task_id).state is TaskState.WAITING
    finally:
        connection.close()


def test_failed_task_mutation_does_not_queue_skill_reconcile(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        waiting, working = _working_and_waiting(connection, root, invalidations=invalidations)
        before = skill_relevance_key(connection, workspace_id)
        stale = TaskCheckpointRequestData(
            workspace_hints=_hints(root),
            task_id=working.task_id,
            expected_revision=working.revision + 7,
            state=TaskState.COMPLETED,
            summary="stale",
            next_step=None,
            wait_reason=None,
            verification=(),
            knowledge=(),
        )
        with pytest.raises(TaskRevisionConflictError):
            mutate_task_checkpoint(connection, stale, watcher_invalidations=invalidations)
        with pytest.raises(TaskRevisionConflictError):
            mutate_dashboard_task(
                database,
                DashboardActionRequest(
                    action="cancel",
                    workspace_id=workspace_id,
                    task_id=working.task_id,
                    expected_revision=working.revision + 3,
                ),
                watcher_invalidations=invalidations,
            )
        assert _drain(invalidations) == []
        assert get_task(connection, working.task_id).state is TaskState.WORKING
        assert get_task(connection, waiting.task_id).state is TaskState.WAITING
        assert skill_relevance_key(connection, workspace_id) == before
    finally:
        connection.close()


def test_reconcile_failure_does_not_rollback_committed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path)
    registry = _focus_registry(tmp_path)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        _waiting, working = _working_and_waiting(connection, root, invalidations=invalidations)
        completed = _checkpoint(
            connection,
            root,
            working,
            TaskState.COMPLETED,
            invalidations=invalidations,
            summary="Committed before projection",
        )
        assert completed.state is TaskState.COMPLETED
        committed_revision = completed.revision
        assert _drain(invalidations) == [workspace_id]

        def fail_projection(*_args: object, **_kwargs: object) -> None:
            raise SkillRuntimeError("projection failed")

        monkeypatch.setattr(skill_runtime_module, "apply_skill_projection", fail_projection)
        with pytest.raises(SkillRuntimeError):
            reconcile_workspace_skills(
                connection,
                workspace_id,
                ("codex",),
                registry_root=registry,
            )
        row = get_task(connection, completed.task_id)
        assert row.state is TaskState.COMPLETED
        assert row.revision == committed_revision
    finally:
        connection.close()


def test_terminal_task_reveals_previous_waiting_task_skills(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _focus_registry(tmp_path)
    definitions = load_skill_registry(registry)
    invalidations: SimpleQueue[str] = SimpleQueue()
    try:
        waiting, working = _working_and_waiting(connection, root, invalidations=invalidations)
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            working.task_id, ("expo",)
        )
        assert _ids(resolve_workspace_skills(connection, workspace_id, definitions)) == ("mobile",)

        completed = _checkpoint(
            connection,
            root,
            working,
            TaskState.COMPLETED,
            invalidations=invalidations,
            summary="Mobile slice done",
        )
        assert completed.state is TaskState.COMPLETED
        assert _drain(invalidations) == [workspace_id]
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            waiting.task_id, ("fastapi",)
        )
        assert _ids(resolve_workspace_skills(connection, workspace_id, definitions)) == ("server",)
    finally:
        connection.close()
