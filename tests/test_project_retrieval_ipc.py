from __future__ import annotations

import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.daemon import serve_daemon
from harness.index import scan_workspace
from harness.ipc import (
    IpcProtocolError,
    IpcRemoteError,
    request_project_context,
    request_project_search,
)
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectSearchKind, ProjectSearchScope
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _seed(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "rotation.md").write_text("rotation\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-m", "init")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type, created_at, updated_at, freshness
            ) VALUES ('card', ?, 'invariant', 'Refresh rotation invariant',
                      'Previous token becomes invalid', 'operator', 'c', 'u', 'fresh')
            """,
            (project.project_id,),
        )
        connection.execute(
            """
            INSERT INTO tasks(id, workspace_id, title, state, wait_reason, revision, created_at, updated_at)
            VALUES ('task', ?, 'Rotate refresh tokens', 'completed', NULL, 1, 'c', 'u')
            """,
            (workspace.workspace_id,),
        )
        return root, database, project.project_id, workspace.workspace_id
    finally:
        connection.close()


def _start(database: Path, socket_path: Path) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop)
    deadline = time.monotonic() + 3
    while not socket_path.exists():
        if future.done():
            future.result()
        if time.monotonic() >= deadline:
            raise AssertionError("daemon did not start")
        time.sleep(0.01)
    return stop, executor, future


def test_project_retrieval_round_trips_through_strict_daemon_ipc(tmp_path: Path) -> None:
    root, database, project_id, workspace_id = _seed(tmp_path)
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
    try:
        searched = request_project_search(
            socket_path,
            hints,
            "refresh rotation",
            scope=ProjectSearchScope.KNOWLEDGE,
            limit=3,
        )
        assert searched.project_id == project_id
        assert searched.workspace_id == workspace_id
        assert searched.results[0].ref == "knowledge:card"
        assert searched.results[0].kind is ProjectSearchKind.KNOWLEDGE
        assert searched.results[0].evidence is None

        docs = request_project_search(
            socket_path,
            hints,
            "rotation",
            scope=ProjectSearchScope.DOCS,
            limit=1,
        )
        assert docs.workspace_state == "current"
        assert docs.exact_coverage is not None
        assert docs.exact_coverage.complete is True
        assert docs.exact_coverage.matched_occurrences == 1
        assert docs.exact_coverage.locations[0].path == "docs/rotation.md"
        assert docs.results[0].ref == "doc:docs/rotation.md"
        assert docs.results[0].evidence is not None
        assert docs.results[0].evidence.snippet.strip() == "rotation"
        assert docs.results[0].evidence_reason is None
        assert set(docs.results[0].evidence.to_wire()) == {
            "start_line",
            "end_line",
            "snippet",
            "truncated",
        }

        context = request_project_context(
            socket_path, hints, ("knowledge:card", "doc:docs/rotation.md")
        )
        assert context.project_id == project_id
        assert [item.ref for item in context.items] == ["knowledge:card", "doc:docs/rotation.md"]
        assert context.items[0].data["body"] == "Previous token becomes invalid"

        with pytest.raises(IpcProtocolError, match="unique"):
            request_project_context(socket_path, hints, ("knowledge:card", "knowledge:card"))
        with pytest.raises(IpcRemoteError) as invalid_ref:
            request_project_context(socket_path, hints, ("knowledge:missing",))
        assert invalid_ref.value.code == "context_ref_error"
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()
