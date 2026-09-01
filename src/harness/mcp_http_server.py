from __future__ import annotations

import secrets
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import uvicorn
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from harness.dashboard import load_or_create_dashboard_access_token
from harness.mcp_bridge import build_mcp_server
from harness.runtime_paths import DASHBOARD_HOST

MCP_HTTP_PATH = "/mcp"
MCP_HTTP_AUTHORIZATION_HEADER = "Authorization"
MCP_HTTP_WORKSPACE_ROOT_HEADER = "X-Harness-Workspace-Root"
_START_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 5.0
_MAX_REQUEST_BODY_BYTES = 16 * 1024


class MCPHTTPServerError(RuntimeError):
    """The daemon-owned loopback MCP endpoint could not start or stop safely."""


class _BearerCapabilityApp:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], token: str) -> None:
        self._app = app
        self._authorization = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            values = [
                value
                for name, value in scope.get("headers", ())
                if name.lower() == b"authorization"
            ]
            if len(values) != 1 or not secrets.compare_digest(values[0], self._authorization):
                await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                    scope, receive, send
                )
                return
        await self._app(scope, receive, send)


class MCPHTTPServerManager:
    """Own one authenticated daemon-lifetime Streamable HTTP MCP listener."""

    def __init__(self, database_path: Path, *, port: int = 0) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise MCPHTTPServerError("MCP HTTP loopback port is invalid")
        self._database_path = database_path
        self._requested_port = port
        self._server: uvicorn.Server | None = None
        self._thread: Thread | None = None
        self._listener: socket.socket | None = None
        self._url: str | None = None
        self._token: str | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            raise MCPHTTPServerError("MCP HTTP server has not started")
        return self._token

    def is_running(self) -> bool:
        return bool(
            self._server is not None
            and self._server.started
            and self._thread is not None
            and self._thread.is_alive()
        )

    def get_url(self) -> str:
        if self.is_running():
            assert self._url is not None
            return self._url
        self.close()
        token = load_or_create_dashboard_access_token(self._database_path)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((DASHBOARD_HOST, self._requested_port))
            listener.listen(128)
            actual_port = listener.getsockname()[1]
            if isinstance(actual_port, bool) or not isinstance(actual_port, int):
                raise MCPHTTPServerError("MCP HTTP listener returned an invalid port")
            mcp = build_mcp_server(workspace_transport="http-header")
            app = _BearerCapabilityApp(
                mcp.streamable_http_app(
                    streamable_http_path=MCP_HTTP_PATH,
                    json_response=False,
                    stateless_http=False,
                    max_request_body_size=_MAX_REQUEST_BODY_BYTES,
                    host=DASHBOARD_HOST,
                ),
                token,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=DASHBOARD_HOST,
                    port=actual_port,
                    log_level="warning",
                    access_log=False,
                )
            )
            thread = Thread(
                target=server.run,
                kwargs={"sockets": [listener]},
                name="harness-mcp-http",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            self._listener = listener
            self._url = f"http://{DASHBOARD_HOST}:{actual_port}{MCP_HTTP_PATH}"
            self._token = token
            thread.start()
            deadline = monotonic() + _START_TIMEOUT_SECONDS
            while not server.started:
                if not thread.is_alive():
                    raise MCPHTTPServerError("MCP HTTP server stopped during startup")
                if monotonic() >= deadline:
                    raise MCPHTTPServerError("MCP HTTP server did not become ready in time")
                sleep(0.01)
            return self._url
        except Exception as exc:
            self.close()
            if isinstance(exc, MCPHTTPServerError):
                raise
            raise MCPHTTPServerError("MCP HTTP server could not be started") from exc

    def close(self) -> None:
        server = self._server
        thread = self._thread
        listener = self._listener
        self._server = None
        self._thread = None
        self._listener = None
        self._url = None
        self._token = None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=_STOP_TIMEOUT_SECONDS)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if thread is not None and thread.is_alive():
            raise MCPHTTPServerError("MCP HTTP server did not stop cleanly")
