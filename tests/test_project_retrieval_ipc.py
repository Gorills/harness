from __future__ import annotations

import json
import socket
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.daemon import serve_daemon
from harness.git_applicability import WorkspaceApplicability
from harness.index import scan_workspace
from harness.ipc import (
    IpcProtocolError,
    IpcRemoteError,
    request_project_context,
    request_project_recall,
    request_workspace_status,
)
from harness.registry import create_project, register_workspace
from harness.retrieval import ProjectSearchKind
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


def _send_raw(socket_path: Path, method: str, params: dict[str, object]) -> dict[str, object]:
    raw = {
        "version": 1,
        "request_id": method,
        "method": method,
        "params": params,
    }
    encoded = (json.dumps(raw, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(socket_path))
        client.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            response.extend(client.recv(4096))
    parsed = json.loads(response)
    assert isinstance(parsed, dict)
    return parsed


def test_raw_surrogate_inputs_are_rejected_and_daemon_stays_live(tmp_path: Path) -> None:
    root, database, _project_id, workspace_id = _seed(tmp_path)
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
    hint = {"path": str(root), "source": "test", "match_mode": "location"}
    invalid = "\ud800"
    cases: tuple[tuple[str, dict[str, object]], ...] = (
        ("project_recall", {"hints": [hint], "query": invalid, "kind": "knowledge", "limit": 1}),
        ("project_context", {"hints": [hint], "refs": [invalid]}),
        ("workspace_skills_reconcile", {"hints": [hint], "profiles": [invalid]}),
        ("skill_cleanup", {"profiles": [invalid]}),
        ("scan_workspace", {"path": str(root / invalid)}),
        ("workspace_status", {"hints": [{**hint, "path": str(root / invalid)}]}),
        ("workspace_status", {"hints": [{**hint, "source": invalid}]}),
    )
    try:
        for method, params in cases:
            rejected = _send_raw(socket_path, method, params)
            assert rejected["ok"] is False, method
            error = rejected["error"]
            assert isinstance(error, dict)
            assert error["code"] == "invalid_request", method
            assert request_workspace_status(socket_path, hints).workspace_id == workspace_id
            assert not future.done(), method
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_recall_scans_past_newer_hidden_cards_before_limit(tmp_path: Path) -> None:
    root, database, project_id, _workspace_id = _seed(tmp_path)
    connection = connect_database(database)
    try:
        for index in range(300):
            knowledge_id = f"a{index:03d}"
            connection.execute(
                """INSERT INTO knowledge_cards(
                    id, project_id, kind, title, body, source_type, created_at, updated_at, freshness
                ) VALUES (?, ?, 'invariant', 'Refresh rotation decoy',
                          'Previous token invalid', 'operator', 'z', 'z', 'fresh')""",
                (knowledge_id, project_id),
            )
            connection.execute(
                """INSERT INTO knowledge_anchors(
                    knowledge_id, workspace_id, relative_path, symbol, fingerprint_kind,
                    content_sha256
                ) VALUES (?, ?, 'docs/rotation.md', '', 'file', ?)""",
                (knowledge_id, _workspace_id, "0" * 64),
            )
    finally:
        connection.close()
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    try:
        hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
        result = request_project_recall(
            socket_path, hints, "refresh rotation", ProjectSearchKind.KNOWLEDGE, limit=1
        )
        assert [hit.ref for hit in result.results] == ["knowledge:card"]
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_recall_ipc_timeout_allows_valid_slow_applicability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, _project_id, _workspace_id = _seed(tmp_path)
    original = WorkspaceApplicability.knowledge_visible

    def delayed(self: WorkspaceApplicability, card: object) -> bool:
        time.sleep(2.2)
        return original(self, card)  # type: ignore[arg-type]

    monkeypatch.setattr(WorkspaceApplicability, "knowledge_visible", delayed)
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    try:
        hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
        result = request_project_recall(
            socket_path, hints, "refresh rotation", ProjectSearchKind.KNOWLEDGE, limit=1
        )
        assert [hit.ref for hit in result.results] == ["knowledge:card"]
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_knowledge_recall_deadline_is_explicit_and_daemon_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, _project_id, workspace_id = _seed(tmp_path)
    import harness.retrieval as retrieval

    calls = 0

    def expired_clock() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 5.0

    monkeypatch.setattr(retrieval, "monotonic", expired_clock)
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
    try:
        with pytest.raises(IpcRemoteError) as error:
            request_project_recall(
                socket_path, hints, "refresh rotation", ProjectSearchKind.KNOWLEDGE, limit=1
            )
        assert error.value.code == "recall_timeout"
        assert request_workspace_status(socket_path, hints).workspace_id == workspace_id
        assert not future.done()
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_project_recall_returns_only_durable_preview_and_validates_input(tmp_path: Path) -> None:
    root, database, project_id, _workspace_id = _seed(tmp_path)
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
    try:
        result = request_project_recall(
            socket_path, hints, "refresh rotation", ProjectSearchKind.KNOWLEDGE
        )
        assert result.project_id == project_id
        assert result.query == "refresh rotation"
        assert result.kind is ProjectSearchKind.KNOWLEDGE
        assert len(result.results) == 1
        hit = result.results[0]
        assert hit.ref == "knowledge:card"
        assert hit.title == "Refresh rotation invariant"
        assert hit.short_summary is None
        assert hit.freshness == "fresh"
        assert "Previous token becomes invalid" not in repr(result)

        with pytest.raises(IpcProtocolError, match="kind"):
            request_project_recall(socket_path, hints, "refresh", ProjectSearchKind.CODE)
        with pytest.raises(IpcProtocolError, match="limit"):
            request_project_recall(
                socket_path, hints, "refresh", ProjectSearchKind.KNOWLEDGE, limit=6
            )
        with pytest.raises(IpcProtocolError, match="query"):
            request_project_recall(socket_path, hints, " " * 4, ProjectSearchKind.KNOWLEDGE)
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_project_context_round_trips_through_strict_daemon_ipc(tmp_path: Path) -> None:
    root, database, project_id, _workspace_id = _seed(tmp_path)
    socket_path = tmp_path / "runtime" / "harness.sock"
    stop, executor, future = _start(database, socket_path)
    hints = (WorkspaceHint(root, "test", WorkspaceHintMatchMode.LOCATION),)
    try:
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
