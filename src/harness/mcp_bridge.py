from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer

from harness.daemon_autostart import ensure_canonical_daemon
from harness.ipc import (
    TaskCheckpointResult,
    TaskStartResult,
    request_task_checkpoint,
    request_task_start,
    request_workspace_search,
    request_workspace_status,
)
from harness.knowledge import KnowledgeAnchorDraft, KnowledgeDraft, KnowledgeKind
from harness.runtime_paths import default_runtime_paths
from harness.tasks import TaskState, TaskWaitReason
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_SERVER_INSTRUCTIONS = (
    "Use project_status before broad repository exploration. Use project_search to locate likely "
    "code, then read/edit files with native host tools. Start or resume a Harness task before "
    "meaningful changes and checkpoint meaningful progress. Targeted native search remains allowed."
)
_SEARCH_DEFAULT_LIMIT = 5
_SEARCH_HARD_LIMIT = 10
_CONTEXT_HARD_LIMIT = 10
_STATUS_MAX_BYTES = 4 * 1024
_SEARCH_MAX_BYTES = 12 * 1024
_CONTEXT_MAX_BYTES = 12 * 1024
_TASK_MAX_BYTES = 4 * 1024
_WORKSPACE_ROOT_ENV = "HARNESS_WORKSPACE_ROOT"


@dataclass(frozen=True, slots=True)
class KnowledgeAnchorInput:
    """Strict model-facing Knowledge anchor input."""

    path: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeInput:
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
    anchors: list[KnowledgeAnchorInput] = field(default_factory=list)


def build_mcp_server() -> MCPServer:
    """Build the production stdio MCP adapter without owning domain state."""
    server = MCPServer(
        "Harness",
        description="Local-first project intelligence and durable task continuity.",
        instructions=_SERVER_INSTRUCTIONS,
        version=distribution_version("harness"),
        log_level="WARNING",
    )

    @server.tool(
        description="Return compact current mechanical state for the active Harness Workspace."
    )
    def project_status() -> dict[str, Any]:
        status = request_workspace_status(_socket_path(), _workspace_hints())
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
                "schema_version": status.schema_version,
            },
            _STATUS_MAX_BYTES,
            "project_status response exceeds model exposure budget",
        )

    @server.tool(
        description=(
            "Search the active Workspace for likely code paths. Returns references and compact "
            "mechanical match reasons; read source with native host file tools."
        )
    )
    def project_search(
        query: str,
        scope: Literal["all", "code", "docs", "knowledge", "tasks"] = "all",
        limit: int = _SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        if not 1 <= limit <= _SEARCH_HARD_LIMIT:
            raise ValueError(f"limit must be between 1 and {_SEARCH_HARD_LIMIT}")
        if scope not in {"all", "code", "docs"}:
            return _bounded(
                {
                    "query": query,
                    "scope": scope,
                    "results": [],
                    "channel_status": "not_indexed_in_current_search_slice",
                },
                _SEARCH_MAX_BYTES,
                "project_search response exceeds model exposure budget",
            )
        result = request_workspace_search(_socket_path(), _workspace_hints(), query, limit=limit)
        hits = []
        for hit in result.results[:limit]:
            if scope == "docs" and not _looks_like_document(hit.relative_path):
                continue
            hits.append(
                {
                    "ref": f"code:{hit.relative_path}",
                    "kind": "code",
                    "title": hit.relative_path.rsplit("/", 1)[-1],
                    "location": hit.relative_path,
                    "short_summary": None,
                    "match_reason": hit.match_kind.value,
                    "freshness": "indexed_snapshot",
                    "path": hit.relative_path,
                }
            )
        return _bounded(
            {"query": query, "scope": scope, "results": hits},
            _SEARCH_MAX_BYTES,
            "project_search response exceeds model exposure budget",
        )

    @server.tool(
        description=(
            "Expand only explicitly selected Harness references. Current code references expose "
            "metadata only; read full source with native host file tools."
        )
    )
    def project_context(refs: list[str]) -> dict[str, Any]:
        if not refs or len(refs) > _CONTEXT_HARD_LIMIT:
            raise ValueError(f"refs must contain between 1 and {_CONTEXT_HARD_LIMIT} items")
        if len(set(refs)) != len(refs):
            raise ValueError("refs must not contain duplicates")
        items: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith("code:"):
                raise ValueError(f"unsupported or invalid context ref: {ref!r}")
            relative_path = ref.removeprefix("code:")
            result = request_workspace_search(
                _socket_path(), _workspace_hints(), relative_path, limit=1
            )
            if not result.results or result.results[0].relative_path != relative_path:
                raise ValueError(f"context ref is not present in the current index: {ref}")
            hit = result.results[0]
            items.append(
                {
                    "ref": ref,
                    "kind": "code",
                    "title": relative_path.rsplit("/", 1)[-1],
                    "location": relative_path,
                    "path": relative_path,
                    "entry_kind": hit.kind.value,
                    "size_bytes": hit.size_bytes,
                    "freshness": "indexed_snapshot",
                }
            )
        return _bounded(
            {"items": items},
            _CONTEXT_MAX_BYTES,
            "project_context response exceeds model exposure budget",
        )

    @server.tool(
        description=(
            "Create a new durable Harness task, or explicitly resume an existing task. Existing "
            "task mutations use revision compare-and-set when required."
        )
    )
    def task_start(
        title: str | None = None,
        task_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        result = request_task_start(
            _socket_path(),
            _workspace_hints(),
            title=title,
            task_id=task_id,
            expected_revision=expected_revision,
        )
        return _bounded(
            _task_start_payload(result), _TASK_MAX_BYTES, "task_start response too large"
        )

    @server.tool(
        description=(
            "Persist meaningful progress for one explicit working Harness task. Requires task_id "
            "and expected_revision; may include bounded semantic Knowledge learned during work."
        )
    )
    def task_checkpoint(
        task_id: str,
        expected_revision: int,
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
    configured = os.environ.get(_WORKSPACE_ROOT_ENV)
    path = Path(configured) if configured else Path.cwd()
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"active Workspace hint cannot be resolved: {path}: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(f"active Workspace hint is not a directory: {resolved}")
    return (
        WorkspaceHint(
            path=resolved,
            source="mcp-configured-root" if configured else "mcp-process-cwd",
            match_mode=(
                WorkspaceHintMatchMode.ROOT if configured else WorkspaceHintMatchMode.LOCATION
            ),
        ),
    )


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


def _looks_like_document(path: str) -> bool:
    lowered = path.casefold()
    return lowered.endswith((".md", ".mdx", ".rst", ".txt")) or "/docs/" in f"/{lowered}"


__all__ = ["build_mcp_server", "run_mcp_server"]
