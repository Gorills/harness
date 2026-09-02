"""Project skill pack is independent of Task lifecycle.

Task mutations do not change resolved skills or enqueue skill reconciliation.
Authoritative scan after a real stack change may change the pack.
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

import harness.dashboard as dashboard_module
import harness.skill_runtime as skill_runtime_module
from harness.daemon import mutate_task_checkpoint, mutate_task_start
from harness.dashboard import (
    DashboardActionRequest,
    DashboardServerManager,
    DashboardSkillPolicyRequest,
    mutate_dashboard_skill_policy,
    mutate_dashboard_task,
)
from harness.host_integration_state import HostIntegrationState
from harness.index import scan_workspace
from harness.ipc import TaskCheckpointRequestData, TaskStartRequestData
from harness.registry import create_project, get_workspace, register_workspace
from harness.skill_policy import (
    ProjectSkillFacetMode,
    get_project_skill_policy,
)
from harness.skill_runtime import SkillRuntimeError, reconcile_workspace_skills
from harness.skills import (
    SKILL_METADATA_FILE_NAME,
    ResolvedSkill,
    load_skill_registry,
    resolve_workspace_skills,
)
from harness.storage import connect_database, initialize_database
from harness.tasks import (
    TaskRecord,
    TaskRevisionConflictError,
    TaskState,
    TaskWaitReason,
    get_task,
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


def _write_skill(
    registry: Path,
    skill_id: str,
    *,
    facets: tuple[str, ...],
    task_hints: tuple[str, ...] = (),
) -> None:
    directory = registry / skill_id
    directory.mkdir(parents=True)
    registry.chmod(0o700)
    (directory / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Portable {skill_id} instructions.\n"
        f"---\n\n# {skill_id}\n",
        encoding="utf-8",
    )
    lines = [f"id: {skill_id}", "applies:", "  facets:"]
    lines.extend(f"    - {facet}" for facet in facets)
    if task_hints:
        lines.append("task_hints:")
        lines.extend(f"  - {value}" for value in task_hints)
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


def _polyglot_registry(tmp_path: Path) -> Path:
    registry = tmp_path / "skills"
    _write_skill(registry, "mobile", facets=("mobile-app",), task_hints=("expo",))
    _write_skill(registry, "server", facets=("backend-service",), task_hints=("fastapi",))
    _write_skill(registry, "container", facets=("containerized",))
    return registry


def _start(
    connection: sqlite3.Connection,
    root: Path,
    title: str,
    stack_hints: tuple[str, ...],
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
    )
    return get_task(connection, result.task_id)


def _checkpoint(
    connection: sqlite3.Connection,
    root: Path,
    task: TaskRecord,
    state: TaskState,
    *,
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
    )
    return get_task(connection, result.task_id)


def _get_text(url: str) -> tuple[int, str]:
    parsed = urlsplit(url)
    assert parsed.hostname is not None and parsed.port is not None
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.request("GET", parsed.path)
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    status = response.status
    connection.close()
    return status, body


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


def test_task_start_does_not_change_project_skills(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    definitions = load_skill_registry(registry)
    try:
        before = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        assert next(item.task_hints for item in definitions if item.skill_id == "mobile") == (
            "expo",
        )
        assert next(item.task_hints for item in definitions if item.skill_id == "server") == (
            "fastapi",
        )
        assert before == ("mobile", "server")
        started = _start(connection, root, "Mobile slice", ("expo",))
        after = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        assert after == before
        assert get_task(connection, started.task_id).state is TaskState.WORKING
    finally:
        connection.close()


def test_task_checkpoint_does_not_change_project_skills(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    definitions = load_skill_registry(registry)
    try:
        before = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        working = _start(connection, root, "Mobile slice", ("expo",))
        waiting = _checkpoint(
            connection,
            root,
            working,
            TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Ready for review",
            next_step="Operator review",
        )
        after = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        assert after == before == ("mobile", "server")
        assert waiting.state is TaskState.WAITING
    finally:
        connection.close()


def test_task_terminal_transition_does_not_change_project_skills(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    definitions = load_skill_registry(registry)
    try:
        before = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        working = _start(connection, root, "Mobile slice", ("expo",))
        completed = _checkpoint(
            connection,
            root,
            working,
            TaskState.COMPLETED,
            summary="Shipped mobile work",
            next_step=None,
        )
        assert completed.state is TaskState.COMPLETED
        mutate_dashboard_task(
            database,
            DashboardActionRequest(
                action="reopen",
                workspace_id=workspace_id,
                task_id=completed.task_id,
                expected_revision=completed.revision,
            ),
        )
        reopened = get_task(connection, completed.task_id)
        after = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        assert after == before == ("mobile", "server")
        assert reopened.state is TaskState.WORKING
    finally:
        connection.close()


def test_dashboard_task_action_does_not_request_skill_reconcile(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    definitions = load_skill_registry(registry)
    try:
        before = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        working = _start(connection, root, "Mobile slice", ("expo",))
        waiting = _checkpoint(
            connection,
            root,
            working,
            TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Ready for review",
            next_step="Operator review",
        )
    finally:
        connection.close()

    invalidations: SimpleQueue[str] = SimpleQueue()
    manager = DashboardServerManager(database, workspace_invalidations=invalidations)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        status = _post(
            url,
            {
                "action": "accept",
                "workspace_id": workspace_id,
                "task_id": waiting.task_id,
                "expected_revision": waiting.revision,
            },
            origin=origin,
        )
        assert status == 303
        assert _drain(invalidations) == []
    finally:
        manager.close()

    connection = connect_database(database)
    try:
        assert get_task(connection, waiting.task_id).state is TaskState.COMPLETED
        after = _ids(
            resolve_workspace_skills(connection, workspace_id, load_skill_registry(registry))
        )
        assert after == before == ("mobile", "server")
    finally:
        connection.close()


def test_dashboard_project_skill_scope_persists_without_full_scan_invalidation(
    tmp_path: Path,
) -> None:
    root, database, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "apps/api/pyproject.toml": ('[project]\nname = "api"\ndependencies = ["fastapi"]\n'),
            "apps/site/package.json": json.dumps(
                {"dependencies": {"next": "16", "react": "19", "react-dom": "19"}}
            ),
        },
    )
    worktree = tmp_path / "repo-feature"
    _git(root, "worktree", "add", "--detach", str(worktree))
    try:
        project_id = get_workspace(connection, workspace_id).project_id
        second = register_workspace(connection, project_id=project_id, path=worktree)
        scan_workspace(connection, second.workspace_id)
    finally:
        connection.close()

    invalidations: SimpleQueue[str] = SimpleQueue()
    manager = DashboardServerManager(database, workspace_invalidations=invalidations)
    try:
        url = manager.get_url()
        parsed = urlsplit(url)
        origin = f"http://127.0.0.1:{parsed.port}"
        project_url = url + f"projects/{project_id}/"
        status = _post(
            project_url,
            {
                "action": "set_skill_scope",
                "project_id": project_id,
                "facet": "web-frontend",
                "mode": "excluded",
            },
            origin=origin,
        )
        assert status == 303
        assert _drain(invalidations) == []
        get_status, html = _get_text(project_url)
        assert get_status == 200
        assert "Области разработки" in html
        assert "<strong>Frontend</strong><span" in html
        assert 'data-mode="excluded"' in html
        assert 'name="facet" value="web-frontend"' in html
        assert 'name="mode" value="auto"' in html
        assert 'aria-label="Авто: Frontend"' in html
    finally:
        manager.close()

    connection = connect_database(database)
    try:
        assert get_project_skill_policy(connection, project_id).excluded_facets == ("web-frontend",)
    finally:
        connection.close()


def test_dashboard_skill_scope_reconciles_each_workspace_and_retries_only_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "apps/api/pyproject.toml": '[project]\nname = "api"\ndependencies = ["fastapi"]\n',
            "apps/site/package.json": json.dumps({"dependencies": {"next": "16"}}),
        },
    )
    worktree = tmp_path / "repo-feature"
    _git(root, "worktree", "add", "--detach", str(worktree))
    try:
        project_id = get_workspace(connection, workspace_id).project_id
        second = register_workspace(connection, project_id=project_id, path=worktree)
        scan_workspace(connection, second.workspace_id)
    finally:
        connection.close()

    monkeypatch.setattr(
        dashboard_module,
        "load_host_integration_state_for_database",
        lambda _database: HostIntegrationState(profiles=frozenset({"codex"})),
    )
    reconciled: list[tuple[str, tuple[str, ...]]] = []

    def reconcile(
        _connection: sqlite3.Connection,
        target_workspace_id: str,
        profiles: tuple[str, ...],
    ) -> object:
        reconciled.append((target_workspace_id, profiles))
        if target_workspace_id == second.workspace_id:
            raise SkillRuntimeError("forced retry")
        return object()

    monkeypatch.setattr(dashboard_module, "reconcile_workspace_skills", reconcile)

    retry = mutate_dashboard_skill_policy(
        database,
        DashboardSkillPolicyRequest(
            project_id=project_id,
            facet="web-frontend",
            mode=ProjectSkillFacetMode.EXCLUDED,
        ),
    )

    assert {item[0] for item in reconciled} == {workspace_id, second.workspace_id}
    assert all(item[1] == ("codex",) for item in reconciled)
    assert retry == (second.workspace_id,)
    connection = connect_database(database)
    try:
        assert get_project_skill_policy(connection, project_id).excluded_facets == ("web-frontend",)
    finally:
        connection.close()


def test_project_stack_change_still_reconciles_skills(tmp_path: Path) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    definitions = load_skill_registry(registry)
    try:
        before = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        assert before == ("mobile", "server")
        (root / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
        scan_workspace(connection, workspace_id)
        after = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        assert after == ("container", "mobile", "server")
        projection = reconcile_workspace_skills(
            connection,
            workspace_id,
            ("codex",),
            registry_root=registry,
        )
        assert projection.selected_skill_ids == after
        assert (root / ".agents" / "skills" / "container" / "SKILL.md").is_file()
    finally:
        connection.close()


def test_failed_task_mutation_does_not_change_project_skills(tmp_path: Path) -> None:
    root, database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    definitions = load_skill_registry(registry)
    try:
        before = _ids(resolve_workspace_skills(connection, workspace_id, definitions))
        working = _start(connection, root, "Mobile slice", ("expo",))
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
            mutate_task_checkpoint(connection, stale)
        with pytest.raises(TaskRevisionConflictError):
            mutate_dashboard_task(
                database,
                DashboardActionRequest(
                    action="cancel",
                    workspace_id=workspace_id,
                    task_id=working.task_id,
                    expected_revision=working.revision + 3,
                ),
            )
        assert get_task(connection, working.task_id).state is TaskState.WORKING
        assert _ids(resolve_workspace_skills(connection, workspace_id, definitions)) == before
    finally:
        connection.close()


def test_reconcile_failure_does_not_rollback_committed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _database, connection, workspace_id = _registered_workspace(tmp_path, _POLYGLOT_FILES)
    registry = _polyglot_registry(tmp_path)
    try:
        working = _start(connection, root, "Mobile slice", ("expo",))
        completed = _checkpoint(
            connection,
            root,
            working,
            TaskState.COMPLETED,
            summary="Committed before projection",
            next_step=None,
        )
        committed_revision = completed.revision

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
