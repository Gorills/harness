from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from harness.daemon import serve_daemon
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX MCP/IPC slice")


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
            searched = await client.call_tool(
                "project_search", {"query": "token service", "scope": "code"}
            )
            assert searched.is_error is False
            assert searched.structured_content is not None
            results = searched.structured_content["results"]
            assert len(results) <= 5
            assert results[0]["ref"] == "code:src/token_service.py"
            assert "TOKEN = 1" not in str(searched.structured_content)
            with pytest.raises(MCPError, match="Unknown tool argument fields"):
                await client.call_tool("project_status", {"unexpected": True})
            with pytest.raises(MCPError, match="Unknown tool argument fields"):
                await client.call_tool(
                    "task_start", {"taskID": "typo-must-not-be-ignored", "title": "Unsafe"}
                )

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
                "schema_version",
            }
            started = await client.call_tool("task_start", {"title": "MCP continuity"})
            assert started.is_error is False
            assert started.structured_content is not None
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
                assert len(response["result"]["instructions"].encode("utf-8")) < 1024
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
                checkpoint_schema = next(
                    tool["inputSchema"] for tool in tools if tool["name"] == "task_checkpoint"
                )
                assert checkpoint_schema["$defs"]["KnowledgeInput"]["additionalProperties"] is False
                assert (
                    checkpoint_schema["$defs"]["KnowledgeAnchorInput"]["additionalProperties"]
                    is False
                )
                serialized = json.dumps(tools, sort_keys=True)
                for forbidden in ("content_sha256", "baseline_head", "source_checkpoint_id"):
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
                },
            )
            assert checkpoint.is_error is False
            assert checkpoint.structured_content is not None
            assert checkpoint.structured_content["revision"] == 2

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
            resumed = await second_client.call_tool("task_start", {"task_id": task_id})
            assert resumed.is_error is False
            assert resumed.structured_content is not None
            assert resumed.structured_content["revision"] == 2
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()
