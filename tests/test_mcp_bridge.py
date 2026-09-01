from __future__ import annotations

import http.client
import json
import os
import select
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast
from urllib.parse import urlencode, urlsplit

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from harness.daemon import serve_daemon
from harness.index import scan_workspace
from harness.ipc import request_dashboard_url
from harness.mcp_bridge import (
    _PROJECT_CONTEXT_DESCRIPTION,
    _PROJECT_SEARCH_DESCRIPTION,
    _SERVER_INSTRUCTIONS,
    _TASK_START_DESCRIPTION,
    _TOOL_ARGUMENTS,
    _unknown_tool_argument_error,
)
from harness.registry import VisibilityMode, create_project, list_workspaces, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import (
    MAX_CHECKPOINT_NEXT_STEP_BYTES,
    MAX_OPERATOR_FEEDBACK_BYTES,
    TaskEventType,
    list_task_events,
)
from harness.tasks import TaskState, get_task, get_task_stack_hints
from harness.visibility import set_project_visibility

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX MCP/IPC slice")


def test_server_instructions_require_task_before_diagnosis_within_budget() -> None:
    encoded = _SERVER_INSTRUCTIONS.encode("utf-8")
    first_512 = _SERVER_INSTRUCTIONS[:512]
    assert len(encoded) < 1024
    assert "must be the first repository action" in first_512
    assert "Before any shell command" in first_512
    assert "Russian" in first_512
    assert "title" in first_512
    assert "next_step" in first_512
    assert "stack_hints" in first_512
    assert "task-focused" in first_512
    assert "before diagnosis" in _SERVER_INSTRUCTIONS
    assert "never skip" in _SERVER_INSTRUCTIONS
    assert "Checkpoint each stage" in _SERVER_INSTRUCTIONS
    assert "implement-after-diagnosis" in _SERVER_INSTRUCTIONS
    assert "code/doc path may be read natively" in _SERVER_INSTRUCTIONS
    assert "project_context is not required for those kinds" in _SERVER_INSTRUCTIONS
    assert "Start/resume a Task before changes." not in _SERVER_INSTRUCTIONS


def test_mcp_bootstrap_requires_task_before_search_diagnosis() -> None:
    text = _SERVER_INSTRUCTIONS
    after_status = text.split("After status", maxsplit=1)[1]
    task_at = after_status.find("start/resume a Task")
    search_at = after_status.find("project_search")
    assert 0 <= task_at < search_at
    assert "After status use project_search" not in text
    assert "After status, use project_search" not in text
    assert "project_search, project_context, then native tools" not in text
    assert "code/doc path may be read natively" in text
    assert "project_context is not required for those kinds" in text
    assert "project_context only for selected semantic refs." not in text


def test_project_search_description_allows_targeted_native_read_after_localization() -> None:
    description = _PROJECT_SEARCH_DESCRIPTION
    assert "exact path" in description
    assert "targeted native read is allowed" in description
    assert "project_context is not required for those kinds" in description
    assert "after task_start or resume" in description
    assert "use project_context only for selected refs" not in description


def test_project_context_description_is_not_mandatory_for_code_docs() -> None:
    description = _PROJECT_CONTEXT_DESCRIPTION
    assert "Not mandatory for code or doc" in description
    assert "already include a path" in description
    assert "metadata only" in description
    assert "Knowledge and Task refs" in description


def test_task_start_description_states_next_discovery_boundary() -> None:
    assert "next host discovery boundary" in _TASK_START_DESCRIPTION
    assert "not current-session MCP skill delivery" in _TASK_START_DESCRIPTION
    assert "live skill injection" in _TASK_START_DESCRIPTION
    assert "stack_hints drive" in _TASK_START_DESCRIPTION


def test_server_instruction_budget_remains_bounded() -> None:
    encoded = _SERVER_INSTRUCTIONS.encode("utf-8")
    first_512 = _SERVER_INSTRUCTIONS[:512]
    assert len(encoded) < 1024
    assert "Russian" in first_512
    assert "title" in first_512
    assert "next_step" in first_512
    assert "stack_hints" in first_512
    assert "task-focused" in first_512
    assert "must be the first repository action" in first_512
    assert "only allowed pre-status action" in first_512


def test_unknown_tool_argument_error_names_allowed_fields_not_unknown_names() -> None:
    start_error = _unknown_tool_argument_error(
        "task_start",
        _TOOL_ARGUMENTS["task_start"],
        {"summary", "taskID"},
    )
    start_text = start_error.message
    assert "Unknown tool argument fields" in start_text
    assert "title" in start_text
    assert "stack_hints" in start_text
    assert "task_id" in start_text
    assert "expected_revision" in start_text
    assert "retry" in start_text
    assert "do not skip" in start_text
    assert "summary" not in start_text
    assert "taskID" not in start_text
    assert start_error.data == {"tool": "task_start", "unknown_field_count": 2}

    status_error = _unknown_tool_argument_error(
        "project_status",
        _TOOL_ARGUMENTS["project_status"],
        {"unexpected"},
    )
    status_text = status_error.message
    assert "no arguments" in status_text
    assert "unexpected" not in status_text
    assert status_error.data == {"tool": "project_status", "unknown_field_count": 1}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "token_service.py").write_text("TOKEN = 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "design.md").write_text("design\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=t@example.invalid", "commit", "-m", "init")
    db = tmp_path / "harness.db"
    initialize_database(db)
    connection = connect_database(db)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()
    return root, db


def _start_daemon(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
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


def _dashboard_post(url: str, fields: dict[str, str | int]) -> int:
    parsed = urlsplit(url)
    assert parsed.hostname is not None and parsed.port is not None
    origin = f"http://{parsed.hostname}:{parsed.port}"
    body = urlencode(fields).encode("ascii")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.request(
        "POST",
        parsed.path,
        body=body,
        headers={
            "Origin": origin,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


@pytest.mark.anyio
async def test_real_stdio_mcp_exposes_stable_five_tool_surface(tmp_path: Path) -> None:
    root, database = _repo(tmp_path)
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_HOST_PROFILE": "cursor",
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    # The explicitly started daemon uses the same canonical socket, while its DB path is selected
    # directly for the test. No bridge process touches SQLite.
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=env,
            cwd=str(tmp_path),
        )
        async with Client(stdio_client(params)) as client:
            assert client.instructions is not None
            first_512 = client.instructions[:512]
            assert "must be the first repository action" in first_512
            assert "Before any shell command" in first_512
            assert "project_status" in first_512
            assert "deferred" in first_512
            assert "initial visible tool list" in first_512
            listed = await client.list_tools()
            assert listed.tools[0].description is not None
            assert "Required first repository action" in listed.tools[0].description
            assert [tool.name for tool in listed.tools] == [
                "project_status",
                "project_search",
                "project_context",
                "task_start",
                "task_checkpoint",
            ]
            searched = await client.call_tool(
                "project_search", {"query": "token service", "scope": "code"}
            )
            assert searched.is_error is False
            assert searched.structured_content is not None
            results = searched.structured_content["results"]
            assert len(results) <= 5
            assert results[0]["ref"] == "code:src/token_service.py"
            assert "TOKEN = 1" not in str(searched.structured_content)
            with pytest.raises(MCPError, match="Unknown tool argument fields") as status_error:
                await client.call_tool("project_status", {"unexpected": True})
            assert "no arguments" in str(status_error.value)
            assert "unexpected" not in str(status_error.value)
            with pytest.raises(MCPError, match="Unknown tool argument fields") as start_error:
                await client.call_tool(
                    "task_start", {"taskID": "typo-must-not-be-ignored", "title": "Unsafe"}
                )
            start_text = str(start_error.value)
            assert "title" in start_text
            assert "retry" in start_text
            assert "do not skip" in start_text
            assert "taskID" not in start_text

            status = await client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            assert set(status.structured_content) == {
                "project_id",
                "workspace_id",
                "visibility_mode",
                "workspace_root",
                "git",
                "index",
                "current_task",
                "relevant_waiting_task",
                "last_checkpoint",
                "next_step",
                "pending_operator_feedback",
                "schema_version",
            }
            assert status.structured_content["pending_operator_feedback"] is None
            assert status.structured_content["visibility_mode"] == "normal"
            assert "scm_write" not in json.dumps(status.structured_content)
            started = await client.call_tool(
                "task_start",
                {"title": "MCP continuity", "stack_hints": [" FastAPI ", "POSTGRES"]},
            )
            assert started.is_error is False
            assert started.structured_content is not None
            assert set(started.structured_content) == {
                "workspace_id",
                "task_id",
                "state",
                "wait_reason",
                "revision",
            }
            task_id = started.structured_content["task_id"]
            assert started.structured_content["revision"] == 1
            for invalid_revision in (True, "1"):
                invalid_checkpoint = await client.call_tool(
                    "task_checkpoint",
                    {
                        "task_id": task_id,
                        "expected_revision": invalid_revision,
                        "state": "working",
                        "summary": "Schema-invalid revision must not mutate",
                    },
                )
                assert invalid_checkpoint.is_error is True
            invalid_limit = await client.call_tool(
                "project_search", {"query": "token", "limit": "1"}
            )
            assert invalid_limit.is_error is True
            checkpoint = await client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 1,
                    "state": "working",
                    "summary": "MCP checkpoint",
                },
            )
            assert checkpoint.is_error is False
            assert checkpoint.structured_content is not None
            assert checkpoint.structured_content["task_id"] == task_id
            assert checkpoint.structured_content["revision"] == 2
            invalid_knowledge = await client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 2,
                    "state": "working",
                    "summary": "Invalid Knowledge must not commit",
                    "knowledge": [
                        {
                            "kind": "behavior",
                            "title": "Token behavior",
                            "body": "Observed during the task",
                            "anchors": [{"path": "src/token_service.py"}],
                            "unexpected": "must be rejected",
                        }
                    ],
                },
            )
            assert invalid_knowledge.is_error is True
            valid_knowledge = await client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 2,
                    "state": "working",
                    "summary": "Persist strict Knowledge",
                    "knowledge": [
                        {
                            "kind": "behavior",
                            "title": "Token behavior",
                            "body": "Observed during the task",
                            "anchors": [{"path": "src/token_service.py"}],
                        }
                    ],
                },
            )
            assert valid_knowledge.is_error is False
            assert valid_knowledge.structured_content is not None
            assert valid_knowledge.structured_content["revision"] == 3
            assert len(valid_knowledge.structured_content["knowledge_ids"]) == 1
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()

    connection = connect_database(database)
    try:
        assert get_task_stack_hints(connection, task_id) == ("fastapi", "postgres")
    finally:
        connection.close()


@pytest.mark.anyio
async def test_mcp_hidden_status_does_not_disclose_enforcement(tmp_path: Path) -> None:
    root, database = _repo(tmp_path)
    connection = connect_database(database)
    try:
        workspace = list_workspaces(connection)[0]
        set_project_visibility(
            connection,
            mode=VisibilityMode.HIDDEN,
            host_profiles=("cursor",),
            project_id=workspace.project_id,
        )
    finally:
        connection.close()
    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=env,
            cwd=str(root),
        )
        async with Client(stdio_client(params)) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "project_status",
                "project_search",
                "project_context",
                "task_start",
                "task_checkpoint",
            ]
            status = await client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            assert status.structured_content["visibility_mode"] == "hidden"
            dumped = json.dumps(status.structured_content)
            assert "scm_write" not in dumped
            assert "unsupported" not in dumped
            assert "info/exclude" not in dumped
            assert "harness-hidden" not in dumped
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


@pytest.mark.anyio
async def test_human_review_feedback_survives_new_mcp_session_and_accept_completes_task(
    tmp_path: Path,
) -> None:
    root, database = _repo(tmp_path)
    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env=env,
        cwd=str(root),
    )
    maximum_next_step = "N" * MAX_CHECKPOINT_NEXT_STEP_BYTES
    maximum_feedback = "F" * MAX_OPERATOR_FEEDBACK_BYTES
    try:
        async with Client(stdio_client(params)) as first_client:
            started = await first_client.call_tool("task_start", {"title": "Human review loop"})
            assert started.is_error is False
            assert started.structured_content is not None
            task_id = started.structured_content["task_id"]
            workspace_id = started.structured_content["workspace_id"]
            waiting = await first_client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 1,
                    "state": "waiting",
                    "wait_reason": "operator_review",
                    "summary": "Implementation ready for review",
                    "next_step": maximum_next_step,
                },
            )
            assert waiting.is_error is False
            assert waiting.structured_content is not None
            assert waiting.structured_content["revision"] == 2

        dashboard = request_dashboard_url(socket_path)
        assert (
            _dashboard_post(
                dashboard.url,
                {
                    "action": "feedback",
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "expected_revision": 2,
                    "feedback": maximum_feedback,
                },
            )
            == 303
        )

        # A fresh model-facing bridge process sees the same durable Task and the feedback that
        # caused its waiting -> working transition. No dashboard-only side channel is required.
        async with Client(stdio_client(params)) as second_client:
            status = await second_client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            current = status.structured_content["current_task"]
            assert current is not None
            assert current["task_id"] == task_id
            assert current["state"] == "working"
            assert current["revision"] == 3
            assert status.structured_content["pending_operator_feedback"] == maximum_feedback

            resumed = await second_client.call_tool(
                "task_start", {"task_id": task_id, "expected_revision": 3}
            )
            assert resumed.is_error is False
            assert resumed.structured_content is not None
            assert resumed.structured_content["revision"] == 3

            applied = await second_client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 3,
                    "state": "working",
                    "summary": "Applied mobile spacing feedback",
                },
            )
            assert applied.is_error is False
            assert applied.structured_content is not None
            assert applied.structured_content["revision"] == 4
            after_checkpoint = await second_client.call_tool("project_status")
            assert after_checkpoint.is_error is False
            assert after_checkpoint.structured_content is not None
            assert after_checkpoint.structured_content["pending_operator_feedback"] is None

            review_again = await second_client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 4,
                    "state": "waiting",
                    "wait_reason": "operator_review",
                    "summary": "Feedback applied; ready again",
                    "next_step": "Final operator review",
                },
            )
            assert review_again.is_error is False
            assert review_again.structured_content is not None
            assert review_again.structured_content["revision"] == 5

        assert (
            _dashboard_post(
                dashboard.url,
                {
                    "action": "accept",
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "expected_revision": 5,
                },
            )
            == 303
        )

        connection = connect_database(database)
        try:
            completed = get_task(connection, task_id)
            assert completed.state is TaskState.COMPLETED
            assert completed.revision == 6
            assert tuple(event.event_type for event in list_task_events(connection, task_id)) == (
                TaskEventType.CREATED,
                TaskEventType.CHECKPOINT,
                TaskEventType.OPERATOR_FEEDBACK,
                TaskEventType.CHECKPOINT,
                TaskEventType.CHECKPOINT,
                TaskEventType.ACCEPTED,
            )
        finally:
            connection.close()
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


@pytest.mark.anyio
async def test_project_search_scope_filters_before_backend_limit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "aa").mkdir()
    (root / "zz").mkdir()
    for index in range(12):
        (root / "aa" / f"token_{index:02}.py").write_text("x = 1\n", encoding="utf-8")
    (root / "zz" / "token_notes.md").write_text("token documentation\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "init",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()

    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env=env,
        cwd=str(root),
    )
    try:
        async with Client(stdio_client(params)) as client:
            docs = await client.call_tool(
                "project_search", {"query": "token", "scope": "docs", "limit": 1}
            )
            code = await client.call_tool(
                "project_search", {"query": "token", "scope": "code", "limit": 3}
            )
            assert docs.is_error is False
            assert docs.structured_content is not None
            assert [item["path"] for item in docs.structured_content["results"]] == [
                "zz/token_notes.md"
            ]
            assert code.is_error is False
            assert code.structured_content is not None
            assert all(
                not item["path"].endswith(".md") for item in code.structured_content["results"]
            )
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_raw_modern_wire_catalog_is_bounded_and_stable() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "harness.mcp_process"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "raw-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    try:
        for request_id, method in ((1, "server/discover"), (2, "tools/list")):
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {"_meta": meta},
            }
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 3)
            assert ready, f"no MCP response for {method}"
            raw = process.stdout.readline()
            assert len(raw.encode("utf-8")) < 12 * 1024
            response = json.loads(raw)
            assert response["jsonrpc"] == "2.0"
            assert response["id"] == request_id
            if method == "server/discover":
                assert response["result"]["supportedVersions"] == ["2026-07-28"]
                instructions = response["result"]["instructions"]
                assert len(instructions.encode("utf-8")) < 1024
                assert "Russian" in instructions[:512]
                assert "title" in instructions[:512]
                assert "next_step" in instructions[:512]
                assert "stack_hints" in instructions[:512]
                assert "task-focused" in instructions[:512]
                assert "durable SCM mutations" in instructions
                assert "code/doc path may be read natively" in instructions
                assert "project_context is not required for those kinds" in instructions
                assert "project_context only for selected semantic refs." not in instructions
                assert "before diagnosis" in instructions
                assert "never skip" in instructions
                assert "After status, start/resume a Task" in instructions
                assert "After status use project_search" not in instructions
                assert "Start/resume a Task before changes." not in instructions
                assert "scm_write" not in instructions
                assert "info/exclude" not in instructions
            else:
                tools = response["result"]["tools"]
                assert [tool["name"] for tool in tools] == [
                    "project_status",
                    "project_search",
                    "project_context",
                    "task_start",
                    "task_checkpoint",
                ]
                for tool in tools:
                    assert tool["inputSchema"]["additionalProperties"] is False
                by_name = {tool["name"]: tool for tool in tools}
                assert "Russian" in by_name["task_start"]["description"]
                assert "affected technologies" in by_name["task_start"]["description"]
                assert "before diagnosis" in by_name["task_start"]["description"]
                assert "project_search" in by_name["task_start"]["description"]
                assert "omit task_id" in by_name["task_start"]["description"]
                assert "never pass summary" in by_name["task_start"]["description"]
                assert "read this schema and retry" in by_name["task_start"]["description"]
                assert "next host discovery boundary" in by_name["task_start"]["description"]
                assert (
                    "not current-session MCP skill delivery" in by_name["task_start"]["description"]
                )
                assert "live skill injection" in by_name["task_start"]["description"]
                assert "targeted native read is allowed" in by_name["project_search"]["description"]
                assert "Not mandatory for code or doc" in by_name["project_context"]["description"]
                assert "Russian" in by_name["task_checkpoint"]["description"]
                assert "each logical stage" in by_name["task_checkpoint"]["description"]
                task_start_schema = by_name["task_start"]["inputSchema"]
                assert "stack_hints" in task_start_schema["properties"]
                checkpoint_schema = by_name["task_checkpoint"]["inputSchema"]
                assert (
                    checkpoint_schema["$defs"]["VerificationInput"]["additionalProperties"] is False
                )
                assert checkpoint_schema["$defs"]["KnowledgeInput"]["additionalProperties"] is False
                assert (
                    checkpoint_schema["$defs"]["KnowledgeAnchorInput"]["additionalProperties"]
                    is False
                )
                serialized = json.dumps(tools, sort_keys=True)
                for forbidden in (
                    "content_sha256",
                    "baseline_head",
                    "source_checkpoint_id",
                    "recommended_skills",
                    "skill_body",
                    "skill_bodies",
                    "skill_refs",
                    "selected_skills",
                ):
                    assert forbidden not in serialized

        oversized_ref = "x" * 20_000
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "_meta": meta,
                "name": "project_context",
                "arguments": {"refs": [oversized_ref]},
            },
        }
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 3)
        assert ready, "no MCP response for oversized project_context ref"
        raw = process.stdout.readline()
        assert len(raw.encode("utf-8")) < 4 * 1024
        response = json.loads(raw)
        assert response["result"]["isError"] is True
        serialized_error = json.dumps(response, sort_keys=True)
        assert oversized_ref[:256] not in serialized_error
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_stdio_transport_flushes_response_and_exits_after_stdin_eof() -> None:
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "eof-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {"_meta": meta},
    }
    completed = subprocess.run(
        [sys.executable, "-m", "harness.mcp_process"],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    assert len(lines[0].encode("utf-8")) < 12 * 1024
    response = json.loads(lines[0])
    assert response["id"] == 1
    assert "result" in response


def test_stdio_transport_bounds_client_controlled_envelope_fields() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "harness.mcp_process"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    stdin = process.stdin
    stdout = process.stdout
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "wire-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    def exchange(request: dict[str, object]) -> tuple[str, dict[str, object]]:
        stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        stdin.flush()
        ready, _, _ = select.select([stdout], [], [], 3)
        assert ready, f"no MCP response for {request['method']}"
        raw = stdout.readline()
        assert len(raw.encode("utf-8")) < 12 * 1024
        return raw, json.loads(raw)

    try:
        _, oversized_opening = exchange(
            {
                "jsonrpc": "2.0",
                "id": "R" * 20_000,
                "method": "server/discover",
                "params": {"_meta": meta},
            }
        )
        assert oversized_opening["id"] is None
        assert "error" in oversized_opening

        _, discovered = exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": meta},
            }
        )
        assert discovered["id"] == 1

        oversized_cases: tuple[tuple[dict[str, object], object], ...] = (
            (
                {
                    "jsonrpc": "2.0",
                    "id": int("9" * 300),
                    "method": "server/discover",
                    "params": {"_meta": meta},
                },
                None,
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "X" * 20_000,
                    "params": {"_meta": meta},
                },
                2,
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "server/discover",
                    "params": {
                        "_meta": {
                            **meta,
                            "io.modelcontextprotocol/protocolVersion": "V" * 20_000,
                        }
                    },
                },
                3,
            ),
            (
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "_meta": meta,
                        "name": "T" * 20_000,
                        "arguments": {},
                    },
                },
                4,
            ),
        )
        for request, expected_id in oversized_cases:
            raw, response = exchange(request)
            assert response["id"] == expected_id
            assert "error" in response
            assert "R" * 512 not in raw
            assert "X" * 512 not in raw
            assert "V" * 512 not in raw
            assert "T" * 512 not in raw

        _, listed = exchange(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/list",
                "params": {"_meta": meta},
            }
        )
        assert listed["id"] == 5
        assert "result" in listed
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_oversized_request_id_is_rejected_before_task_mutation(tmp_path: Path) -> None:
    root, database = _repo(tmp_path)
    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "harness.mcp_process"],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    stdin = process.stdin
    stdout = process.stdout
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "mutation-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    def exchange(request: dict[str, object]) -> dict[str, object]:
        stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        stdin.flush()
        ready, _, _ = select.select([stdout], [], [], 3)
        assert ready, f"no MCP response for {request['method']}"
        raw = stdout.readline()
        assert len(raw.encode("utf-8")) < 12 * 1024
        return cast(dict[str, object], json.loads(raw))

    try:
        discovered = exchange(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": meta},
            }
        )
        assert discovered["id"] == 1

        rejected = exchange(
            {
                "jsonrpc": "2.0",
                "id": "M" * 20_000,
                "method": "tools/call",
                "params": {
                    "_meta": meta,
                    "name": "task_start",
                    "arguments": {"title": "must not mutate"},
                },
            }
        )
        assert rejected["id"] is None
        assert "error" in rejected

        started = exchange(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "_meta": meta,
                    "name": "task_start",
                    "arguments": {"title": "only task"},
                },
            }
        )
        assert started["id"] == 2
        result = started["result"]
        assert isinstance(result, dict)
        assert result["isError"] is False
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        assert structured["revision"] == 1
    finally:
        process.terminate()
        process.wait(timeout=3)
        stop.set()
        executor.shutdown(wait=True)
        future.result()


@pytest.mark.anyio
async def test_project_context_expands_long_ref_returned_by_project_search(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    first = "a" * 140
    second = "b" * 140
    relative_path = f"{first}/{second}/token_service.py"
    source = root / relative_path
    source.parent.mkdir(parents=True)
    source.write_text("TOKEN = 1\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "init",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()

    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env=env,
        cwd=str(root),
    )
    try:
        async with Client(stdio_client(params)) as client:
            searched = await client.call_tool(
                "project_search", {"query": "token", "scope": "code", "limit": 1}
            )
            assert searched.is_error is False
            assert searched.structured_content is not None
            ref = searched.structured_content["results"][0]["ref"]
            assert len(ref.encode("utf-8")) > 256

            context = await client.call_tool("project_context", {"refs": [ref]})
            assert context.is_error is False
            assert context.structured_content == {
                "items": [
                    {
                        "ref": ref,
                        "kind": "code",
                        "title": "token_service.py",
                        "location": relative_path,
                        "path": relative_path,
                        "entry_kind": "file",
                        "size_bytes": source.stat().st_size,
                        "freshness": "indexed_snapshot",
                    }
                ]
            }
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_project_search_success_wire_stays_within_model_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    directory = "a" * 150
    for index in range(10):
        source = root / directory / f"token_{index:02}_service.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("TOKEN = 1\n", encoding="utf-8")
    _git(root, "init")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "init",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()

    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "harness.mcp_process"],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "raw-budget-test", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    try:
        for request in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": meta},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "_meta": meta,
                    "name": "project_search",
                    "arguments": {"query": "token", "limit": 3},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "_meta": meta,
                    "name": "project_search",
                    "arguments": {"query": "token", "limit": 10},
                },
            },
        ):
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
            ready, _, _ = select.select([process.stdout], [], [], 3)
            assert ready, f"no MCP response for {request['method']}"
            raw = process.stdout.readline()
            if request["id"] == 1:
                continue
            assert len(raw.encode("utf-8")) < 12 * 1024
            response = json.loads(raw)
            if request["id"] == 2:
                assert response["result"]["isError"] is False
                assert response["result"]["content"]
                assert len(response["result"]["structuredContent"]["results"]) == 3
            else:
                assert response["result"]["isError"] is True
                assert (
                    "response exceeds model exposure budget"
                    in response["result"]["content"][0]["text"]
                )
    finally:
        process.terminate()
        process.wait(timeout=3)
        stop.set()
        executor.shutdown(wait=True)
        future.result()


@pytest.mark.anyio
async def test_task_continuity_survives_independent_mcp_processes(tmp_path: Path) -> None:
    root, database = _repo(tmp_path)
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env=env,
        cwd=str(root),
    )
    try:
        async with Client(stdio_client(params)) as first_client:
            started = await first_client.call_tool("task_start", {"title": "Durable continuity"})
            assert started.is_error is False
            assert started.structured_content is not None
            task_id = started.structured_content["task_id"]
            checkpoint = await first_client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 1,
                    "state": "working",
                    "summary": "Persisted before bridge restart",
                    "next_step": "Resume from this exact checkpoint",
                    "verification": [
                        {
                            "name": "focused tests",
                            "status": "passed",
                            "evidence": "pytest target: passed",
                        }
                    ],
                },
            )
            assert checkpoint.is_error is False
            assert checkpoint.structured_content is not None
            assert checkpoint.structured_content["revision"] == 2
            assert checkpoint.structured_content["verification_count"] == 1

        async with Client(stdio_client(params)) as second_client:
            status = await second_client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            current = status.structured_content["current_task"]
            assert current == {
                "task_id": task_id,
                "title": "Durable continuity",
                "state": "working",
                "wait_reason": None,
                "revision": 2,
            }
            assert status.structured_content["relevant_waiting_task"] is None
            assert status.structured_content["next_step"] == "Resume from this exact checkpoint"
            assert status.structured_content["last_checkpoint"]["verification"] == [
                {"name": "focused tests", "status": "passed"}
            ]
            resumed = await second_client.call_tool("task_start", {"task_id": task_id})
            assert resumed.is_error is False
            assert resumed.structured_content is not None
            assert resumed.structured_content["revision"] == 2
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


@pytest.mark.anyio
async def test_cross_host_task_and_knowledge_continuity_without_worktree_mixing(
    tmp_path: Path,
) -> None:
    root, database = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked", str(linked))
    linked = linked.resolve()
    (linked / "src" / "linked_only.py").write_text("LINKED = 2\n", encoding="utf-8")

    connection = connect_database(database)
    try:
        first = list_workspaces(connection)[0]
        linked_workspace = register_workspace(
            connection,
            project_id=first.project_id,
            path=linked,
        )
        scan_workspace(connection, linked_workspace.workspace_id)
        root_workspace_id = first.workspace_id
    finally:
        connection.close()

    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)

    def host_env(profile: str, workspace: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = str(runtime)
        env["HARNESS_HOST_PROFILE"] = profile
        if profile == "claude-code":
            env["CLAUDE_PROJECT_DIR"] = str(workspace)
            env.pop("HARNESS_WORKSPACE_ROOT", None)
        else:
            env["HARNESS_WORKSPACE_ROOT"] = str(workspace)
            env.pop("CLAUDE_PROJECT_DIR", None)
        return env

    try:
        cursor_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=host_env("cursor", root),
            cwd=str(tmp_path),
        )
        async with Client(stdio_client(cursor_params)) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "project_status",
                "project_search",
                "project_context",
                "task_start",
                "task_checkpoint",
            ]
            started = await client.call_tool("task_start", {"title": "Cross host continuity"})
            assert started.is_error is False
            assert started.structured_content is not None
            task_id = started.structured_content["task_id"]
            checkpoint = await client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": 1,
                    "state": "working",
                    "summary": "Persist host-neutral knowledge",
                    "knowledge": [
                        {
                            "kind": "behavior",
                            "title": "Cross host invariant",
                            "body": "Task continuity is owned by Harness domain state.",
                            "anchors": [{"path": "src/token_service.py"}],
                        }
                    ],
                },
            )
            assert checkpoint.is_error is False

        codex_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=host_env("codex", root),
            cwd=str(tmp_path),
        )
        async with Client(stdio_client(codex_params)) as client:
            status = await client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            assert status.structured_content["workspace_id"] == root_workspace_id
            assert status.structured_content["current_task"]["task_id"] == task_id
            knowledge = await client.call_tool(
                "project_search",
                {"query": "cross host invariant", "scope": "knowledge", "limit": 5},
            )
            assert knowledge.is_error is False
            assert knowledge.structured_content is not None
            assert any(
                item["ref"].startswith("knowledge:")
                for item in knowledge.structured_content["results"]
            )

        cursor_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=host_env("cursor", root),
            cwd=str(tmp_path),
        )
        async with Client(stdio_client(cursor_params)) as client:
            status = await client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            assert status.structured_content["workspace_id"] == root_workspace_id
            assert status.structured_content["current_task"]["task_id"] == task_id

            knowledge = await client.call_tool(
                "project_search",
                {"query": "cross host invariant", "scope": "knowledge", "limit": 5},
            )
            assert knowledge.is_error is False
            assert knowledge.structured_content is not None
            assert any(
                item["ref"].startswith("knowledge:")
                for item in knowledge.structured_content["results"]
            )
            tasks = await client.call_tool(
                "project_search",
                {"query": "cross host continuity", "scope": "tasks", "limit": 5},
            )
            assert tasks.is_error is False
            assert tasks.structured_content is not None
            assert any(
                item["ref"] == f"task:{task_id}" for item in tasks.structured_content["results"]
            )

        linked_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=host_env("cursor", linked),
            cwd=str(root),
        )
        async with Client(stdio_client(linked_params)) as client:
            status = await client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            assert status.structured_content["workspace_id"] == linked_workspace.workspace_id
            assert status.structured_content["workspace_id"] != root_workspace_id
            assert status.structured_content["current_task"] is None
            linked_search = await client.call_tool(
                "project_search", {"query": "linked only", "scope": "code", "limit": 5}
            )
            assert linked_search.is_error is False
            assert linked_search.structured_content is not None
            assert (
                linked_search.structured_content["results"][0]["ref"] == "code:src/linked_only.py"
            )

        cursor_again = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=host_env("cursor", root),
            cwd=str(linked),
        )
        async with Client(stdio_client(cursor_again)) as client:
            status = await client.call_tool("project_status")
            assert status.is_error is False
            assert status.structured_content is not None
            assert status.structured_content["workspace_id"] == root_workspace_id
            assert status.structured_content["current_task"]["task_id"] == task_id
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()
