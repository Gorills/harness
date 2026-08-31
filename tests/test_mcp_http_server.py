from __future__ import annotations

import os
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError

from harness.daemon import serve_daemon
from harness.dashboard import read_dashboard_access_token
from harness.ipc import IpcError, request_shutdown, request_workspace_scan, request_workspace_status
from harness.mcp_http_server import (
    MCP_HTTP_AUTHORIZATION_HEADER,
    MCP_HTTP_WORKSPACE_ROOT_HEADER,
)
from harness.runtime_paths import MCP_HTTP_ISOLATED_PORT, RuntimePaths, default_runtime_paths
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon HTTP MCP slice")


def _repository(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path.resolve()


def _start_daemon(database: Path, socket_path: Path) -> tuple[ThreadPoolExecutor, Future[None]]:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path)
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if future.done():
            future.result()
        if socket_path.exists():
            return executor, future
        sleep(0.01)
    raise AssertionError("daemon did not start")


@pytest.mark.anyio
async def test_daemon_http_mcp_requires_capability_and_explicit_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(tmp_path / "dev-root"))
    paths = default_runtime_paths()
    root = _repository(tmp_path / "repo")
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        scan = request_workspace_scan(paths.socket, root)
        token = read_dashboard_access_token(paths.database)
        assert token is not None
        url = f"http://127.0.0.1:{MCP_HTTP_ISOLATED_PORT}/mcp"

        async with httpx2.AsyncClient() as raw:
            unauthorized = await raw.post(url, content=b"{}")
        assert unauthorized.status_code == 401

        headers = {
            MCP_HTTP_AUTHORIZATION_HEADER: f"Bearer {token}",
            MCP_HTTP_WORKSPACE_ROOT_HEADER: str(root),
        }
        async with create_mcp_http_client(headers=headers) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport, mode="legacy") as client:
                listed = await client.list_tools()
                assert [tool.name for tool in listed.tools] == [
                    "project_status",
                    "project_search",
                    "project_context",
                    "task_start",
                    "task_checkpoint",
                ]
                result = await client.call_tool("project_status", {})
                assert not result.is_error
                assert result.structured_content is not None
                assert result.structured_content["workspace_id"] == scan.workspace_id

        missing_root_headers = {
            MCP_HTTP_AUTHORIZATION_HEADER: f"Bearer {token}",
        }
        async with create_mcp_http_client(headers=missing_root_headers) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            with pytest.RaisesGroup(pytest.RaisesGroup(MCPError)):
                async with Client(transport, mode="legacy"):
                    pass
    finally:
        request_shutdown(paths.socket)
        await anyio.to_thread.run_sync(future.result, 10)
        executor.shutdown()

    with pytest.raises(IpcError):
        request_workspace_status(
            paths.socket,
            (
                WorkspaceHint(
                    path=root,
                    source="test",
                    match_mode=WorkspaceHintMatchMode.ROOT,
                ),
            ),
        )


def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimePaths:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(tmp_path / "dev-root"))
    return default_runtime_paths()


@pytest.mark.anyio
async def test_daemon_http_mcp_rejects_invalid_capability_and_unknown_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    root = _repository(tmp_path / "repo")
    unknown = _repository(tmp_path / "unknown")
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        request_workspace_scan(paths.socket, root)
        token = read_dashboard_access_token(paths.database)
        assert token is not None
        url = f"http://127.0.0.1:{MCP_HTTP_ISOLATED_PORT}/mcp"

        async with httpx2.AsyncClient() as raw:
            wrong = await raw.post(
                url,
                content=b"{}",
                headers={MCP_HTTP_AUTHORIZATION_HEADER: "Bearer not-the-capability"},
            )
        assert wrong.status_code == 401

        unknown_headers = {
            MCP_HTTP_AUTHORIZATION_HEADER: f"Bearer {token}",
            MCP_HTTP_WORKSPACE_ROOT_HEADER: str(unknown),
        }
        async with create_mcp_http_client(headers=unknown_headers) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            with pytest.RaisesGroup(pytest.RaisesGroup(MCPError)):
                async with Client(transport, mode="legacy"):
                    pass
    finally:
        request_shutdown(paths.socket)
        await anyio.to_thread.run_sync(future.result, 10)
        executor.shutdown()


@pytest.mark.anyio
async def test_daemon_http_mcp_is_unreachable_after_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    root = _repository(tmp_path / "repo")
    executor, future = _start_daemon(paths.database, paths.socket)
    url = f"http://127.0.0.1:{MCP_HTTP_ISOLATED_PORT}/mcp"
    try:
        request_workspace_scan(paths.socket, root)
        async with httpx2.AsyncClient() as raw:
            probe = await raw.post(url, content=b"{}")
        assert probe.status_code == 401
    finally:
        request_shutdown(paths.socket)
        await anyio.to_thread.run_sync(future.result, 10)
        executor.shutdown()

    with pytest.raises(httpx2.ConnectError):
        async with httpx2.AsyncClient() as raw:
            await raw.post(url, content=b"{}")
