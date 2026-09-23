from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import pytest

from harness.dashboard import (
    DashboardActionRequest,
    _parse_dashboard_action_form,
    _recoverable_form_fields,
    _render_task_actions,
)
from harness.index import scan_workspace
from harness.knowledge import KnowledgeAnchorDraft, KnowledgeDraft, KnowledgeKind
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import TaskEventType, list_task_events
from harness.task_workflow import (
    task_accept,
    task_checkpoint,
    task_delete,
    task_set_deployment,
    task_set_state,
    task_start,
)
from harness.tasks import (
    TaskConflictError,
    TaskNotFoundError,
    TaskOperatorStatus,
    TaskRevisionConflictError,
    TaskState,
    TaskValidationError,
    TaskWaitReason,
    get_task,
)


def _setup(tmp_path: Path) -> tuple[sqlite3.Connection, str, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("answer = 42\n", encoding="utf-8")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return connection, workspace.workspace_id, root


@pytest.mark.parametrize("initial", list(TaskState))
@pytest.mark.parametrize("target", list(TaskState))
def test_operator_can_choose_every_state_with_cas(
    tmp_path: Path, initial: TaskState, target: TaskState
) -> None:
    connection, workspace, _root = _setup(tmp_path)
    try:
        created = task_start(connection, workspace, "Управление задачей")
        before = task_set_state(
            connection,
            workspace,
            created.task_id,
            expected_revision=created.revision,
            state=initial,
            wait_reason=TaskWaitReason.OPERATOR_INPUT if initial is TaskState.WAITING else None,
        ).task
        mutation = task_set_state(
            connection,
            workspace,
            before.task_id,
            expected_revision=before.revision,
            state=target,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW if target is TaskState.WAITING else None,
        )
        assert mutation.task.state is target
        assert mutation.task.revision == before.revision + 1
        assert mutation.event == list_task_events(connection, created.task_id)[-1]
        assert mutation.event.event_type is (
            TaskEventType.ACCEPTED if target is TaskState.COMPLETED else TaskEventType.STATE_CHANGED
        )
        with pytest.raises(TaskRevisionConflictError):
            task_set_state(
                connection,
                workspace,
                before.task_id,
                expected_revision=before.revision,
                state=TaskState.CANCELLED,
            )
        assert get_task(connection, before.task_id) == mutation.task
    finally:
        connection.close()


def test_operator_state_retains_one_working_and_rolls_back_event_failure(tmp_path: Path) -> None:
    connection, workspace, _root = _setup(tmp_path)
    try:
        first = task_start(connection, workspace, "Первая")
        done = task_accept(
            connection, workspace, first.task_id, expected_revision=first.revision
        ).task
        second = task_start(connection, workspace, "Вторая")
        with pytest.raises(TaskConflictError):
            task_set_state(
                connection,
                workspace,
                first.task_id,
                expected_revision=done.revision,
                state=TaskState.WORKING,
            )
        assert get_task(connection, first.task_id) == done
        connection.execute(
            "CREATE TRIGGER fail_operator_event BEFORE INSERT ON task_events "
            "BEGIN SELECT RAISE(ABORT, 'event failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="event failure"):
            task_set_state(
                connection,
                workspace,
                second.task_id,
                expected_revision=second.revision,
                state=TaskState.CANCELLED,
            )
        assert get_task(connection, second.task_id) == second
    finally:
        connection.close()


@pytest.mark.parametrize(
    "test_flag,prod_flag", [(False, False), (True, False), (False, True), (True, True)]
)
def test_deployment_flags_have_independent_current_and_history_values(
    tmp_path: Path, test_flag: bool, prod_flag: bool
) -> None:
    connection, workspace, _root = _setup(tmp_path)
    try:
        created = task_start(connection, workspace, "Деплой")
        result = task_set_deployment(
            connection,
            workspace,
            created.task_id,
            expected_revision=created.revision,
            deploy_test=test_flag,
            deploy_prod=prod_flag,
        )
        assert (result.task.deploy_test, result.task.deploy_prod) == (test_flag, prod_flag)
        assert result.event == list_task_events(connection, created.task_id)[-1]
        assert result.event.operator_status == result.task.operator_status
        if test_flag and prod_flag:
            assert result.task.operator_status is TaskOperatorStatus.DEPLOY_BOTH
        cleared = task_set_deployment(
            connection,
            workspace,
            created.task_id,
            expected_revision=result.task.revision,
            deploy_test=False,
            deploy_prod=False,
        )
        assert cleared.task.operator_status is None
        assert list_task_events(connection, created.task_id)[-2] == result.event
    finally:
        connection.close()


def test_delete_removes_provenance_and_history_atomically_and_keeps_source(tmp_path: Path) -> None:
    connection, workspace, root = _setup(tmp_path)
    try:
        created = task_start(connection, workspace, "Лишняя задача")
        result = task_checkpoint(
            connection,
            workspace,
            created.task_id,
            expected_revision=created.revision,
            state=TaskState.WORKING,
            summary="Диагностика",
            knowledge=(
                KnowledgeDraft(
                    kind=KnowledgeKind.INVARIANT,
                    title="Число",
                    body="Число хранится в модуле",
                    anchors=(KnowledgeAnchorDraft(path="source.py"),),
                ),
            ),
        )
        before = tuple(connection.iterdump())
        with pytest.raises(TaskRevisionConflictError):
            task_delete(connection, workspace, created.task_id, expected_revision=1)
        assert tuple(connection.iterdump()) == before
        connection.execute(
            "CREATE TRIGGER fail_task_delete BEFORE DELETE ON tasks "
            "BEGIN SELECT RAISE(ABORT, 'delete failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="delete failure"):
            task_delete(
                connection, workspace, created.task_id, expected_revision=result.task.revision
            )
        assert get_task(connection, created.task_id) == result.task
        assert connection.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone() == (1,)
        connection.execute("DROP TRIGGER fail_task_delete")
        task_delete(connection, workspace, created.task_id, expected_revision=result.task.revision)
        with pytest.raises(TaskNotFoundError):
            get_task(connection, created.task_id)
        for table in (
            "task_events",
            "task_baselines",
            "task_checkpoints",
            "knowledge_cards",
            "knowledge_anchors",
            "task_search",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert (root / "source.py").read_text() == "answer = 42\n"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_agent_cannot_complete_and_review_requires_operator_acceptance(tmp_path: Path) -> None:
    connection, workspace, _root = _setup(tmp_path)
    try:
        created = task_start(connection, workspace, "Приёмка")
        with pytest.raises(TaskValidationError, match="only the operator"):
            task_checkpoint(
                connection,
                workspace,
                created.task_id,
                expected_revision=created.revision,
                state=TaskState.COMPLETED,
                summary="Автозакрытие запрещено",
            )
        assert get_task(connection, created.task_id) == created
        review = task_checkpoint(
            connection,
            workspace,
            created.task_id,
            expected_revision=created.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Готово",
            next_step="Принять результат",
        )
        assert all(
            e.event_type is not TaskEventType.ACCEPTED
            for e in list_task_events(connection, created.task_id)
        )
        accepted = task_accept(
            connection, workspace, created.task_id, expected_revision=review.task.revision
        )
        assert accepted.task.state is TaskState.COMPLETED
    finally:
        connection.close()


def test_dashboard_independent_checkbox_values_and_conflict_draft() -> None:
    base = {
        "action": "set_deployment",
        "workspace_id": "workspace",
        "task_id": "task",
        "expected_revision": "7",
    }
    for enabled in (
        {},
        {"deploy_test": "1"},
        {"deploy_prod": "1"},
        {"deploy_test": "1", "deploy_prod": "1"},
    ):
        payload = urlencode(base | enabled).encode()
        request = _parse_dashboard_action_form(payload)
        assert isinstance(request, DashboardActionRequest)
        assert request.deploy_test == ("deploy_test" in enabled)
        assert request.deploy_prod == ("deploy_prod" in enabled)
        draft = _recoverable_form_fields(payload)
        html = _render_task_actions(
            workspace_id="workspace",
            task_id="task",
            revision=8,
            state="completed",
            wait_reason=None,
            detailed=True,
            operator_status=TaskOperatorStatus.DEPLOY_BOTH,
            form_values=draft,
        )
        for name in ("deploy_test", "deploy_prod"):
            marker = f'type="checkbox" name="{name}" value="1"'
            assert (
                (marker + " checked") in html
                if name in enabled
                else (marker + " data-recovered-draft") in html
            )
            assert 'data-recovered-draft="true"' in html
        assert 'name="state"' in html
        assert 'value="working"' in html
        assert 'value="waiting"' in html
        assert 'value="completed" selected' in html
        assert 'value="cancelled"' in html


@pytest.mark.parametrize("bad_state", ["working", [], None, "completed"])
def test_ipc_checkpoint_rejects_non_enum_and_terminal_inputs(bad_state: object) -> None:
    from typing import cast

    from harness.ipc import IpcProtocolError, _task_checkpoint_params_to_wire

    with pytest.raises(IpcProtocolError, match="state must be working or waiting"):
        _task_checkpoint_params_to_wire(
            (),
            "task",
            expected_revision=1,
            state=cast(TaskState, bad_state),
            summary="Проверка",
            next_step=None,
            wait_reason=None,
            verification=(),
            knowledge=(),
        )


@pytest.mark.parametrize("bad_state", ["working", [], None])
def test_operator_state_rejects_invalid_types_without_mutation(
    tmp_path: Path, bad_state: object
) -> None:
    from typing import cast

    connection, workspace, _root = _setup(tmp_path)
    try:
        task = task_start(connection, workspace, "Валидация состояния")
        with pytest.raises(TaskValidationError, match="unsupported"):
            task_set_state(
                connection,
                workspace,
                task.task_id,
                expected_revision=task.revision,
                state=cast(TaskState, bad_state),
            )
        assert get_task(connection, task.task_id) == task
    finally:
        connection.close()
