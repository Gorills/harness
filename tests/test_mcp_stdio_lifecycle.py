import json
import subprocess
import sys

import pytest


def test_stdio_transport_exits_cleanly_on_empty_stdin() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "harness.mcp_process"],
        input="",
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""


@pytest.mark.parametrize("protocol_era", ["modern", "legacy"])
def test_stdio_transport_drains_all_accepted_requests_before_eof_shutdown(
    protocol_era: str,
) -> None:
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "eof-burst-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if protocol_era == "modern":
        requests = [
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "server/discover",
                "params": {"_meta": meta},
            }
            for request_id in range(1, 11)
        ]
        expected_ids = list(range(1, 11))
    else:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "eof-burst-review", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            *[
                {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}
                for request_id in range(2, 10)
            ],
        ]
        expected_ids = list(range(1, 10))

    completed = subprocess.run(
        [sys.executable, "-m", "harness.mcp_process"],
        input="".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests),
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert sorted(response["id"] for response in responses) == expected_ids
    assert all("result" in response for response in responses)


def test_stdio_transport_finishes_slow_accepted_request_before_eof_shutdown() -> None:
    script = """
import anyio

from harness.mcp_bridge import HarnessMCPServer

server = HarnessMCPServer("EOF slow review", version="0")

@server.tool()
async def slow() -> dict[str, bool]:
    await anyio.sleep(0.2)
    return {"ok": True}

anyio.run(server.run_stdio_async)
"""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "eof-slow-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": meta, "name": "slow", "arguments": {}},
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 1
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["isError"] is False


def test_stdio_transport_bounds_eof_drain_when_handler_does_not_finish() -> None:
    script = """
import anyio

import harness.mcp_bridge as bridge

bridge._MCP_EOF_DRAIN_TIMEOUT_SECONDS = 0.05
server = bridge.HarnessMCPServer("EOF timeout review", version="0")

@server.tool()
async def slow() -> dict[str, bool]:
    await anyio.sleep(1.0)
    return {"ok": True}

anyio.run(server.run_stdio_async)
"""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "eof-timeout-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": meta, "name": "slow", "arguments": {}},
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(request, separators=(",", ":")) + "\n",
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 1
    assert responses[0]["id"] == 1
    assert responses[0]["error"]["message"] == "Connection closed"


def test_stdio_transport_does_not_wait_for_peer_cancelled_request_after_eof() -> None:
    script = """
import anyio

from harness.mcp_bridge import HarnessMCPServer

server = HarnessMCPServer("EOF cancellation review", version="0")

@server.tool()
async def slow() -> dict[str, bool]:
    await anyio.sleep(1.0)
    return {"ok": True}

anyio.run(server.run_stdio_async)
"""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "eof-cancel-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": meta, "name": "slow", "arguments": {}},
    }
    cancel = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "1", "reason": "review complete"},
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        input="".join(json.dumps(item, separators=(",", ":")) + "\n" for item in (request, cancel)),
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""


@pytest.mark.parametrize("duplicate_id", [1, "1"])
def test_stdio_transport_rejects_duplicate_coerced_request_id_before_dispatch(
    duplicate_id: int | str,
) -> None:
    script = """
import anyio

from harness.mcp_bridge import HarnessMCPServer

server = HarnessMCPServer("Duplicate ID review", version="0")

@server.tool()
async def slow() -> dict[str, str]:
    await anyio.sleep(1.0)
    return {"which": "slow"}

@server.tool()
async def fast() -> dict[str, str]:
    return {"which": "fast"}

anyio.run(server.run_stdio_async)
"""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "duplicate-id-review", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    first = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"_meta": meta, "name": "slow", "arguments": {}},
    }
    duplicate = {
        "jsonrpc": "2.0",
        "id": duplicate_id,
        "method": "tools/call",
        "params": {"_meta": meta, "name": "fast", "arguments": {}},
    }
    cancel = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "1", "reason": "cancel accepted request"},
    }

    completed = subprocess.run(
        [sys.executable, "-c", script],
        input="".join(
            json.dumps(item, separators=(",", ":")) + "\n" for item in (first, duplicate, cancel)
        ),
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32600,
                "message": "JSON-RPC request id collides with an in-flight request",
            },
        }
    ]
