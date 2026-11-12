from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from test_mcp_http_server import _isolated_runtime, _start_daemon

from harness.dashboard import read_dashboard_access_token
from harness.ipc import IpcRemoteError, request_servers, request_shutdown
from harness.runtime_paths import mcp_http_listen_port
from harness.storage import connect_database


@pytest.mark.anyio
async def test_rootless_server_mcp_separate_catalog_authenticated_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_harness_http_ports: None
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        request_servers(paths.socket, "list", {})
        token = read_dashboard_access_token(paths.database)
        assert token is not None
        url = f"http://127.0.0.1:{mcp_http_listen_port(paths.socket)}/servers/mcp"
        async with httpx2.AsyncClient() as raw:
            assert (await raw.post(url, content=b"{}")).status_code == 401
            oversized = await raw.post(
                url,
                content=b"x" * (64 * 1024 + 1),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert oversized.status_code == 413
        headers = {"Authorization": f"Bearer {token}"}
        async with create_mcp_http_client(headers=headers) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client), mode="legacy"
            ) as client:
                tools = (await client.list_tools()).tools
                assert [tool.name for tool in tools] == [
                    "server_list",
                    "server_get",
                    "server_save",
                    "server_archive",
                    "server_record",
                ]
                assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)
                assert (await client.call_tool("server_list", {})).structured_content[
                    "scope"
                ] == "local_user"
                saved = await client.call_tool(
                    "server_save",
                    {
                        "server_id": "projectless",
                        "expected_revision": 0,
                        "metadata": {
                            "name": "Synthetic host",
                            "domains": ["test.example.invalid"],
                            "passport": {"cpu_cores": 8, "memory_mib": 24576, "swap_mib": 0},
                            "facts": [
                                {
                                    "text": "Fixture service state",
                                    "category": "services",
                                    "source": "fixture",
                                    "checked_at": None,
                                }
                            ],
                        },
                    },
                )
                assert saved.structured_content["revision"] == 1
                card = await client.call_tool("server_get", {"server_id": "projectless"})
                assert card.structured_content["metadata"]["name"] == "Synthetic host"
                assert card.structured_content["metadata"]["passport"]["memory_mib"] == 24576
                assert card.structured_content["metadata"]["passport"]["swap_mib"] == 0
                assert card.structured_content["metadata"]["facts"][0]["category"] == "services"
                for arguments in (
                    {
                        "server_id": "projectless",
                        "expected_revision": 0,
                        "metadata": {"name": "Stale"},
                    },
                    {
                        "server_id": "secret",
                        "expected_revision": 0,
                        "metadata": {"name": "password=CANARY"},
                    },
                    {
                        "server_id": "secret",
                        "expected_revision": 0,
                        "metadata": {"name": "Safe"},
                        "password": "CANARY",
                    },
                ):
                    with pytest.raises(MCPError) as error:
                        await client.call_tool("server_save", arguments)
                    assert "CANARY" not in str(error.value)
                recorded = await client.call_tool(
                    "server_record",
                    {
                        "server_id": "projectless",
                        "expected_revision": 1,
                        "event": {
                            "kind": "audit",
                            "summary": "Read-only verified",
                            "checked_at": "2026-10-05T00:00:00Z",
                        },
                    },
                )
                assert recorded.structured_content["revision"] == 2
                assert len(recorded.model_dump_json().encode()) < 14 * 1024
        persisted = request_servers(paths.socket, "get", {"server_id": "projectless"})
        assert persisted["history"][0]["data"]["summary"] == "Read-only verified"
        assert [event["kind"] for event in persisted["history"]] == ["audit", "created"]
        # Infrastructure history remains independent; journaling never invents a project Task.
        with connect_database(paths.database) as connection:
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
        assert "CANARY" not in json.dumps(persisted)
    finally:
        request_shutdown(paths.socket)
        future.result(timeout=10)
        executor.shutdown(wait=True)


@pytest.mark.anyio
async def test_malformed_unicode_rejection_keeps_real_ipc_and_mcp_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_harness_http_ports: None
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        for operation, arguments in (
            (
                "save",
                {"server_id": "malformed", "expected_revision": 0, "metadata": {"name": "\ud800"}},
            ),
            (
                "save",
                {
                    "server_id": "malformed",
                    "expected_revision": 0,
                    "metadata": {"name": "Safe", "aliases": ["\udfff"]},
                },
            ),
            ("list", {"query": "\ud800"}),
            ("list", {"environment": "\udfff"}),
        ):
            with pytest.raises(IpcRemoteError) as error:
                request_servers(paths.socket, operation, arguments)
            assert error.value.code == "invalid_server_request"
            assert len(str(error.value).encode()) < 200
            assert request_servers(paths.socket, "list", {})["total"] == 0
            assert not future.done()
        token = read_dashboard_access_token(paths.database)
        assert token is not None
        url = f"http://127.0.0.1:{mcp_http_listen_port(paths.socket)}/servers/mcp"
        headers = {"Authorization": f"Bearer {token}"}
        # ASCII JSON reaches the real wire with a JSON-escaped lone surrogate.
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                    "name": "server_save",
                    "arguments": {
                        "server_id": "malformed",
                        "expected_revision": 0,
                        "metadata": {"name": "\ud800"},
                    },
                },
            }
        ).encode()
        async with httpx2.AsyncClient() as raw:
            response = await raw.post(
                url,
                content=payload,
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "MCP-Method": "tools/call",
                    "MCP-Name": "server_save",
                },
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == -32602
            assert "Server request rejected" in response.text
            assert "\\ud800" not in response.text and len(response.content) < 1000
        async with create_mcp_http_client(headers=headers) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client), mode="legacy"
            ) as client:
                listed = await client.call_tool("server_list", {})
                assert listed.structured_content["total"] == 0
        assert not future.done()
    finally:
        request_shutdown(paths.socket)
        future.result(timeout=10)
        executor.shutdown(wait=True)


@pytest.mark.anyio
async def test_multisite_cyrillic_card_larger_than_project_budget_roundtrips_real_server_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_harness_http_ports: None
) -> None:
    from test_server_sites import large_metadata

    from harness.servers import MAX_RESPONSE_BYTES

    paths = _isolated_runtime(tmp_path, monkeypatch)
    _executor, future = _start_daemon(paths.database, paths.socket)
    try:
        request_servers(paths.socket, "list", {})
        token = read_dashboard_access_token(paths.database)
        url = f"http://127.0.0.1:{mcp_http_listen_port(paths.socket)}/servers/mcp"
        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}"}
        ) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client), mode="legacy"
            ) as client:
                tools = (await client.list_tools()).tools
                assert len(tools) == 5
                description = next(tool.description for tool in tools if tool.name == "server_save")
                assert (
                    description and "platform_profiles" in description and "site_id" in description
                )
                result = await client.call_tool(
                    "server_save",
                    {"server_id": "large", "expected_revision": 0, "metadata": large_metadata()},
                )
                assert len(result.structured_content["metadata"]["sites"]) == 5
                assert result.structured_content["sites_page"]["full_total"] == 16
                assert (
                    len(json.dumps(result.structured_content, ensure_ascii=True).encode())
                    <= MAX_RESPONSE_BYTES
                )
                assert len(result.model_dump_json().encode()) <= 52 * 1024
                result = await client.call_tool(
                    "server_get", {"server_id": "large", "history_limit": 20}
                )
                assert len(result.structured_content["metadata"]["sites"]) == 5
                assert result.structured_content["sites_page"]["full_total"] == 16
                page = await client.call_tool(
                    "server_get",
                    {
                        "server_id": "large",
                        "sites_offset": 15,
                        "expected_revision": 1,
                        "history_limit": 0,
                        "profiles_limit": 0,
                    },
                )
                assert page.structured_content["metadata"]["sites"][0]["site_id"] == "app-15"
                changed = {
                    **page.structured_content["metadata"]["sites"][0],
                    "purpose": "Selected late child",
                }
                updated = await client.call_tool(
                    "server_save", {"server_id": "large", "expected_revision": 1, "site": changed}
                )
                assert updated.structured_content["revision"] == 2
                with pytest.raises(MCPError):
                    await client.call_tool(
                        "server_get",
                        {"server_id": "large", "sites_offset": 5, "expected_revision": 1},
                    )
                with pytest.raises(MCPError):
                    await client.call_tool(
                        "server_save",
                        {"server_id": "large", "expected_revision": 2, "metadata": {"sites": []}},
                    )
                query = await client.call_tool("server_list", {"query": "Площадка 15"})
                assert query.structured_content["total"] == 1
                with pytest.raises(MCPError) as error:
                    await client.call_tool(
                        "server_save",
                        {
                            "server_id": "bad",
                            "expected_revision": 0,
                            "metadata": {
                                "name": "bad",
                                "sites": [
                                    {
                                        "site_id": "x",
                                        "name": "safe",
                                        "source": "operator",
                                        "dependencies": ["password=CANARY"],
                                    }
                                ],
                            },
                        },
                    )
                assert "CANARY" not in str(error.value)
        assert (
            len(request_servers(paths.socket, "get", {"server_id": "large"})["metadata"]["sites"])
            == 5
        )
    finally:
        request_shutdown(paths.socket)
        future.result(timeout=10)


@pytest.mark.anyio
async def test_linked_map_real_ipc_mcp_contract_and_partial_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_harness_http_ports: None
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        request_servers(
            paths.socket,
            "save",
            {
                "server_id": "map-fixture",
                "expected_revision": 0,
                "metadata": {"name": "Synthetic map"},
            },
        )
        token = read_dashboard_access_token(paths.database)
        assert token is not None
        url = f"http://127.0.0.1:{mcp_http_listen_port(paths.socket)}/servers/mcp"
        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}"}
        ) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client), mode="legacy"
            ) as client:
                schema = (
                    await client.call_tool(
                        "server_get", {"server_id": "map-fixture", "collection": "schema"}
                    )
                ).structured_content
                assert schema["schema"]["object_fields"]["certificate_use"]["observed_at"] == "date"
                assert "check_publish" in schema["schema"]["operations"]
                assert schema["schema"]["object_fields"]["directory"]["entry_type"] == "entry_type"
                assert schema["schema"]["object_fields"]["route"]["probe_scope"] == "probe_scope"
                assert schema["schema"]["read_derived"]["related_maximum_per_kind"] == 6
                revision = 1

                async def save(operation: Mapping[str, object]) -> dict[str, object]:
                    nonlocal revision
                    result = (
                        await client.call_tool(
                            "server_save",
                            {
                                "server_id": "map-fixture",
                                "expected_revision": revision,
                                "map_operation": operation,
                            },
                        )
                    ).structured_content
                    assert isinstance(result, dict)
                    revision = result["revision"]
                    return result

                await save(
                    {
                        "action": "upsert",
                        "kind": "application",
                        "object_id": "app",
                        "data": {
                            "name": "Shop",
                            "usage": "closed",
                            "owner": "Operator",
                            "source": "fixture",
                        },
                    }
                )
                await save(
                    {
                        "action": "upsert",
                        "kind": "domain",
                        "object_id": "dom",
                        "data": {
                            "name": "map.example.test",
                            "hostname": "map.example.test",
                            "source": "fixture",
                        },
                    }
                )
                await save(
                    {
                        "action": "link",
                        "link": {
                            "link_id": "app-domain",
                            "from_kind": "application",
                            "from_id": "app",
                            "to_kind": "domain",
                            "to_id": "dom",
                            "role": "serves",
                            "source": "fixture config",
                            "checked_at": "2026-10-05T00:00:00Z",
                            "basis": "configuration",
                            "reason": "Explicit mapping",
                        },
                    }
                )
                found = (
                    await client.call_tool(
                        "server_get",
                        {
                            "server_id": "map-fixture",
                            "collection": "application",
                            "query": "map.example.test",
                            "expected_revision": revision,
                        },
                    )
                ).structured_content
                assert found["matched"] == found["total"] == 1 and found["complete"]
                coverage = [
                    {
                        "area": "domain",
                        "state": "partial",
                        "source": "fixture",
                        "reason": "One public endpoint",
                    }
                ]
                await save(
                    {
                        "action": "check_start",
                        "check_id": "scan",
                        "coverage": coverage,
                        "source": "fixture",
                        "started_at": "2026-10-05T00:00:00Z",
                    }
                )
                portion = {
                    "action": "check_append",
                    "check_id": "scan",
                    "batch": 0,
                    "observations": [
                        {
                            "kind": "domain",
                            "object_id": "dom",
                            "values": {
                                "availability": "login",
                                "discovery_sources": ["fixture nginx", "fixture TLS SAN"],
                            },
                            "source": "fixture",
                            "checked_at": "2026-10-05T00:00:00Z",
                            "basis": "probe",
                            "reason": "Login page only",
                        }
                    ],
                }
                await save(portion)
                unchanged = revision
                assert (await save(portion))["already_appended"] and revision == unchanged
                await save(
                    {
                        "action": "check_publish",
                        "check_id": "scan",
                        "coverage": coverage,
                        "finished_at": "2026-10-05T00:00:01Z",
                    }
                )
                exact = (
                    await client.call_tool(
                        "server_get",
                        {"server_id": "map-fixture", "collection": "domain", "object_id": "dom"},
                    )
                ).structured_content
                assert exact["object"]["availability"] == "login" and exact["links_total"] == 1
                assert exact["object"]["discovery_sources"] == ["fixture nginx", "fixture TLS SAN"]
                operator = request_servers(
                    paths.socket,
                    "get",
                    {"server_id": "map-fixture", "collection": "application", "object_id": "app"},
                )["object"]
                assert operator["usage"] == "closed" and operator["owner"] == "Operator"
                for operation in (
                    {"action": "delete", "kind": "application", "object_id": "app"},
                    {
                        "action": "upsert",
                        "kind": "certificate_use",
                        "object_id": "secret",
                        "data": {
                            "name": "Safe",
                            "source": "fixture",
                            "key_reference": "password=CANARY",
                        },
                    },
                ):
                    with pytest.raises(MCPError) as error:
                        await save(operation)
                    assert "CANARY" not in str(error.value)
                assert len(json.dumps(exact, ensure_ascii=True).encode()) < 48 * 1024
        with connect_database(paths.database) as connection:
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    finally:
        request_shutdown(paths.socket)
        future.result(timeout=10)
        executor.shutdown(wait=True)


@pytest.mark.anyio
async def test_malformed_probe_urls_rollback_and_keep_actual_mcp_ipc_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_harness_http_ports: None
) -> None:
    paths = _isolated_runtime(tmp_path, monkeypatch)
    executor, future = _start_daemon(paths.database, paths.socket)
    try:
        request_servers(
            paths.socket,
            "save",
            {
                "server_id": "fixture",
                "expected_revision": 0,
                "metadata": {"name": "Malformed probe URL fixture"},
            },
        )
        token = read_dashboard_access_token(paths.database)
        url = f"http://127.0.0.1:{mcp_http_listen_port(paths.socket)}/servers/mcp"

        def snapshot() -> list[list[tuple[object, ...]]]:
            with connect_database(paths.database) as c:
                return [
                    c.execute(f"SELECT * FROM {table}").fetchall()
                    for table in (
                        "servers",
                        "server_map_objects",
                        "server_map_links",
                        "server_map_checks",
                        "server_map_batches",
                        "server_events",
                    )
                ]

        before = snapshot()
        async with httpx2.AsyncClient(timeout=5) as raw:
            for number, probe_url in enumerate(
                ("https://[broken/", "https://example.test\uff0fhealth")
            ):
                payload = {
                    "jsonrpc": "2.0",
                    "id": number,
                    "method": "tools/call",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                        "name": "server_save",
                        "arguments": {
                            "server_id": "fixture",
                            "expected_revision": 1,
                            "map_operation": {
                                "action": "upsert",
                                "kind": "domain",
                                "object_id": "api",
                                "data": {
                                    "name": "api.example.test",
                                    "hostname": "api.example.test",
                                    "source": "fixture",
                                    "probe_scope": "functional",
                                    "probe_url": probe_url,
                                    "expected_result": "HTTP 200",
                                },
                            },
                        },
                    },
                }
                response = await raw.post(
                    url,
                    content=json.dumps(payload).encode(),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2026-07-28",
                        "MCP-Method": "tools/call",
                        "MCP-Name": "server_save",
                    },
                )
                assert response.status_code == 400 and response.json()["error"]["code"] == -32602
                assert "Server request rejected" in response.text and len(response.content) < 1000
                assert probe_url not in response.text
                assert request_servers(paths.socket, "list", {})["total"] == 1
                assert not future.done() and paths.socket.exists()
                assert snapshot() == before
        async with create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}"}
        ) as http_client:
            async with Client(
                streamable_http_client(url, http_client=http_client), mode="legacy"
            ) as client:
                assert (await client.call_tool("server_list", {})).structured_content["total"] == 1
    finally:
        request_shutdown(paths.socket)
        future.result(timeout=10)
        executor.shutdown(wait=True)
