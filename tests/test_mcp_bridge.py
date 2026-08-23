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
                "schema_version",
            }
            started = await client.call_tool("task_start", {"title": "MCP continuity"})
            assert started.is_error is False
            assert started.structured_content is not None
            task_id = started.structured_content["task_id"]
            assert started.structured_content["revision"] == 1
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
                serialized = json.dumps(tools, sort_keys=True)
                for forbidden in ("content_sha256", "baseline_head", "source_checkpoint_id"):
                    assert forbidden not in serialized
    finally:
        process.terminate()
        process.wait(timeout=3)
