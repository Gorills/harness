from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from test_mcp_http_server import _isolated_runtime, _start_daemon

from harness.dashboard import read_dashboard_access_token
from harness.ipc import IpcRemoteError, request_servers, request_shutdown
from harness.registry import create_project
from harness.runtime_paths import mcp_http_listen_port
from harness.servers import (
    ServerConflictError,
    ServerError,
    archive_server,
    clear_server_history,
    dispatch_servers,
    get_server,
    record_event,
    save_server,
)
from harness.storage import connect_database, initialize_database


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
        for table in (
            "servers",
            "server_events",
            "server_projects",
            "server_imported_sites",
            "server_imported_profiles",
            "sqlite_sequence",
            "sqlite_master",
        )
    }


def _seed(connection: sqlite3.Connection) -> None:
    project = create_project(connection)
    save_server(
        connection,
        "selected",
        0,
        {
            "name": "Selected",
            "sites": [
                {
                    "site_id": "front",
                    "profile_id": "test",
                    "name": "Frontend",
                    "source": "operator",
                    "paths": ["/srv/site"],
                }
            ],
            "platform_profiles": [
                {"profile_id": "test", "name": "Test", "environment": "test", "source": "operator"}
            ],
            "project_ids": [project.project_id],
        },
        source_key="import:selected",
    )
    for revision in range(1, 22):
        record_event(
            connection,
            "selected",
            revision,
            {"kind": "audit", "summary": "Read-only", "checked_at": "2026-10-05T00:00:00Z"},
        )
    archive_server(connection, "selected", 22)
    save_server(connection, "neighbor", 0, {"name": "Other"})


def test_clear_all_pages_preserves_card_links_ledgers_neighbors_and_guards(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    initialize_database(path)
    with connect_database(path) as connection:
        _seed(connection)
        before = _snapshot(connection)
        result = clear_server_history(connection, "selected", 23, True)
        after = _snapshot(connection)
        assert result["deleted_events"] == 23 and result["revision"] == 24
        old_row, new_row = before["servers"][1], after["servers"][1]
        assert old_row[0] == new_row[0] == "selected"
        assert new_row[1] == old_row[1] + 1
        assert new_row[2:6] == old_row[2:6]  # archive/data/source/created_at
        assert new_row[6] == result["updated_at"]
        assert new_row[6] > old_row[6]
        assert before["servers"][0] == after["servers"][0]
        assert after["server_events"] == [
            row for row in before["server_events"] if row[1] != "selected"
        ]
        for table in (
            "server_projects",
            "server_imported_sites",
            "server_imported_profiles",
            "sqlite_master",
            "sqlite_sequence",
        ):
            assert after[table] == before[table]
        card = get_server(connection, "selected", history_limit=20)
        assert card["history"] == [] and not card["history_truncated"]
        for sql in ("DELETE FROM server_events", "UPDATE server_events SET kind='operation'"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(sql)
            connection.rollback()
        # New ordinary events still append after the explicit clear, at the next revision.
        card = record_event(
            connection,
            "selected",
            24,
            {
                "kind": "operation",
                "summary": "Service checked",
                "checked_at": "2026-10-05T00:00:00Z",
            },
        )
        assert card["revision"] == 25 and len(card["history"]) == 1
        assert card["history"][0]["sequence"] > max(row[0] for row in before["server_events"])


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"server_id": "selected", "expected_revision": 23},
        {"server_id": "selected", "expected_revision": 23, "confirm": False},
        {"server_id": "selected", "expected_revision": 23, "confirm": 1},
        {"server_id": "selected", "expected_revision": 23, "confirm": "true"},
        {"server_id": "selected", "expected_revision": True, "confirm": True},
        {"server_id": "selected", "expected_revision": 0, "confirm": True},
        {"server_id": "selected", "expected_revision": -1, "confirm": True},
        {"server_id": "selected", "expected_revision": "23", "confirm": True},
        {"server_id": "selected", "expected_revision": 22, "confirm": True},
        {"server_id": "missing", "expected_revision": 23, "confirm": True},
        {"server_id": "../selected", "expected_revision": 23, "confirm": True},
        {"server_id": True, "expected_revision": 23, "confirm": True},
        {"server_id": "selected", "expected_revision": 23, "confirm": True, "all": True},
    ],
)
def test_invalid_clear_never_mutates(tmp_path: Path, arguments: dict[str, object]) -> None:
    path = tmp_path / "state.db"
    initialize_database(path)
    with connect_database(path) as connection:
        _seed(connection)
        before = _snapshot(connection)
        with pytest.raises(ServerError):
            dispatch_servers(connection, "clear_history", arguments)
        assert not connection.in_transaction and _snapshot(connection) == before


def test_clear_failure_rolls_back_deleted_events_revision_and_trigger(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    initialize_database(path)
    with connect_database(path) as connection:
        _seed(connection)
        connection.execute(
            "CREATE TRIGGER fail_clear BEFORE UPDATE ON servers WHEN NEW.id='selected' "
            "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
        )
        before = _snapshot(connection)
        with pytest.raises(sqlite3.IntegrityError, match="injected write failure"):
            clear_server_history(connection, "selected", 23, True)
        assert not connection.in_transaction and _snapshot(connection) == before
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM server_events")
        connection.rollback()


@pytest.mark.parametrize("unexpected", [False, True])
def test_missing_or_changed_delete_guard_refuses_without_mutation(
    tmp_path: Path, unexpected: bool
) -> None:
    path = tmp_path / "state.db"
    initialize_database(path)
    with connect_database(path) as connection:
        _seed(connection)
        connection.execute("DROP TRIGGER server_events_no_delete")
        if unexpected:
            connection.execute(
                "CREATE TRIGGER server_events_no_delete BEFORE DELETE ON server_events "
                "BEGIN SELECT RAISE(ABORT, 'changed policy'); END"
            )
        before = _snapshot(connection)
        with pytest.raises(ServerError, match="protection"):
            clear_server_history(connection, "selected", 23, True)
        assert not connection.in_transaction and _snapshot(connection) == before


def test_concurrent_clears_admit_only_one_cas_writer(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    initialize_database(path)
    with connect_database(path) as connection:
        _seed(connection)

    def attempt() -> str:
        with connect_database(path) as connection:
            try:
                clear_server_history(connection, "selected", 23, True)
                return "cleared"
            except ServerConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["cleared", "conflict"]
    with connect_database(path) as connection:
        assert get_server(connection, "selected")["revision"] == 24
        assert get_server(connection, "selected")["history"] == []
        assert len(get_server(connection, "neighbor")["history"]) == 1


@pytest.mark.anyio
async def test_real_cli_ipc_clear_and_mcp_cannot_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_harness_http_ports: None
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        request_servers(
            paths.socket,
            "save",
            {"server_id": "selected", "expected_revision": 0, "metadata": {"name": "Selected"}},
        )
        request_servers(
            paths.socket,
            "save",
            {"server_id": "neighbor", "expected_revision": 0, "metadata": {"name": "Neighbor"}},
        )
        before = request_servers(paths.socket, "get", {"server_id": "selected"})
        neighbor = request_servers(paths.socket, "get", {"server_id": "neighbor"})
        token = read_dashboard_access_token(paths.database)
        assert token is not None
        url = f"http://127.0.0.1:{mcp_http_listen_port(paths.socket)}/servers/mcp"
        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}"}
        ) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client), mode="legacy"
            ) as client:
                assert [tool.name for tool in (await client.list_tools()).tools] == [
                    "server_list",
                    "server_get",
                    "server_save",
                    "server_archive",
                    "server_record",
                ]
                with pytest.raises(MCPError):
                    await client.call_tool(
                        "server_clear_history",
                        {"server_id": "selected", "expected_revision": 1, "confirm": True},
                    )
                with pytest.raises(MCPError):
                    await client.call_tool(
                        "server_record",
                        {"server_id": "selected", "expected_revision": 1, "confirm": True},
                    )
        command = [
            sys.executable,
            "-c",
            "from harness.entrypoints import harness_main; raise SystemExit(harness_main())",
            "servers",
            "clear-history",
            "selected",
            "--expected-revision",
            "1",
        ]
        rejected = subprocess.run(
            command, capture_output=True, text=True, timeout=20, env=os.environ.copy()
        )
        assert rejected.returncode != 0 and "--confirm" in rejected.stdout
        assert request_servers(paths.socket, "get", {"server_id": "selected"}) == before
        cleared = subprocess.run(
            [*command, "--confirm"],
            capture_output=True,
            text=True,
            timeout=20,
            env=os.environ.copy(),
        )
        assert cleared.returncode == 0, cleared.stderr
        assert json.loads(cleared.stdout)["deleted_events"] == 1
        after = request_servers(paths.socket, "get", {"server_id": "selected"})
        assert after["history"] == [] and after["revision"] == 2
        assert after["metadata"] == before["metadata"]
        assert request_servers(paths.socket, "get", {"server_id": "neighbor"}) == neighbor
        stale = subprocess.run(
            [*command, "--confirm"],
            capture_output=True,
            text=True,
            timeout=20,
            env=os.environ.copy(),
        )
        assert stale.returncode != 0
        assert request_servers(paths.socket, "get", {"server_id": "selected"}) == after
        with pytest.raises(IpcRemoteError):
            request_servers(
                paths.socket,
                "clear_history",
                {"server_id": "selected", "expected_revision": True, "confirm": True},
            )
    finally:
        request_shutdown(paths.socket)
        future.result(timeout=10)
        executor.shutdown(wait=True)
