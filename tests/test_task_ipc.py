from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

import pytest

import harness.git_workspace as git_workspace_module
import harness.ipc as ipc_module
import harness.knowledge as knowledge_module
import harness.task_changes as task_changes_module
from harness.daemon import serve_daemon
from harness.index import scan_workspace
from harness.ipc import (
    PROTOCOL_VERSION,
    IpcRemoteError,
    TaskStartResult,
    request_status,
    request_task_checkpoint,
    request_task_start,
    request_workspace_task_status,
)
from harness.knowledge import KnowledgeDraft, KnowledgeKind
from harness.registry import create_project, register_workspace
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.tasks import TaskState, TaskWaitReason, get_task_stack_hints
from harness.workspace_resolution import WorkspaceHint

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX IPC slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path, *, content: str = "tracked\n") -> None:
    path.mkdir()
    (path / "tracked.txt").write_text(content, encoding="utf-8")
    _git(path, "init")
    _git(path, "add", "tracked.txt")
    _git(
        path,
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


def _registered_database(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    _make_repo(root)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        return root, database, workspace.workspace_id
    finally:
        connection.close()


def _start_server(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop_event)
    deadline = time.monotonic() + 3
    while not socket_path.exists():
        if future.done():
            future.result()
        if time.monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon socket did not appear")
        time.sleep(0.01)
    return stop_event, executor, future


def _stop_server(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def _raw_request(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(socket_path))
        client.sendall(encoded)
        response = bytearray()
        while not response.endswith(b"\n"):
            response.extend(client.recv(4096))
    value: object = json.loads(response.decode("utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_default_task_ipc_timeout_exceeds_stacked_checkpoint_mechanical_budgets() -> None:
    # task_checkpoint can legitimately spend these bounded phases sequentially:
    # Workspace identity resolution (three rev-parse calls), changed-file capture,
    # then Knowledge anchor capture. The client must remain connected beyond all
    # of them so a successful commit cannot become an ambiguous transport timeout.
    stacked_budget = (
        3 * git_workspace_module._GIT_COMMAND_TIMEOUT_SECONDS
        + task_changes_module._CHANGED_FILES_TIMEOUT_SECONDS
        + knowledge_module._KNOWLEDGE_ANCHOR_CAPTURE_TIMEOUT_SECONDS
    )
    assert ipc_module._TASK_REQUEST_TIMEOUT_SECONDS >= stacked_budget + 10.0


def test_task_start_and_checkpoint_round_trip_is_bounded_and_atomic(tmp_path: Path) -> None:
    root, database, workspace_id = _registered_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    secret_body = "SECRET-SOURCE-LIKE semantic body that must not be echoed"
    try:
        started = request_task_start(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            title="Investigate token rotation",
            stack_hints=(" FastAPI ", "POSTGRES"),
        )
        assert started == TaskStartResult(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace_id,
            task_id=started.task_id,
            state=TaskState.WORKING,
            wait_reason=None,
            revision=1,
        )
        with pytest.raises(IpcRemoteError) as conflict_info:
            request_task_start(
                socket_path,
                [WorkspaceHint(root, "explicit-root")],
                title="Must not replace working Task",
            )
        assert conflict_info.value.code == "task_conflict"

        checkpoint_raw = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "bounded-checkpoint",
                "method": "task_checkpoint",
                "params": {
                    "hints": [
                        {
                            "path": str(root.resolve()),
                            "source": "explicit-root",
                            "match_mode": "root",
                        }
                    ],
                    "task_id": started.task_id,
                    "expected_revision": 1,
                    "state": "working",
                    "summary": "Recorded one invariant",
                    "next_step": None,
                    "wait_reason": None,
                    "knowledge": [
                        {
                            "kind": "invariant",
                            "title": "Tracked file is authoritative",
                            "body": secret_body,
                            "anchors": [{"path": "tracked.txt", "symbol": "token"}],
                        }
                    ],
                },
            },
        )
        checkpoint_result = checkpoint_raw["result"]
        assert isinstance(checkpoint_result, dict)
        assert set(checkpoint_result) == {
            "schema_version",
            "workspace_id",
            "task_id",
            "state",
            "wait_reason",
            "revision",
            "checkpoint_id",
            "knowledge_ids",
        }
        assert checkpoint_result["schema_version"] == SCHEMA_VERSION
        assert checkpoint_result["workspace_id"] == workspace_id
        assert checkpoint_result["task_id"] == started.task_id
        assert checkpoint_result["state"] == "working"
        assert checkpoint_result["wait_reason"] is None
        assert checkpoint_result["revision"] == 2
        checkpoint_id = checkpoint_result["checkpoint_id"]
        knowledge_ids = checkpoint_result["knowledge_ids"]
        assert isinstance(checkpoint_id, str) and checkpoint_id
        assert isinstance(knowledge_ids, list) and len(knowledge_ids) == 1
        assert isinstance(knowledge_ids[0], str) and knowledge_ids[0]
        checkpoint_serialized = json.dumps(checkpoint_raw, sort_keys=True)
        for forbidden in (
            secret_body,
            "anchors",
            "changed_paths",
            "baseline_head",
            "index_snapshot_sha256",
            "source_checkpoint_id",
        ):
            assert forbidden not in checkpoint_serialized

        raw = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "bounded-resume",
                "method": "task_start",
                "params": {
                    "hints": [
                        {
                            "path": str(root.resolve()),
                            "source": "explicit-root",
                            "match_mode": "root",
                        }
                    ],
                    "task_id": started.task_id,
                },
            },
        )
        assert raw == {
            "version": PROTOCOL_VERSION,
            "request_id": "bounded-resume",
            "ok": True,
            "result": {
                "schema_version": SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "task_id": started.task_id,
                "state": "working",
                "wait_reason": None,
                "revision": 2,
            },
        }
    finally:
        _stop_server(stop_event, executor, future)

    connection = connect_database(database)
    try:
        task_row = connection.execute(
            "SELECT revision, state FROM tasks WHERE id = ?", (started.task_id,)
        ).fetchone()
        assert task_row == (2, "working")
        assert connection.execute(
            "SELECT COUNT(*) FROM task_checkpoints WHERE task_id = ?", (started.task_id,)
        ).fetchone() == (1,)
        assert get_task_stack_hints(connection, started.task_id) == ("fastapi", "postgres")
        knowledge_row = connection.execute(
            "SELECT body, source_task_id, source_checkpoint_id FROM knowledge_cards"
        ).fetchone()
        assert knowledge_row == (secret_body, started.task_id, checkpoint_id)
    finally:
        connection.close()


def test_waiting_resume_requires_revision_and_working_resume_is_idempotent(tmp_path: Path) -> None:
    root, database, _workspace_id = _registered_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        started = request_task_start(
            socket_path, [WorkspaceHint(root, "explicit-root")], title="Wait for operator"
        )
        waiting = request_task_checkpoint(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            started.task_id,
            expected_revision=1,
            state=TaskState.WAITING,
            summary="Need operator review",
            next_step="Review the proposed change",
            wait_reason=TaskWaitReason.OPERATOR_REVIEW,
        )
        assert waiting.revision == 2
        assert waiting.state is TaskState.WAITING

        with pytest.raises(IpcRemoteError) as exc_info:
            request_task_start(
                socket_path,
                [WorkspaceHint(root, "explicit-root")],
                task_id=started.task_id,
            )
        assert exc_info.value.code == "task_validation_error"

        resumed = request_task_start(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            task_id=started.task_id,
            expected_revision=2,
        )
        assert resumed.state is TaskState.WORKING
        assert resumed.revision == 3

        idempotent = request_task_start(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            task_id=started.task_id,
            expected_revision=1,
        )
        assert idempotent == resumed
    finally:
        _stop_server(stop_event, executor, future)

    connection = connect_database(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND event_type = 'resumed'",
            (started.task_id,),
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_task_start_wire_rejects_stack_hints_on_resume_and_malformed_hints(tmp_path: Path) -> None:
    root, database, _workspace_id = _registered_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        started = request_task_start(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            title="Wire hints",
            stack_hints=("fastapi",),
        )
        hints = [
            {
                "path": str(root.resolve()),
                "source": "explicit-root",
                "match_mode": "location",
            }
        ]
        resume_with_hints = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "resume-hints",
                "method": "task_start",
                "params": {
                    "hints": hints,
                    "task_id": started.task_id,
                    "stack_hints": ["postgres"],
                },
            },
        )
        malformed_create = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "malformed-hints",
                "method": "task_start",
                "params": {"hints": hints, "title": "Bad hints", "stack_hints": "fastapi"},
            },
        )

        assert resume_with_hints["error"] == {
            "code": "invalid_request",
            "message": "IPC request is invalid",
        }
        assert malformed_create["error"] == {
            "code": "invalid_request",
            "message": "IPC request is invalid",
        }
    finally:
        _stop_server(stop_event, executor, future)


def test_workspace_task_status_exposes_only_relevant_task_continuity(tmp_path: Path) -> None:
    root, database, workspace_id = _registered_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        started = request_task_start(
            socket_path, [WorkspaceHint(root, "explicit-root")], title="Continue after restart"
        )
        working = request_task_checkpoint(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            started.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="First checkpoint",
            next_step="Continue from the persisted checkpoint",
        )
        status = request_workspace_task_status(socket_path, [WorkspaceHint(root, "explicit-root")])
        assert status.workspace_id == workspace_id
        assert status.task is not None
        assert status.task.task_id == started.task_id
        assert status.task.title == "Continue after restart"
        assert status.task.state is TaskState.WORKING
        assert status.task.revision == working.revision == 2
        assert status.last_checkpoint is not None
        assert status.last_checkpoint.checkpoint_id == working.checkpoint_id
        assert status.last_checkpoint.task_revision == 2
        assert status.last_checkpoint.next_step == "Continue from the persisted checkpoint"
        raw_status = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "task-continuity-status",
                "method": "workspace_task_status",
                "params": {
                    "hints": [
                        {
                            "path": str(root.resolve()),
                            "source": "explicit-root",
                            "match_mode": "root",
                        }
                    ]
                },
            },
        )
        raw_result = raw_status["result"]
        assert isinstance(raw_result, dict)
        assert set(raw_result) == {"schema_version", "workspace_id", "task", "last_checkpoint"}
        assert "First checkpoint" not in json.dumps(raw_status, sort_keys=True)
        for forbidden in ("summary", "changed_paths", "baseline_head", "knowledge_ids"):
            assert forbidden not in json.dumps(raw_status, sort_keys=True)

        waiting = request_task_checkpoint(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            started.task_id,
            expected_revision=2,
            state=TaskState.WAITING,
            summary="Waiting for operator",
            next_step="Resume this exact Task after feedback",
            wait_reason=TaskWaitReason.OPERATOR_INPUT,
        )
        waiting_status = request_workspace_task_status(
            socket_path, [WorkspaceHint(root, "explicit-root")]
        )
        assert waiting_status.task is not None
        assert waiting_status.task.task_id == started.task_id
        assert waiting_status.task.state is TaskState.WAITING
        assert waiting_status.task.revision == waiting.revision == 3
        assert waiting_status.last_checkpoint is not None
        assert waiting_status.last_checkpoint.checkpoint_id == waiting.checkpoint_id
        assert waiting_status.last_checkpoint.next_step == "Resume this exact Task after feedback"
    finally:
        _stop_server(stop_event, executor, future)


def test_stale_task_checkpoint_is_non_mutating_including_knowledge(tmp_path: Path) -> None:
    root, database, _workspace_id = _registered_database(tmp_path)
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        started = request_task_start(
            socket_path, [WorkspaceHint(root, "explicit-root")], title="Concurrent checkpoint"
        )
        current = request_task_checkpoint(
            socket_path,
            [WorkspaceHint(root, "explicit-root")],
            started.task_id,
            expected_revision=1,
            state=TaskState.WORKING,
            summary="First writer",
        )
        assert current.revision == 2

        with pytest.raises(IpcRemoteError) as exc_info:
            request_task_checkpoint(
                socket_path,
                [WorkspaceHint(root, "explicit-root")],
                started.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Stale writer",
                knowledge=(
                    KnowledgeDraft(
                        kind=KnowledgeKind.CAVEAT,
                        title="Must not persist",
                        body="stale semantic content",
                    ),
                ),
            )
        assert exc_info.value.code == "task_revision_conflict"
    finally:
        _stop_server(stop_event, executor, future)

    connection = connect_database(database)
    try:
        assert connection.execute(
            "SELECT revision FROM tasks WHERE id = ?", (started.task_id,)
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM task_checkpoints WHERE task_id = ?", (started.task_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (started.task_id,)
        ).fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM knowledge_cards").fetchone() == (0,)
    finally:
        connection.close()


def test_task_checkpoint_rejects_task_from_other_workspace_without_mutation(tmp_path: Path) -> None:
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    _make_repo(root_a, content="a\n")
    _make_repo(root_b, content="b\n")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project_a = create_project(connection)
        project_b = create_project(connection)
        workspace_a = register_workspace(connection, project_id=project_a.project_id, path=root_a)
        workspace_b = register_workspace(connection, project_id=project_b.project_id, path=root_b)
        scan_workspace(connection, workspace_a.workspace_id)
        scan_workspace(connection, workspace_b.workspace_id)
    finally:
        connection.close()

    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        started = request_task_start(
            socket_path, [WorkspaceHint(root_a, "explicit-root")], title="Owned by A"
        )
        with pytest.raises(IpcRemoteError) as exc_info:
            request_task_checkpoint(
                socket_path,
                [WorkspaceHint(root_b, "explicit-root")],
                started.task_id,
                expected_revision=1,
                state=TaskState.WORKING,
                summary="Wrong workspace",
            )
        assert exc_info.value.code == "task_workspace_conflict"
    finally:
        _stop_server(stop_event, executor, future)

    connection = connect_database(database)
    try:
        assert connection.execute(
            "SELECT revision FROM tasks WHERE id = ?", (started.task_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM task_checkpoints WHERE task_id = ?", (started.task_id,)
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "method,params",
    [
        (
            "task_start",
            {
                "hints": [{"path": "/repo", "source": "test", "match_mode": "root"}],
                "title": "Valid",
                "extra": True,
            },
        ),
        (
            "task_checkpoint",
            {
                "hints": [{"path": "/repo", "source": "test", "match_mode": "root"}],
                "task_id": "task",
                "expected_revision": 0,
                "state": "working",
                "summary": "summary",
                "next_step": None,
                "wait_reason": None,
                "knowledge": [],
            },
        ),
    ],
)
def test_task_ipc_rejects_malformed_params_and_daemon_recovers(
    tmp_path: Path,
    method: str,
    params: dict[str, object],
) -> None:
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    stop_event, executor, future = _start_server(database, socket_path)
    try:
        response = _raw_request(
            socket_path,
            {
                "version": PROTOCOL_VERSION,
                "request_id": "bad-task",
                "method": method,
                "params": params,
            },
        )
        assert response == {
            "version": PROTOCOL_VERSION,
            "request_id": None,
            "ok": False,
            "error": {"code": "invalid_request", "message": "IPC request is invalid"},
        }
        assert request_status(socket_path).schema_version == SCHEMA_VERSION
    finally:
        _stop_server(stop_event, executor, future)
