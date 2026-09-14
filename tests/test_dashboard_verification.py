from __future__ import annotations

from pathlib import Path

from harness.dashboard import read_dashboard_task_detail, render_task_page
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_checkpoint, task_comment, task_resume, task_start
from harness.tasks import TaskState, TaskWaitReason
from harness.verification import VerificationDraft, VerificationStatus


def _review_task(tmp_path: Path) -> tuple[Path, str, str, int]:
    root = tmp_path / "project"
    root.mkdir()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        started = task_start(connection, workspace.workspace_id, "Проверить результат")
        reviewed = task_checkpoint(
            connection,
            workspace.workspace_id,
            started.task_id,
            expected_revision=started.revision,
            state=TaskState.WAITING,
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
            summary="Отчёт с проверками",
            next_step="Оценить результаты проверок",
            verification=[
                VerificationDraft("Unit suite", VerificationStatus.PASSED, "12 passed"),
                VerificationDraft(
                    "API <script>bad</script>",
                    VerificationStatus.FAILED,
                    'assert expected == actual\n<img src=x onerror="bad()">',
                ),
                VerificationDraft("Real host", VerificationStatus.NOT_RUN, "Хост недоступен"),
            ],
        ).task
        return database, workspace.workspace_id, reviewed.task_id, reviewed.revision
    finally:
        connection.close()


def test_dashboard_shows_reported_status_evidence_and_keeps_human_accept(tmp_path: Path) -> None:
    database, _workspace_id, task_id, _revision = _review_task(tmp_path)
    html = render_task_page(read_dashboard_task_detail(database, task_id), base_path="/")

    assert "Проверки последнего отчёта" in html
    assert "Сообщено агентом" in html
    for status in ("passed", "failed", "not_run"):
        assert f'data-status="{status}"' in html
    assert "Пройдена" in html
    assert "Ошибка" in html
    assert "Не запускалась" in html
    assert "12 passed" in html
    assert "Хост недоступен" in html
    assert "API &lt;script&gt;bad&lt;/script&gt;" in html
    assert "assert expected == actual\n&lt;img src=x onerror=&quot;bad()&quot;&gt;" in html
    assert "<script>bad</script>" not in html
    assert 'name="action" value="accept"' in html
    assert "После этого отчёта задача обновлялась" not in html


def test_dashboard_marks_older_verification_without_claiming_current_success(
    tmp_path: Path,
) -> None:
    database, workspace_id, task_id, revision = _review_task(tmp_path)
    connection = connect_database(database)
    try:
        task_comment(
            connection, workspace_id, task_id, expected_revision=revision, comment="Нужно уточнение"
        )
    finally:
        connection.close()
    html = render_task_page(read_dashboard_task_detail(database, task_id), base_path="/")

    assert "После этого отчёта задача обновлялась" in html
    assert f"Отчёт r{revision}" in html
    assert "12 passed" in html


def test_latest_report_without_checks_does_not_promote_historical_verification(
    tmp_path: Path,
) -> None:
    database, workspace_id, task_id, revision = _review_task(tmp_path)
    connection = connect_database(database)
    try:
        resumed = task_resume(connection, workspace_id, task_id, expected_revision=revision)
        task_checkpoint(
            connection,
            workspace_id,
            task_id,
            expected_revision=resumed.revision,
            state=TaskState.WORKING,
            summary="Изменения после проверки",
        )
        other_root = tmp_path / "other"
        other_root.mkdir()
        other_project = create_project(connection)
        other_workspace = register_workspace(
            connection, project_id=other_project.project_id, path=other_root
        )
        other = task_start(connection, other_workspace.workspace_id, "Другая задача")
        task_checkpoint(
            connection,
            other_workspace.workspace_id,
            other.task_id,
            expected_revision=other.revision,
            state=TaskState.COMPLETED,
            summary="Чужой отчёт",
            verification=[VerificationDraft("Foreign", VerificationStatus.PASSED, "FOREIGN PROOF")],
        )
    finally:
        connection.close()
    html = render_task_page(read_dashboard_task_detail(database, task_id), base_path="/")
    latest_panel = html.split('class="panel verification-panel"', 1)[1].split("</section>", 1)[0]

    assert "В последнем отчёте проверки не указаны" in latest_panel
    assert "12 passed" not in latest_panel
    assert "12 passed" in html  # Historical evidence remains on its checkpoint.
    assert "FOREIGN PROOF" not in html


def test_paged_timeline_keeps_evidence_with_original_report(tmp_path: Path) -> None:
    database, workspace_id, task_id, revision = _review_task(tmp_path)
    connection = connect_database(database)
    try:
        resumed = task_resume(connection, workspace_id, task_id, expected_revision=revision)
        latest = task_checkpoint(
            connection,
            workspace_id,
            task_id,
            expected_revision=resumed.revision,
            state=TaskState.WORKING,
            summary="Последний отчёт без проверок",
        ).task
        for number in range(61):
            latest = task_comment(
                connection,
                workspace_id,
                task_id,
                expected_revision=latest.revision,
                comment=f"Уточнение {number}",
            ).task
    finally:
        connection.close()
    first = render_task_page(read_dashboard_task_detail(database, task_id), base_path="/")
    older = render_task_page(read_dashboard_task_detail(database, task_id, page=2), base_path="/")

    assert "12 passed" not in first
    assert "12 passed" in older
    for html in (first, older):
        latest_panel = html.split('class="panel verification-panel"', 1)[1].split("</section>", 1)[
            0
        ]
        assert "В последнем отчёте проверки не указаны" in latest_panel
        assert "12 passed" not in latest_panel
        assert "Последний отчёт без проверок" in html
