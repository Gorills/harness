from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness.daemon import serve_daemon
from harness.index import scan_workspace
from harness.ipc import request_project_search
from harness.registry import create_project, register_workspace
from harness.retrieval import PROJECT_SEARCH_MAX_BYTES, ProjectSearchScope
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX MCP/IPC slice")

_LANGUAGE_SOURCES = (
    ("python", "src/service.py", "def target_call():\n    return 1\ntarget_call()\n"),
    ("javascript", "src/service.js", "function target_call() { return 1 }\ntarget_call()\n"),
    (
        "typescript",
        "src/service.ts",
        "function target_call(): number { return 1 }\ntarget_call()\n",
    ),
    (
        "tsx",
        "src/Button.tsx",
        "function target_call() { return <button onClick={() => target_call()} /> }\n",
    ),
    (
        "go",
        "src/service.go",
        "package service\nfunc target_call() {}\nfunc run() { target_call() }\n",
    ),
    ("rust", "src/service.rs", "fn target_call() {}\nfn run() { target_call(); }\n"),
    (
        "java",
        "src/Service.java",
        "class Service { static void target_call() {} void run() { target_call(); } }\n",
    ),
)


@contextmanager
def _running_repository(tmp_path: Path, files: dict[str, str]) -> Iterator[tuple[Path, Path]]:
    root = tmp_path / "repo"
    root.mkdir()
    for relative_path, source in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    for arguments in (
        ["init", "-b", "main"],
        ["add", "."],
        [
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "initial",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()

    socket_path = tmp_path / "r" / "harness" / "harness.sock"
    stop = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(serve_daemon, database, socket_path, stop_event=stop)
        try:
            deadline = time.monotonic() + 3
            while not socket_path.exists():
                if future.done():
                    future.result()
                if time.monotonic() >= deadline:
                    raise AssertionError("daemon did not start")
                time.sleep(0.01)
            yield root, socket_path
        finally:
            stop.set()
            future.result(timeout=5)


@pytest.mark.parametrize(
    ("language", "relative_path", "source"),
    _LANGUAGE_SOURCES,
    ids=[language for language, _path, _source in _LANGUAGE_SOURCES],
)
def test_symbol_navigation_supported_language_survives_real_ipc(
    tmp_path: Path, language: str, relative_path: str, source: str
) -> None:
    with _running_repository(tmp_path, {relative_path: source}) as (root, socket_path):
        result = request_project_search(
            socket_path,
            (WorkspaceHint(root, "test"),),
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        assert result.workspace_state == "current"
        assert result.exact_coverage is not None
        assert result.exact_coverage.complete is True
        navigation = result.symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == (language,)
        assert navigation.precise_classification_complete is True
        assert navigation.definition_count == 1
        assert navigation.call_count == 1
        assert {relation.path for relation in navigation.relations} == {relative_path}
        assert all(relation.evidence is not None for relation in navigation.relations)


def test_symbol_navigation_all_seven_languages_survive_real_ipc(tmp_path: Path) -> None:
    files = {path: source for _language, path, source in _LANGUAGE_SOURCES}
    with _running_repository(tmp_path, files) as (root, socket_path):
        result = request_project_search(
            socket_path,
            (WorkspaceHint(root, "test"),),
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        navigation = result.symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == (
            "go",
            "java",
            "javascript",
            "python",
            "rust",
            "tsx",
            "typescript",
        )
        assert navigation.parsed_precise_files == 7
        assert navigation.definition_count == 7
        assert navigation.call_count == 7
        assert navigation.precise_classification_complete is True


def test_unsupported_source_retains_exact_coverage_through_real_ipc(tmp_path: Path) -> None:
    with _running_repository(
        tmp_path, {"src/service.vue": "<script>function target_call() {}</script>\n"}
    ) as (root, socket_path):
        result = request_project_search(
            socket_path,
            (WorkspaceHint(root, "test"),),
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        assert result.exact_coverage is not None
        assert result.exact_coverage.complete is True
        assert result.exact_coverage.matched_occurrences == 1
        navigation = result.symbol_navigation
        assert navigation is not None
        assert navigation.precise_languages == ()
        assert navigation.matching_unsupported_files == 1
        assert navigation.precise_classification_complete is False
        assert navigation.relations == ()


@pytest.mark.anyio
async def test_python_import_resolution_survives_ipc_and_real_stdio_mcp(tmp_path: Path) -> None:
    with _running_repository(
        tmp_path,
        {
            "src/service.py": "def target_call():\n    return 1\n",
            "src/use.py": "from service import target_call as tc\ndef invoke():\n    return tc()\n",
        },
    ) as (root, socket_path):
        result = request_project_search(
            socket_path,
            (WorkspaceHint(root, "test"),),
            "target_call",
            scope=ProjectSearchScope.CODE,
        )
        navigation = result.symbol_navigation
        assert navigation is not None
        call = next(relation for relation in navigation.relations if relation.kind == "call")
        assert call.target == "tc"
        assert call.resolved_target == "service.target_call"
        assert call.resolution_kind == "python_from_import_binding"
        assert call.resolved_definition_path == "src/service.py"
        assert call.resolved_definition_line == 1
        assert call.resolved_definition_column == 5
        assert call.resolved_definition_kind == "function"
        assert call.resolution_validation_kind == "python_workspace_direct_export"
        assert "resolution_module" not in call.to_wire()

        env = dict(os.environ)
        env.update(
            {
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_RUNTIME_DIR": str(socket_path.parent.parent),
                "HARNESS_HOST_PROFILE": "cursor",
                "HARNESS_WORKSPACE_ROOT": str(root),
            }
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "harness.mcp_process"],
            env=env,
            cwd=str(tmp_path),
        )
        async with Client(stdio_client(parameters)) as client:
            searched = await client.call_tool(
                "project_search", {"query": "target_call", "scope": "code"}
            )
            assert searched.is_error is False
            assert searched.content == []
            payload = searched.structured_content
            assert payload is not None
            assert payload["symbol_navigation"] == navigation.to_wire()
            assert "resolution_module" not in json.dumps(payload)
            assert len(searched.model_dump_json(by_alias=True).encode("utf-8")) <= (
                PROJECT_SEARCH_MAX_BYTES
            )
