from __future__ import annotations

import json
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.stdio import stdio_server
from mcp.shared.dispatcher import as_request_id, coerce_request_id
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp_types import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    CallToolResult,
    ErrorData,
    InputRequiredResult,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    TextContent,
)
from mcp_types import Tool as MCPTool
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from harness.cursor_adapter import (
    CURSOR_USER_MCP_MISSING_WORKSPACE_ROOT_MESSAGE,
    cursor_user_mcp_missing_workspace_root,
    production_mcp_isolated_checkout_root,
)
from harness.daemon_autostart import ensure_canonical_daemon
from harness.host_adapters import workspace_hints_from_environment
from harness.ipc import (
    IpcRemoteError,
    TaskCheckpointResult,
    TaskStartResult,
    WorkspaceTaskSummary,
    request_project_context,
    request_project_search,
    request_task_checkpoint,
    request_task_start,
    request_workspace_status,
    request_workspace_task_status,
)
from harness.knowledge import KnowledgeAnchorDraft, KnowledgeDraft, KnowledgeKind
from harness.retrieval import MAX_PROJECT_CONTEXT_REF_BYTES, ProjectSearchScope
from harness.runtime_paths import default_runtime_paths
from harness.tasks import TaskState, TaskWaitReason
from harness.workspace_resolution import WorkspaceHint

_OPERATOR_LANGUAGE = "Russian"
_SERVER_INSTRUCTIONS = (
    "Write every operator-facing Task title, summary, next_step, and Knowledge title/body in "
    f"{_OPERATOR_LANGUAGE}. Use project_status before broad repository exploration. Use "
    "project_search across code, docs, Knowledge, and Task history; expand only selected refs "
    "with project_context, then read/edit repository files with native host tools. Start or "
    "resume a Harness task before meaningful changes and checkpoint meaningful progress. Address "
    "pending operator feedback before continuing reviewed work. Targeted native search remains "
    "allowed."
)
_ISOLATED_CHECKOUT_REFUSAL_INSTRUCTIONS = (
    "Production Harness MCP is refused against the Harness source checkout overlay. "
    "Do not call Harness MCP tools. Isolated checkout MCP is the project server "
    "harness-dev: scripts/dev harness mcp without HARNESS_HOST_PROFILE."
)
_ISOLATED_CHECKOUT_REFUSAL_MESSAGE = (
    "production Harness MCP is refused in the Harness source checkout; "
    "use the tracked harness-dev overlay or native host tools"
)
_CURSOR_USER_MCP_REFUSAL_INSTRUCTIONS = (
    "Cursor MCP has no Workspace root. Production Cursor MCP is the project harness "
    "MCP server in this window's .cursor/mcp.json. Enable it with "
    "`agent mcp enable harness`, then fully quit and reopen Cursor. Leftover "
    "user-harness is not Workspace identity. Isolated Harness source checkout uses "
    "harness-dev. Do not call these tools."
)
_SEARCH_DEFAULT_LIMIT = 5
_SEARCH_HARD_LIMIT = 10
_CONTEXT_HARD_LIMIT = 10
_STATUS_MAX_BYTES = 10 * 1024
_SEARCH_MAX_BYTES = 12 * 1024
_CONTEXT_MAX_BYTES = 12 * 1024
_TASK_MAX_BYTES = 4 * 1024
_CONTEXT_REF_MAX_BYTES = MAX_PROJECT_CONTEXT_REF_BYTES
_MCP_WIRE_OVERHEAD_BYTES = 1024
_MCP_STDIO_FRAME_MAX_BYTES = 12 * 1024
_MCP_REQUEST_ID_MAX_BYTES = 256
_MCP_EOF_DRAIN_TIMEOUT_SECONDS = 65.0
_TOOL_RESPONSE_MAX_BYTES = {
    "project_status": _STATUS_MAX_BYTES,
    "project_search": _SEARCH_MAX_BYTES,
    "project_context": _CONTEXT_MAX_BYTES,
    "task_start": _TASK_MAX_BYTES,
    "task_checkpoint": _TASK_MAX_BYTES,
}
_TOOL_ARGUMENTS: dict[str, frozenset[str]] = {
    "project_status": frozenset(),
    "project_search": frozenset({"query", "scope", "limit"}),
    "project_context": frozenset({"refs"}),
    "task_start": frozenset({"title", "stack_hints", "task_id", "expected_revision"}),
    "task_checkpoint": frozenset(
        {
            "task_id",
            "expected_revision",
            "state",
            "summary",
            "next_step",
            "wait_reason",
            "knowledge",
        }
    ),
}


class _StrictInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeAnchorInput(_StrictInputModel):
    """Strict model-facing Knowledge anchor input."""

    path: str
    symbol: str | None = None


class KnowledgeInput(_StrictInputModel):
    """Strict model-facing Knowledge card input."""

    kind: Literal[
        "behavior",
        "data_flow",
        "invariant",
        "architecture_rationale",
        "decision",
        "caveat",
        "operational_detail",
    ]
    title: str
    body: str
    anchors: list[KnowledgeAnchorInput] = Field(default_factory=list)


class HarnessMCPServer(MCPServer):
    """MCPServer with explicit fail-closed model and stdio wire contracts."""

    def __init__(
        self,
        *args: Any,
        tool_refusal_message: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tool_refusal_message = tool_refusal_message

    async def run_stdio_async(self) -> None:
        """Run official SDK stdio with bounded request identity and response frames."""
        async with stdio_server() as (read_stream, write_stream):
            filtered_send, filtered_receive = anyio.create_memory_object_stream[
                SessionMessage | Exception
            ](0)
            bounded_send, bounded_receive = anyio.create_memory_object_stream[SessionMessage](0)
            rejection_send = bounded_send.clone()
            pending_requests: set[str | int] = set()
            pending_condition = anyio.Condition()

            async def add_pending(request_id: str | int) -> bool:
                async with pending_condition:
                    key = coerce_request_id(request_id)
                    if key in pending_requests:
                        return False
                    pending_requests.add(key)
                    return True

            async def settle_pending(request_id: str | int) -> None:
                async with pending_condition:
                    key = coerce_request_id(request_id)
                    if key not in pending_requests:
                        return
                    pending_requests.remove(key)
                    pending_condition.notify_all()

            async def complete_pending(item: SessionMessage) -> None:
                message = item.message
                if not isinstance(message, (JSONRPCResponse, JSONRPCError)) or message.id is None:
                    return
                await settle_pending(message.id)

            async def cancel_pending(item: SessionMessage | Exception) -> None:
                if not isinstance(item, SessionMessage) or not isinstance(
                    item.message, JSONRPCNotification
                ):
                    return
                if item.message.method != "notifications/cancelled":
                    return
                request_id = as_request_id((item.message.params or {}).get("requestId"))
                if request_id is not None:
                    await settle_pending(request_id)

            async def drain_pending_after_eof() -> None:
                # The SDK treats read-stream EOF as abandonment and cancels in-flight handlers.
                # Delay that logical EOF long enough for every request already accepted from
                # physical stdin to put its response onto the outbound stream. The timeout keeps
                # a broken/hung handler from recreating the historical shutdown deadlock.
                with anyio.move_on_after(_MCP_EOF_DRAIN_TIMEOUT_SECONDS):
                    async with pending_condition:
                        while pending_requests:
                            await pending_condition.wait()

            async def relay_inbound() -> None:
                async with filtered_send, rejection_send:
                    async for item in read_stream:
                        if _has_oversized_request_id(item):
                            await rejection_send.send(_oversized_request_id_error())
                            continue
                        if isinstance(item, SessionMessage) and isinstance(
                            item.message, JSONRPCRequest
                        ):
                            if not await add_pending(item.message.id):
                                await rejection_send.send(_duplicate_request_id_error())
                                continue
                        await filtered_send.send(item)
                        await cancel_pending(item)
                    await drain_pending_after_eof()

            async def relay_outbound() -> None:
                try:
                    async with bounded_receive:
                        async for item in bounded_receive:
                            await write_stream.send(_bounded_stdio_message(item))
                            await complete_pending(item)
                finally:
                    with anyio.CancelScope(shield=True):
                        await write_stream.aclose()

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(relay_inbound)
                task_group.start_soon(relay_outbound)
                async with bounded_send:
                    await self._lowlevel_server.run(
                        filtered_receive,
                        bounded_send,
                        self._lowlevel_server.create_initialization_options(),
                    )

    async def list_tools(self) -> list[MCPTool]:
        if self._tool_refusal_message is not None:
            return []
        tools = await super().list_tools()
        for tool in tools:
            if tool.name in _TOOL_ARGUMENTS:
                tool.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        if self._tool_refusal_message is not None:
            raise MCPError(
                code=INVALID_REQUEST,
                message=self._tool_refusal_message,
            )
        allowed = _TOOL_ARGUMENTS.get(name)
        if allowed is not None:
            unknown = sorted(set(arguments).difference(allowed))
            if unknown:
                raise MCPError(
                    code=INVALID_PARAMS,
                    message="Unknown tool argument fields",
                    data={"tool": name, "unknown_field_count": len(unknown)},
                )
        result = await super().call_tool(name, arguments, context)
        if isinstance(result, CallToolResult):
            return _bounded_call_result(name, result)
        return result


def _mcp_tool_refusal() -> tuple[str, str] | None:
    """Return model-facing instructions and call error when tools must not be exposed."""
    if cursor_user_mcp_missing_workspace_root():
        return (
            _CURSOR_USER_MCP_REFUSAL_INSTRUCTIONS,
            CURSOR_USER_MCP_MISSING_WORKSPACE_ROOT_MESSAGE,
        )
    if production_mcp_isolated_checkout_root() is not None:
        return (_ISOLATED_CHECKOUT_REFUSAL_INSTRUCTIONS, _ISOLATED_CHECKOUT_REFUSAL_MESSAGE)
    return None


def build_mcp_server() -> MCPServer:
    """Build the production stdio MCP adapter without owning domain state."""
    refusal = _mcp_tool_refusal()
    server = HarnessMCPServer(
        "Harness",
        description="Local-first project intelligence and durable task continuity.",
        instructions=refusal[0] if refusal is not None else _SERVER_INSTRUCTIONS,
        version=distribution_version("harness"),
        log_level="WARNING",
        tool_refusal_message=None if refusal is None else refusal[1],
    )

    @server.tool(
        description="Return compact current Workspace, Git, index, and durable Task continuity state."
    )
    def project_status() -> dict[str, Any]:
        socket_path = _socket_path()
        hints = _workspace_hints()
        status = request_workspace_status(socket_path, hints)
        task_status = request_workspace_task_status(socket_path, hints)
        if (
            task_status.workspace_id != status.workspace_id
            or task_status.schema_version != status.schema_version
        ):
            raise ValueError("project_status Workspace changed during bounded status read")
        selected_task = task_status.task
        current_task = (
            _status_task_payload(selected_task)
            if selected_task is not None and selected_task.state is TaskState.WORKING
            else None
        )
        relevant_waiting_task = (
            _status_task_payload(selected_task)
            if selected_task is not None and selected_task.state is TaskState.WAITING
            else None
        )
        last_checkpoint = task_status.last_checkpoint
        return _bounded(
            {
                "project_id": status.project_id,
                "workspace_id": status.workspace_id,
                "visibility_mode": status.visibility_mode,
                "workspace_root": str(status.workspace_root),
                "git": {
                    "branch": status.branch,
                    "head": status.head,
                    "dirty_path_count": status.dirty_path_count,
                },
                "index": {"indexed_file_count": status.indexed_file_count},
                "current_task": current_task,
                "relevant_waiting_task": relevant_waiting_task,
                "last_checkpoint": (
                    None
                    if last_checkpoint is None
                    else {
                        "checkpoint_id": last_checkpoint.checkpoint_id,
                        "task_revision": last_checkpoint.task_revision,
                        "state": last_checkpoint.state.value,
                        "wait_reason": (
                            last_checkpoint.wait_reason.value
                            if last_checkpoint.wait_reason is not None
                            else None
                        ),
                    }
                ),
                "next_step": last_checkpoint.next_step if last_checkpoint is not None else None,
                "pending_operator_feedback": task_status.pending_operator_feedback,
                "schema_version": status.schema_version,
            },
            _STATUS_MAX_BYTES,
            "project_status response exceeds model exposure budget",
        )

    @server.tool(
        description=(
            "Search bounded Project Intelligence across code paths, docs, durable Knowledge, and "
            "Task history. Results are compact refs; use project_context only for selected refs."
        )
    )
    def project_search(
        query: str,
        scope: Literal["all", "code", "docs", "knowledge", "tasks"] = "all",
        limit: StrictInt = _SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        if not 1 <= limit <= _SEARCH_HARD_LIMIT:
            raise ValueError(f"limit must be between 1 and {_SEARCH_HARD_LIMIT}")
        result = request_project_search(
            _socket_path(),
            _workspace_hints(),
            query,
            limit=limit,
            scope=ProjectSearchScope(scope),
        )
        hits = [
            {
                "ref": hit.ref,
                "kind": hit.kind.value,
                "title": hit.title,
                "location": hit.location,
                "short_summary": hit.short_summary,
                "match_reason": hit.match_reason,
                "freshness": hit.freshness,
                **({"path": hit.path} if hit.path is not None else {}),
            }
            for hit in result.results[:limit]
        ]
        return _bounded(
            {"query": query, "scope": scope, "results": hits},
            _SEARCH_MAX_BYTES,
            "project_search response exceeds model exposure budget",
        )

    @server.tool(
        description=(
            "Expand only explicitly selected Project Intelligence refs. Code/doc refs expose "
            "metadata only; Knowledge and Task refs expose bounded durable semantic context."
        )
    )
    def project_context(refs: list[str]) -> dict[str, Any]:
        if not refs or len(refs) > _CONTEXT_HARD_LIMIT:
            raise ValueError(f"refs must contain between 1 and {_CONTEXT_HARD_LIMIT} items")
        if len(set(refs)) != len(refs):
            raise ValueError("refs must not contain duplicates")
        for ref in refs:
            if (
                not isinstance(ref, str)
                or not ref
                or "\x00" in ref
                or len(ref.encode("utf-8")) > _CONTEXT_REF_MAX_BYTES
            ):
                raise ValueError("unsupported or invalid context ref")
        try:
            result = request_project_context(_socket_path(), _workspace_hints(), refs)
        except IpcRemoteError as exc:
            if exc.code in {"context_ref_error", "retrieval_error"}:
                raise ValueError("context ref is not available in the active Project") from exc
            raise
        items: list[dict[str, Any]] = []
        for item in result.items:
            payload: dict[str, Any] = {"ref": item.ref, "kind": item.kind.value, **item.data}
            if item.kind.value in {"code", "doc"}:
                path = item.data.get("path")
                if not isinstance(path, str) or not path:
                    raise ValueError("Structural Index context item has invalid path metadata")
                payload.update(
                    {
                        "title": path.rsplit("/", 1)[-1],
                        "location": path,
                        "freshness": "indexed_snapshot",
                    }
                )
            items.append(payload)
        return _bounded(
            {"items": items},
            _CONTEXT_MAX_BYTES,
            "project_context response exceeds model exposure budget",
        )

    @server.tool(
        description=(
            "Create a new durable Harness task, or explicitly resume an existing task. A new "
            f"title is operator-facing {_OPERATOR_LANGUAGE}. Existing task mutations use "
            "revision compare-and-set when required."
        )
    )
    def task_start(
        title: str | None = None,
        stack_hints: list[str] | None = None,
        task_id: str | None = None,
        expected_revision: StrictInt | None = None,
    ) -> dict[str, Any]:
        result = request_task_start(
            _socket_path(),
            _workspace_hints(),
            title=title,
            stack_hints=() if stack_hints is None else stack_hints,
            task_id=task_id,
            expected_revision=expected_revision,
        )
        return _bounded(
            _task_start_payload(result), _TASK_MAX_BYTES, "task_start response too large"
        )

    @server.tool(
        description=(
            "Persist meaningful progress for one explicit working Harness task. Requires task_id "
            "and expected_revision. Write summary, next_step, and Knowledge title/body in "
            f"{_OPERATOR_LANGUAGE}."
        )
    )
    def task_checkpoint(
        task_id: str,
        expected_revision: StrictInt,
        state: Literal["working", "waiting", "completed"],
        summary: str,
        next_step: str | None = None,
        wait_reason: Literal["operator_review", "operator_input", "external"] | None = None,
        knowledge: list[KnowledgeInput] | None = None,
    ) -> dict[str, Any]:
        result = request_task_checkpoint(
            _socket_path(),
            _workspace_hints(),
            task_id,
            expected_revision=expected_revision,
            state=TaskState(state),
            summary=summary,
            next_step=next_step,
            wait_reason=TaskWaitReason(wait_reason) if wait_reason is not None else None,
            knowledge=_knowledge_drafts(knowledge or []),
        )
        return _bounded(
            _task_checkpoint_payload(result),
            _TASK_MAX_BYTES,
            "task_checkpoint response too large",
        )

    return server


def run_mcp_server() -> None:
    """Run the production MCP stdio transport."""
    build_mcp_server().run(transport="stdio")


def _socket_path() -> Path:
    paths = default_runtime_paths()
    ensure_canonical_daemon(paths)
    return paths.socket


def _workspace_hints() -> tuple[WorkspaceHint, ...]:
    return workspace_hints_from_environment()


def _knowledge_drafts(items: list[KnowledgeInput]) -> tuple[KnowledgeDraft, ...]:
    return tuple(
        KnowledgeDraft(
            kind=KnowledgeKind(item.kind),
            title=item.title,
            body=item.body,
            anchors=tuple(
                KnowledgeAnchorDraft(path=anchor.path, symbol=anchor.symbol)
                for anchor in item.anchors
            ),
        )
        for item in items
    )


def _task_start_payload(result: TaskStartResult) -> dict[str, Any]:
    return {
        "workspace_id": result.workspace_id,
        "task_id": result.task_id,
        "state": result.state.value,
        "wait_reason": result.wait_reason.value if result.wait_reason is not None else None,
        "revision": result.revision,
    }


def _status_task_payload(task: WorkspaceTaskSummary) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "state": task.state.value,
        "wait_reason": task.wait_reason.value if task.wait_reason is not None else None,
        "revision": task.revision,
    }


def _task_checkpoint_payload(result: TaskCheckpointResult) -> dict[str, Any]:
    return {
        "workspace_id": result.workspace_id,
        "task_id": result.task_id,
        "state": result.state.value,
        "wait_reason": result.wait_reason.value if result.wait_reason is not None else None,
        "revision": result.revision,
        "checkpoint_id": result.checkpoint_id,
        "knowledge_ids": list(result.knowledge_ids),
    }


def _bounded(payload: dict[str, Any], limit: int, message: str) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError(message)
    return payload


def _bounded_call_result(name: str, result: CallToolResult) -> CallToolResult:
    limit = _TOOL_RESPONSE_MAX_BYTES.get(name)
    if limit is None:
        return result
    encoded = result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    if len(encoded) + _MCP_WIRE_OVERHEAD_BYTES <= limit:
        return result
    return CallToolResult(
        content=[TextContent(type="text", text="Tool response exceeds model exposure budget")],
        is_error=True,
    )


def _has_oversized_request_id(item: SessionMessage | Exception) -> bool:
    return (
        isinstance(item, SessionMessage)
        and isinstance(item.message, JSONRPCRequest)
        and _request_id_is_oversized(item.message.id)
    )


def _request_id_is_oversized(request_id: str | int) -> bool:
    encoded_id = json.dumps(request_id, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(encoded_id) > _MCP_REQUEST_ID_MAX_BYTES


def _oversized_request_id_error() -> SessionMessage:
    return SessionMessage(
        JSONRPCError(
            jsonrpc="2.0",
            id=None,
            error=ErrorData(
                code=INVALID_REQUEST,
                message="JSON-RPC request id exceeds Harness wire budget",
            ),
        )
    )


def _duplicate_request_id_error() -> SessionMessage:
    return SessionMessage(
        JSONRPCError(
            jsonrpc="2.0",
            id=None,
            error=ErrorData(
                code=INVALID_REQUEST,
                message="JSON-RPC request id collides with an in-flight request",
            ),
        )
    )


def _bounded_stdio_message(item: SessionMessage) -> SessionMessage:
    encoded = item.message.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8")
    if len(encoded) + 1 <= _MCP_STDIO_FRAME_MAX_BYTES:
        return item
    response_id: str | int | None = None
    if isinstance(item.message, (JSONRPCResponse, JSONRPCError)):
        candidate = item.message.id
        if candidate is not None and not _request_id_is_oversized(candidate):
            response_id = candidate
    return SessionMessage(
        JSONRPCError(
            jsonrpc="2.0",
            id=response_id,
            error=ErrorData(
                code=INVALID_REQUEST,
                message="MCP response exceeds Harness stdio wire budget",
            ),
        ),
        metadata=item.metadata,
    )


__all__ = ["build_mcp_server", "run_mcp_server"]
