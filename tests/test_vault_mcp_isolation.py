from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp_project_intelligence import _init_repo, _start_daemon
from test_vault import PASSWORD, SECRET, record

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.vault.store import VaultStore


@pytest.mark.anyio
@pytest.mark.parametrize("no_password", [False, True])
async def test_real_mcp_cannot_expand_unlocked_vault(tmp_path: Path, no_password: bool) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()
    folder = tmp_path / "backup"
    folder.mkdir()
    vault = VaultStore(tmp_path / "harness-vault")
    vault.create(PASSWORD, str(folder), no_password=no_password)
    vault.save_record(
        "", record(project_id=project.project_id, title="private-title-canary"), vault.revision
    )
    runtime = tmp_path / "runtime"
    stop, executor, future = _start_daemon(database, runtime / "harness" / "harness.sock")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime), "HARNESS_WORKSPACE_ROOT": str(root)},
        cwd=str(root),
    )
    try:
        async with Client(stdio_client(params)) as client:
            tools = await client.list_tools()
            assert all("vault" not in tool.name for tool in tools.tools)
            assert all(tool.name != "project_search" for tool in tools.tools)
            calls: list[tuple[str, dict[str, object]]] = [
                ("project_status", {}),
                ("project_context", {"refs": ["vault:projects.kdbx"]}),
            ]
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                assert SECRET not in str(result)
                assert "private-title-canary" not in str(result)
            connection = connect_database(database)
            try:
                dump = "\n".join(connection.iterdump())
                assert SECRET not in dump
                assert "private-title-canary" not in dump
            finally:
                connection.close()
    finally:
        stop.set()
        future.result(timeout=10)
        executor.shutdown()
