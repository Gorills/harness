from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import harness.retrieval as retrieval
from harness.dashboard import read_dashboard_home
from harness.registry import create_project, get_workspace, register_workspace
from harness.retrieval import (
    ProjectRetrievalError,
    ProjectSearchHit,
    read_project_context,
    search_tasks,
)
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_set_deployment, task_start
from harness.tasks import TaskOperatorStatus

_FIRST_ID = "a123456789" + "0" * 22
_SECOND_ID = "a123456789" + "1" * 22
_FOREIGN_ID = "a123456789" + "2" * 22
_OTHER_ID = "b987654321" + "0" * 22


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path]]:
    path = tmp_path / "harness.db"
    initialize_database(path)
    connection = connect_database(path)
    try:
        yield connection, path
    finally:
        connection.close()


def _workspace(connection: sqlite3.Connection, root: Path) -> str:
    root.mkdir()
    project = create_project(connection)
    return register_workspace(connection, project_id=project.project_id, path=root).workspace_id


def _task(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    title: str,
    *,
    checkpoints: tuple[str, ...] = (),
    comments: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Seed deterministic history directly; intermediate checkpoints remain working.

    Revisions and events describe creation, progress/comments, then one terminal checkpoint.
    Direct SQLite seeding keeps the long-history retrieval fixture independent of Git snapshots.
    """
    created_at = datetime(2026, 9, 11, tzinfo=UTC)
    revision = 1
    final_revision = 2 + len(checkpoints) + len(comments)
    connection.execute(
        "INSERT INTO tasks(id,workspace_id,title,state,revision,created_at,updated_at) "
        "VALUES (?,?,?,'completed',?,?,?)",
        (
            task_id,
            workspace_id,
            title,
            final_revision,
            created_at.isoformat(),
            (created_at + timedelta(seconds=final_revision)).isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO task_events(task_id,task_revision,event_type,created_at) "
        "VALUES (?,1,'created',?)",
        (task_id, created_at.isoformat()),
    )
    checkpoint_ids: list[str] = []
    event_ids: list[int] = []
    for summary in checkpoints:
        revision += 1
        checkpoint_id = f"{task_id}-checkpoint-{revision}"
        timestamp = (created_at + timedelta(seconds=revision)).isoformat()
        connection.execute(
            "INSERT INTO task_checkpoints(id,task_id,task_revision,state,summary,created_at,current_dirty_path_count) "
            "VALUES (?,?,?,'working',?,?,0)",
            (checkpoint_id, task_id, revision, summary, timestamp),
        )
        connection.execute(
            "INSERT INTO task_events(task_id,task_revision,event_type,checkpoint_id,created_at) "
            "VALUES (?,?,'checkpoint',?,?)",
            (task_id, revision, checkpoint_id, timestamp),
        )
        checkpoint_ids.append(checkpoint_id)
    for comment in comments:
        revision += 1
        cursor = connection.execute(
            "INSERT INTO task_events(task_id,task_revision,event_type,operator_comment,created_at) "
            "VALUES (?,?,'operator_comment',?,?)",
            (task_id, revision, comment, (created_at + timedelta(seconds=revision)).isoformat()),
        )
        assert cursor.lastrowid is not None
        event_ids.append(cursor.lastrowid)
    checkpoint_id = f"{task_id}-completed"
    connection.execute(
        "INSERT INTO task_checkpoints(id,task_id,task_revision,state,summary,created_at,current_dirty_path_count) "
        "VALUES (?,?,?,'completed','Latest checkpoint',?,0)",
        (
            checkpoint_id,
            task_id,
            final_revision,
            (created_at + timedelta(seconds=final_revision)).isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO task_events(task_id,task_revision,event_type,checkpoint_id,created_at) "
        "VALUES (?,?,'checkpoint',?,?)",
        (
            task_id,
            final_revision,
            checkpoint_id,
            (created_at + timedelta(seconds=final_revision)).isoformat(),
        ),
    )
    return tuple(checkpoint_ids), tuple(event_ids)


def _ids(hits: tuple[ProjectSearchHit, ...]) -> list[str]:
    return [hit.ref.removeprefix("task:").partition("#")[0] for hit in hits]


@pytest.mark.parametrize(
    ("deploy_test", "deploy_prod", "expected_status", "queries"),
    [
        (True, False, TaskOperatorStatus.DEPLOY_TEST, ("deploy_test",)),
        (False, True, TaskOperatorStatus.DEPLOY_PROD, ("deploy_prod",)),
        (
            True,
            True,
            TaskOperatorStatus.DEPLOY_BOTH,
            ("deploy_test", "deploy_prod", "деплой на тест", "деплой на прод"),
        ),
    ],
)
def test_historical_deployment_search_and_context_survive_clearing_current_flags(
    database: tuple[sqlite3.Connection, Path],
    tmp_path: Path,
    deploy_test: bool,
    deploy_prod: bool,
    expected_status: TaskOperatorStatus,
    queries: tuple[str, ...],
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    task = task_start(connection, workspace_id, "Подготовить релиз")
    marked = task_set_deployment(
        connection,
        workspace_id,
        task.task_id,
        expected_revision=task.revision,
        deploy_test=deploy_test,
        deploy_prod=deploy_prod,
    )
    task_set_deployment(
        connection,
        workspace_id,
        task.task_id,
        expected_revision=marked.task.revision,
        deploy_test=False,
        deploy_prod=False,
    )
    expected_ref = f"task:{task.task_id}#event:{marked.event.event_id}"
    for query in queries:
        hits = search_tasks(
            connection,
            query,
            limit=5,
            project_id=get_workspace(connection, workspace_id).project_id,
        )
        assert [hit.ref for hit in hits] == [expected_ref]
        assert hits[0].short_summary == expected_status.value
        (context,) = read_project_context(connection, workspace_id, (hits[0].ref,))
        assert context.data["operator_status"] is None
        assert context.data["selected_event"] == {
            "event_id": marked.event.event_id,
            "task_revision": marked.task.revision,
            "event_type": "operator_status_updated",
            "operator_status": expected_status.value,
            "operator_status_truncated": False,
            "created_at": marked.event.created_at,
        }
    (history,) = read_project_context(connection, workspace_id, (f"task:{task.task_id}",))
    recent_history = history.data["recent_history"]
    assert isinstance(recent_history, list)
    assert any(
        isinstance(event, dict) and event.get("operator_status") == expected_status.value
        for event in recent_history
    )


@pytest.mark.parametrize("legacy_status", ["deploy_test", "deploy_prod"])
def test_legacy_deployment_event_keeps_search_and_context_payload(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path, legacy_status: str
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    _task(connection, workspace_id, _FIRST_ID, "Legacy release")
    cursor = connection.execute(
        "INSERT INTO task_events(task_id,task_revision,event_type,operator_status,created_at) "
        "VALUES (?,3,'operator_status_updated',?,'legacy-time')",
        (_FIRST_ID, legacy_status),
    )
    hits = search_tasks(
        connection,
        legacy_status,
        limit=5,
        project_id=get_workspace(connection, workspace_id).project_id,
    )
    assert [hit.ref for hit in hits] == [f"task:{_FIRST_ID}#event:{cursor.lastrowid}"]
    assert hits[0].short_summary == legacy_status
    (context,) = read_project_context(connection, workspace_id, (hits[0].ref,))
    selected_event = context.data["selected_event"]
    assert isinstance(selected_event, dict)
    assert selected_event["operator_status"] == legacy_status


@pytest.mark.parametrize("fragment_kind", ["checkpoint", "operator_comment"])
def test_long_task_history_does_not_hide_other_matching_tasks(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path, fragment_kind: str
) -> None:
    connection, path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    repeated = ("needle",) * 400
    _task(
        connection,
        workspace_id,
        _FIRST_ID,
        "Long history",
        checkpoints=repeated if fragment_kind == "checkpoint" else (),
        comments=repeated if fragment_kind == "operator_comment" else (),
    )
    weaker_summary = "needle " + "details " * 200
    weaker_checkpoints, _ = _task(
        connection,
        workspace_id,
        _OTHER_ID,
        "Separate work",
        checkpoints=(weaker_summary,),
    )
    for hits in (
        search_tasks(connection, "needle", limit=24),
        search_tasks(
            connection,
            "needle",
            limit=24,
            project_id=get_workspace(connection, workspace_id).project_id,
        ),
        read_dashboard_home(path, search_query="needle").task_search_results,
    ):
        assert set(_ids(hits)) == {_FIRST_ID, _OTHER_ID}
        assert len(hits) == 2
        weaker = next(hit for hit in hits if _OTHER_ID in hit.ref)
        assert weaker.ref == f"task:{_OTHER_ID}#checkpoint:{weaker_checkpoints[0]}"
        assert weaker.short_summary is not None
        assert weaker.short_summary.startswith("needle details")


def test_selected_best_fragment_preserves_verification_and_event_details(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    checkpoint_ids, event_ids = _task(
        connection,
        workspace_id,
        _FIRST_ID,
        "Credential work",
        checkpoints=("rotation old behavior", "rotation verification passed"),
        comments=("operator requested preserving legacy behavior",),
    )
    connection.execute(
        "INSERT INTO task_checkpoint_verification(checkpoint_id,position,name,status,evidence,source) "
        "VALUES (?,0,'focused suite','passed','27 assertions passed','agent_reported')",
        (checkpoint_ids[1],),
    )
    hits = search_tasks(connection, "rotation verification", limit=24)
    assert len(hits) == 1
    assert hits[0].ref == f"task:{_FIRST_ID}#checkpoint:{checkpoint_ids[1]}"
    assert hits[0].short_summary == "rotation verification passed"
    context = read_project_context(connection, workspace_id, (hits[0].ref,))
    selected = context[0].data["selected_checkpoint"]
    assert isinstance(selected, dict)
    assert selected["verification_count"] == 1
    assert selected["verification"][0]["evidence"] == "27 assertions passed"
    comments = search_tasks(connection, "operator requested", limit=24)
    assert comments[0].ref == f"task:{_FIRST_ID}#event:{event_ids[0]}"
    assert comments[0].short_summary == "operator requested preserving legacy behavior"


def test_title_phrase_keeps_priority_over_a_repetitive_matching_checkpoint(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    _task(
        connection,
        workspace_id,
        _FIRST_ID,
        "alpha beta " + "context " * 20,
        checkpoints=("alpha beta " * 50,),
    )
    hits = search_tasks(connection, "alpha beta", limit=24)
    assert len(hits) == 1
    assert hits[0].ref == f"task:{_FIRST_ID}"
    assert hits[0].match_reason == "Task title"
    assert hits[0].short_summary == "Latest checkpoint"


def test_all_query_terms_outrank_a_frequently_repeated_partial_match(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    checkpoint_ids, _ = _task(
        connection,
        workspace_id,
        _FIRST_ID,
        "Terminology review",
        checkpoints=("alpha " * 100, "alpha beta " + "context " * 300),
    )
    hits = search_tasks(connection, "alpha beta", limit=24)
    assert len(hits) == 1
    assert hits[0].ref == f"task:{_FIRST_ID}#checkpoint:{checkpoint_ids[1]}"
    assert hits[0].short_summary is not None
    assert hits[0].short_summary.startswith("alpha beta context")


def test_successive_queries_on_one_connection_do_not_reuse_previous_terms(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    checkpoint_ids, _ = _task(
        connection,
        workspace_id,
        _FIRST_ID,
        "Terminology review",
        checkpoints=("alpha beta", "alpha gamma", "alpha"),
    )
    for query, checkpoint_index in (
        ("alpha beta", 0),
        ("alpha gamma", 1),
        ("alpha", 2),
        ("beta alpha", 0),
    ):
        hits = search_tasks(connection, query, limit=24)
        assert len(hits) == 1
        assert hits[0].ref == f"task:{_FIRST_ID}#checkpoint:{checkpoint_ids[checkpoint_index]}"


def test_ranking_failure_does_not_poison_the_next_query_on_the_connection(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    checkpoint_ids, _ = _task(
        connection,
        workspace_id,
        _FIRST_ID,
        "Terminology review",
        checkpoints=("alpha beta", "alpha gamma"),
    )

    def fail_matching(_terms: tuple[str, ...], *_values: str) -> int:
        raise RuntimeError("private-ranking-detail")

    with monkeypatch.context() as failing:
        failing.setattr(retrieval, "matching_term_count", fail_matching)
        with pytest.raises(ProjectRetrievalError) as error:
            search_tasks(connection, "alpha beta", limit=24)
        assert "private-ranking-detail" not in str(error.value)
    hits = search_tasks(connection, "alpha gamma", limit=24)
    assert len(hits) == 1
    assert hits[0].ref == f"task:{_FIRST_ID}#checkpoint:{checkpoint_ids[1]}"


@pytest.mark.parametrize(
    "query", [_FIRST_ID, _FIRST_ID.upper(), _FIRST_ID[:10], f"task:{_FIRST_ID}"]
)
def test_task_full_id_and_displayed_prefix_find_the_latest_task_summary(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path, query: str
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    _task(connection, workspace_id, _FIRST_ID, "Credential rotation")
    hits = search_tasks(connection, query, limit=24)
    assert _ids(hits) == [_FIRST_ID]
    assert hits[0].ref == f"task:{_FIRST_ID}"
    assert hits[0].short_summary == "Latest checkpoint"


def test_ambiguous_task_prefix_returns_each_matching_task_with_project_scope(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    connection, path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    other_workspace_id = _workspace(connection, tmp_path / "other")
    _task(connection, workspace_id, _FIRST_ID, "First local work")
    _task(connection, workspace_id, _SECOND_ID, "Second local work")
    _task(connection, other_workspace_id, _FOREIGN_ID, "Foreign private work")
    prefix = _FIRST_ID[:10]
    global_hits = read_dashboard_home(path, search_query=prefix).task_search_results
    assert set(_ids(global_hits)) == {_FIRST_ID, _SECOND_ID, _FOREIGN_ID}
    assert len(global_hits) == 3
    project_id = get_workspace(connection, workspace_id).project_id
    local_hits = search_tasks(connection, prefix, limit=24, project_id=project_id)
    assert set(_ids(local_hits)) == {_FIRST_ID, _SECOND_ID}
    assert "Foreign private work" not in str(local_hits)
    assert search_tasks(connection, _FOREIGN_ID, limit=24, project_id=project_id) == ()
    assert len(search_tasks(connection, prefix, limit=1)) == 1
    assert search_tasks(connection, prefix[:9], limit=24) == ()


@pytest.mark.parametrize("query", [_FIRST_ID, _FIRST_ID[:10]])
def test_task_id_match_precedes_incidental_lexical_mentions(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path, query: str
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    _task(connection, workspace_id, _OTHER_ID, f"{query} mentioned by another task")
    _task(connection, workspace_id, _FIRST_ID, "Credential rotation")
    assert _ids(search_tasks(connection, query, limit=24))[0] == _FIRST_ID


@pytest.mark.parametrize("legacy_id", ["opaque-legacy-identity", "task:opaque-legacy-identity"])
def test_opaque_legacy_task_id_matches_exactly_without_arbitrary_text_prefixes(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path, legacy_id: str
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    _task(connection, workspace_id, legacy_id, "Credential rotation")
    for query in (legacy_id, f"task:{legacy_id}"):
        assert _ids(search_tasks(connection, query, limit=24)) == [legacy_id]
    assert search_tasks(connection, "opaque-legacy", limit=24) == ()


def test_exact_legacy_id_takes_precedence_over_a_generated_id_prefix(
    database: tuple[sqlite3.Connection, Path], tmp_path: Path
) -> None:
    connection, _path = database
    workspace_id = _workspace(connection, tmp_path / "repo")
    legacy_id = _FIRST_ID[:10]
    _task(connection, workspace_id, _FIRST_ID, "Generated identity")
    _task(connection, workspace_id, legacy_id, "Legacy identity")
    assert _ids(search_tasks(connection, legacy_id, limit=24)) == [legacy_id]
