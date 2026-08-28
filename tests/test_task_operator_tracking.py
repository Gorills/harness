from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.storage as storage
import harness.task_workflow as task_workflow
from harness.dashboard import (
    read_dashboard_task_detail,
    read_dashboard_workspace_detail,
    render_task_page,
    render_workspace_page,
)
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectSearchScope, search_project
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import TaskEventType, list_task_events
from harness.task_workflow import (
    task_checkpoint,
    task_comment,
    task_reopen,
    task_set_jira_url,
    task_set_operator_status,
    task_start,
)
from harness.tasks import (
    TaskConflictError,
    TaskOperatorStatus,
    TaskRevisionConflictError,
    TaskState,
    TaskValidationError,
    get_task,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _database(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "init", "-b", "feature/HAR-42-dashboard")
    _git(root, "add", "tracked.txt")
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
    return database, connection, workspace.workspace_id


def test_schema_v12_migrates_operator_fields_events_and_branch_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "migration.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 12)
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace', 'project', '/repo', '/repo/.git')
            """
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
            ) VALUES ('task', 'workspace', 'Existing task', 'working', NULL, 2, 'c', 'u')
            """
        )
        connection.execute(
            """
            INSERT INTO task_baselines(
                task_id, head, branch, captured_at, index_is_fresh,
                index_file_count, index_snapshot_sha256
            ) VALUES ('task', NULL, 'feature/MIG-13', 'c', 1, 0, ?)
            """,
            ("0" * 64,),
        )
        cursor = connection.execute(
            """
            INSERT INTO task_events(
                task_id, task_revision, event_type, checkpoint_id,
                operator_feedback, created_at
            ) VALUES ('task', 2, 'operator_feedback', NULL, 'Keep this note', 'u')
            """
        )
        old_event_id = cursor.lastrowid
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", 13)
    status = initialize_database(database)
    assert status.schema_version == 13
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT jira_url, operator_status FROM tasks WHERE id = 'task'"
        ).fetchone() == (None, None)
        assert connection.execute(
            "SELECT id, operator_feedback, operator_comment FROM task_events"
        ).fetchall() == [(old_event_id, "Keep this note", None)]
        assert connection.execute(
            "SELECT task_id FROM task_search WHERE task_search MATCH 'MIG'"
        ).fetchall() == [("task",)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO task_events(
                    task_id, task_revision, event_type, checkpoint_id,
                    operator_feedback, operator_comment, jira_url, operator_status, created_at
                ) VALUES ('task', 3, 'operator_comment', NULL, NULL, NULL, NULL, NULL, 'bad')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tasks SET operator_status = 'custom_status' WHERE id = 'task'"
            )
    finally:
        connection.close()


def test_operator_tracking_reopen_and_task_search_are_one_cas_history(tmp_path: Path) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        started = task_start(connection, workspace_id, "Подготовить релиз HAR-42")
        commented = task_comment(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=started.revision,
            comment="Проверить rollout после smoke-теста",
        )
        linked = task_set_jira_url(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=commented.task.revision,
            jira_url="https://jira.example/browse/HAR-42",
        )
        marked = task_set_operator_status(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=linked.task.revision,
            operator_status=TaskOperatorStatus.DEPLOY_TEST,
        )
        completed = task_checkpoint(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=marked.task.revision,
            state=TaskState.COMPLETED,
            summary="Релиз-кандидат собран",
        ).task
        reopened = task_reopen(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=completed.revision,
        )

        assert reopened.task.task_id == started.task_id
        assert reopened.task.state is TaskState.WORKING
        assert reopened.task.jira_url == "https://jira.example/browse/HAR-42"
        assert reopened.task.operator_status is TaskOperatorStatus.DEPLOY_TEST
        assert reopened.task.revision == 6
        assert tuple(
            event.event_type for event in list_task_events(connection, started.task_id)
        ) == (
            TaskEventType.CREATED,
            TaskEventType.OPERATOR_COMMENT,
            TaskEventType.JIRA_LINK_UPDATED,
            TaskEventType.OPERATOR_STATUS_UPDATED,
            TaskEventType.CHECKPOINT,
            TaskEventType.REOPENED,
        )

        for query in (
            "rollout",
            "HAR-42",
            "feature/HAR-42-dashboard",
            "деплой на тест",
        ):
            hits = search_project(
                connection,
                workspace_id,
                query,
                scope=ProjectSearchScope.TASKS,
                limit=8,
            )
            assert hits and hits[0].ref.startswith(f"task:{started.task_id}")

        with pytest.raises(TaskRevisionConflictError):
            task_comment(
                connection,
                workspace_id,
                started.task_id,
                expected_revision=completed.revision,
                comment="Устаревший комментарий",
            )
    finally:
        connection.close()


def test_reopen_obeys_one_working_task_and_metadata_validation(tmp_path: Path) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        first = task_start(connection, workspace_id, "Первая задача")
        completed = task_checkpoint(
            connection,
            workspace_id,
            first.task_id,
            expected_revision=first.revision,
            state=TaskState.COMPLETED,
            summary="Готово",
        ).task
        second = task_start(connection, workspace_id, "Вторая задача")

        with pytest.raises(TaskConflictError):
            task_reopen(
                connection,
                workspace_id,
                completed.task_id,
                expected_revision=completed.revision,
            )
        assert get_task(connection, completed.task_id) == completed
        assert get_task(connection, second.task_id).state is TaskState.WORKING

        with pytest.raises(TaskValidationError):
            task_set_jira_url(
                connection,
                workspace_id,
                second.task_id,
                expected_revision=second.revision,
                jira_url="javascript:alert(1)",
            )
        with pytest.raises(TaskValidationError):
            task_set_jira_url(
                connection,
                workspace_id,
                second.task_id,
                expected_revision=second.revision,
                jira_url="https://[malformed",
            )
        assert get_task(connection, second.task_id).revision == second.revision
    finally:
        connection.close()


def test_dashboard_renders_operator_tracking_and_task_search(tmp_path: Path) -> None:
    database, connection, workspace_id = _database(tmp_path)
    try:
        started = task_start(connection, workspace_id, "Релиз дашборда")
        commented = task_comment(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=started.revision,
            comment="Комментарий <script>alert(1)</script> rollout",
        )
        linked = task_set_jira_url(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=commented.task.revision,
            jira_url="https://jira.example/browse/HAR-42",
        )
        task_set_operator_status(
            connection,
            workspace_id,
            started.task_id,
            expected_revision=linked.task.revision,
            operator_status=TaskOperatorStatus.DEPLOY_PROD,
        )
    finally:
        connection.close()

    task_detail = read_dashboard_task_detail(database, started.task_id)
    task_html = render_task_page(task_detail, base_path="/capability/")
    assert "Деплой на прод" in task_html
    assert "https://jira.example/browse/HAR-42" in task_html
    assert "Добавить комментарий" in task_html
    assert "Комментарий &lt;script&gt;alert(1)&lt;/script&gt; rollout" in task_html
    assert "Комментарий <script>" not in task_html

    workspace_detail = read_dashboard_workspace_detail(
        database,
        workspace_id,
        search_query="rollout",
    )
    assert workspace_detail.task_search_results
    workspace_html = render_workspace_page(workspace_detail, base_path="/capability/")
    assert "Релиз дашборда" in workspace_html
    assert f"/capability/tasks/{started.task_id}/" in workspace_html


def test_operator_tracking_event_failure_rolls_back_task_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database_path, connection, workspace_id = _database(tmp_path)
    try:
        started = task_start(connection, workspace_id, "Rollback operator note")

        def fail_event(*_args: object, **_kwargs: object) -> object:
            raise sqlite3.IntegrityError("injected event failure")

        monkeypatch.setattr(task_workflow, "_insert_operator_event", fail_event)
        with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
            task_comment(
                connection,
                workspace_id,
                started.task_id,
                expected_revision=started.revision,
                comment="Must roll back",
            )

        assert get_task(connection, started.task_id) == started
        assert tuple(
            event.event_type for event in list_task_events(connection, started.task_id)
        ) == (TaskEventType.CREATED,)
    finally:
        connection.close()
