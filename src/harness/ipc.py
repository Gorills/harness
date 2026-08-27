from __future__ import annotations

import json
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from harness.index import IndexedFileKind
from harness.knowledge import (
    MAX_KNOWLEDGE_ANCHORS_PER_CARD,
    MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT,
    KnowledgeAnchorDraft,
    KnowledgeDraft,
    KnowledgeKind,
)
from harness.retrieval import (
    MAX_PROJECT_CONTEXT_REF_BYTES,
    ProjectContextItem,
    ProjectSearchHit,
    ProjectSearchKind,
    ProjectSearchScope,
)
from harness.search import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_QUERY_BYTES,
    IndexedPathSearchScope,
    SearchMatchKind,
)
from harness.task_checkpoints import (
    MAX_CHECKPOINT_NEXT_STEP_BYTES,
    MAX_CHECKPOINT_SUMMARY_BYTES,
    MAX_OPERATOR_FEEDBACK_BYTES,
)
from harness.tasks import (
    MAX_TASK_STACK_HINT_BYTES,
    MAX_TASK_STACK_HINTS,
    MAX_TASK_TITLE_BYTES,
    TaskState,
    TaskWaitReason,
)
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024
_REQUEST_ID_MAX_LENGTH = 64
_HINT_SOURCE_MAX_LENGTH = 64
_HINT_PATH_MAX_LENGTH = 4096
_MAX_WORKSPACE_HINTS = 4
_DEFAULT_TIMEOUT_SECONDS = 2.0
_SCAN_REQUEST_TIMEOUT_SECONDS = 40.0
_TASK_REQUEST_TIMEOUT_SECONDS = 60.0
_TASK_ID_MAX_LENGTH = 128
_INDEX_RELATIVE_PATH_MAX_BYTES = 4096
_PROJECT_CONTEXT_REF_MAX_BYTES = MAX_PROJECT_CONTEXT_REF_BYTES
_PROJECT_CONTEXT_MAX_REFS = 10
_HOST_PROFILE_MAX_BYTES = 64
_HOST_PROFILE_MAX_ITEMS = 8


class IpcError(RuntimeError):
    """Base class for local Harness IPC failures."""


class IpcProtocolError(IpcError):
    """Raised when an IPC peer violates the bounded Harness wire contract."""


class IpcMessageTooLargeError(IpcProtocolError):
    """Raised when one IPC message exceeds the configured hard byte limit."""


class IpcTransportError(IpcError):
    """Raised when the local transport cannot connect or complete a request."""


class UnsupportedIpcTransportError(IpcTransportError):
    """Raised when this bounded implementation has no proven transport for the platform."""


class IpcRemoteError(IpcError):
    """Raised when the daemon returns a structured IPC error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Bounded result returned by the global read-only daemon status request."""

    schema_version: int
    project_count: int
    workspace_count: int


@dataclass(frozen=True, slots=True)
class RuntimeDiagnosticsResult:
    """Read-only daemon runtime identity and subsystem diagnostics."""

    schema_version: int
    package_version: str
    python_executable: str
    code_sha256: str
    project_count: int
    workspace_count: int
    dashboard_running: bool


@dataclass(frozen=True, slots=True)
class DashboardUrlResult:
    """Capability-bearing loopback URL for the daemon-owned local dashboard."""

    url: str


@dataclass(frozen=True, slots=True)
class WorkspaceStatusResult:
    """Bounded Workspace-scoped status returned by the daemon."""

    schema_version: int
    workspace_id: str
    project_id: str
    visibility_mode: str
    workspace_root: Path
    head: str | None
    branch: str | None
    dirty_path_count: int
    indexed_file_count: int


@dataclass(frozen=True, slots=True)
class WorkspaceTaskSummary:
    """Bounded Task identity/state exposed by the daemon for status continuity."""

    task_id: str
    title: str
    state: TaskState
    wait_reason: TaskWaitReason | None
    revision: int


@dataclass(frozen=True, slots=True)
class WorkspaceTaskCheckpointSummary:
    """Bounded latest-checkpoint identity/state exposed for status continuity."""

    checkpoint_id: str
    task_revision: int
    state: TaskState
    wait_reason: TaskWaitReason | None
    next_step: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceTaskStatusResult:
    """Current/relevant Task status for one resolved Workspace."""

    schema_version: int
    workspace_id: str
    task: WorkspaceTaskSummary | None
    last_checkpoint: WorkspaceTaskCheckpointSummary | None
    pending_operator_feedback: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceScanResult:
    """Bounded registration/reconciliation result returned by ``harness scan``."""

    schema_version: int
    workspace_id: str
    project_id: str
    visibility_mode: str
    workspace_root: Path
    project_created: bool
    workspace_created: bool
    file_count: int
    added: int
    updated: int
    removed: int


@dataclass(frozen=True, slots=True)
class WorkspaceSkillsResult:
    """Bounded project-skill reconciliation result returned by the daemon."""

    schema_version: int
    workspace_id: str
    selected_skill_ids: tuple[str, ...]
    materialized: int
    removed: int
    unchanged: int
    exclude_changed: bool


@dataclass(frozen=True, slots=True)
class SkillCleanupResult:
    """Bounded global generated-skill cleanup result returned by the daemon."""

    schema_version: int
    workspace_count: int
    cleaned_workspace_count: int
    skipped_workspace_count: int
    removed: int
    exclude_changed_count: int


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    """Acknowledgement that the daemon accepted a local shutdown request."""

    accepted: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSearchHit:
    """One bounded mechanical search hit returned over local IPC."""

    relative_path: str
    kind: IndexedFileKind
    size_bytes: int
    match_kind: SearchMatchKind


@dataclass(frozen=True, slots=True)
class WorkspaceSearchResult:
    """Bounded Workspace-scoped indexed-path search result returned by the daemon."""

    schema_version: int
    workspace_id: str
    project_id: str
    workspace_root: Path
    results: tuple[WorkspaceSearchHit, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceIndexEntryResult:
    """One exact bounded Structural Index entry returned by the daemon."""

    schema_version: int
    workspace_id: str
    project_id: str
    workspace_root: Path
    relative_path: str
    kind: IndexedFileKind
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ProjectSearchResult:
    """Bounded Project Intelligence search result returned by the daemon."""

    schema_version: int
    workspace_id: str
    project_id: str
    results: tuple[ProjectSearchHit, ...]


@dataclass(frozen=True, slots=True)
class ProjectContextResult:
    """Selective Project Intelligence context expansion returned by the daemon."""

    schema_version: int
    workspace_id: str
    project_id: str
    items: tuple[ProjectContextItem, ...]


@dataclass(frozen=True, slots=True)
class TaskStartRequestData:
    """Validated daemon-domain Task start/resume request data."""

    workspace_hints: tuple[WorkspaceHint, ...]
    title: str | None
    stack_hints: tuple[str, ...]
    task_id: str | None
    expected_revision: int | None


@dataclass(frozen=True, slots=True)
class TaskCheckpointRequestData:
    """Validated daemon-domain Task checkpoint request data."""

    workspace_hints: tuple[WorkspaceHint, ...]
    task_id: str
    expected_revision: int
    state: TaskState
    summary: str
    next_step: str | None
    wait_reason: TaskWaitReason | None
    knowledge: tuple[KnowledgeDraft, ...]


@dataclass(frozen=True, slots=True)
class TaskStartResult:
    """Bounded Task identity/state result returned by task_start IPC."""

    schema_version: int
    workspace_id: str
    task_id: str
    state: TaskState
    wait_reason: TaskWaitReason | None
    revision: int


@dataclass(frozen=True, slots=True)
class TaskCheckpointResult:
    """Bounded Task/checkpoint identities returned by task_checkpoint IPC."""

    schema_version: int
    workspace_id: str
    task_id: str
    state: TaskState
    wait_reason: TaskWaitReason | None
    revision: int
    checkpoint_id: str
    knowledge_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IpcRequest:
    """Validated internal request independent of MCP wire objects."""

    request_id: str
    method: str
    workspace_hints: tuple[WorkspaceHint, ...] = ()
    scan_path: Path | None = None
    search_query: str | None = None
    search_limit: int | None = None
    search_scope: IndexedPathSearchScope | None = None
    index_relative_path: str | None = None
    project_search_scope: ProjectSearchScope | None = None
    context_refs: tuple[str, ...] | None = None
    task_start: TaskStartRequestData | None = None
    task_checkpoint: TaskCheckpointRequestData | None = None
    host_profiles: tuple[str, ...] | None = None


def request_status(
    socket_path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> StatusResult:
    """Request global daemon status over the current POSIX local IPC transport."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {"version": PROTOCOL_VERSION, "request_id": request_id, "method": "status"},
        timeout=timeout,
    )
    return _status_from_response(response, expected_request_id=request_id)


def request_runtime_diagnostics(
    socket_path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> RuntimeDiagnosticsResult:
    """Request read-only daemon runtime identity and subsystem diagnostics."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {"version": PROTOCOL_VERSION, "request_id": request_id, "method": "runtime_diagnostics"},
        timeout=timeout,
    )
    return _runtime_diagnostics_from_response(response, expected_request_id=request_id)


def request_dashboard_url(
    socket_path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> DashboardUrlResult:
    """Ask harnessd for the daemon-owned loopback dashboard URL."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {"version": PROTOCOL_VERSION, "request_id": request_id, "method": "dashboard_url"},
        timeout=timeout,
    )
    return _dashboard_url_from_response(response, expected_request_id=request_id)


def request_workspace_status(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> WorkspaceStatusResult:
    """Request one Workspace-scoped status using ordered absolute filesystem hints."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_status",
            "params": {"hints": _workspace_hints_to_wire(hints)},
        },
        timeout=timeout,
    )
    return _workspace_status_from_response(response, expected_request_id=request_id)


def request_workspace_task_status(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> WorkspaceTaskStatusResult:
    """Request bounded current/relevant Task status for one resolved Workspace."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_task_status",
            "params": {"hints": _workspace_hints_to_wire(hints)},
        },
        timeout=timeout,
    )
    return _workspace_task_status_from_response(response, expected_request_id=request_id)


def request_workspace_scan(
    socket_path: Path,
    path: Path,
    *,
    timeout: float = _SCAN_REQUEST_TIMEOUT_SECONDS,
) -> WorkspaceScanResult:
    """Register/reuse one Git Workspace and request deterministic index reconciliation."""
    scan_path = str(path)
    _validate_scan_path(scan_path)
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "scan_workspace",
            "params": {"path": scan_path},
        },
        timeout=timeout,
    )
    return _workspace_scan_from_response(response, expected_request_id=request_id)


def request_workspace_skills_reconcile(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    profiles: Sequence[str],
    *,
    timeout: float = _SCAN_REQUEST_TIMEOUT_SECONDS,
) -> WorkspaceSkillsResult:
    """Resolve and reconcile project skills for one Workspace through daemon-owned state."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_skills_reconcile",
            "params": {
                "hints": _workspace_hints_to_wire(hints),
                "profiles": _host_profiles_to_wire(profiles),
            },
        },
        timeout=timeout,
    )
    return _workspace_skills_from_response(response, expected_request_id=request_id)


def request_skill_cleanup(
    socket_path: Path,
    profiles: Sequence[str],
    *,
    timeout: float = _SCAN_REQUEST_TIMEOUT_SECONDS,
) -> SkillCleanupResult:
    """Remove Harness-owned generated skills from safely identifiable registered Workspaces."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "skill_cleanup",
            "params": {"profiles": _host_profiles_to_wire(profiles)},
        },
        timeout=timeout,
    )
    return _skill_cleanup_from_response(response, expected_request_id=request_id)


def request_shutdown(
    socket_path: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ShutdownResult:
    """Request a clean shutdown from the current-user Harness daemon."""
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {"version": PROTOCOL_VERSION, "request_id": request_id, "method": "shutdown"},
        timeout=timeout,
    )
    return _shutdown_from_response(response, expected_request_id=request_id)


def request_workspace_search(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    scope: IndexedPathSearchScope | None = None,
) -> WorkspaceSearchResult:
    """Request bounded deterministic indexed-path search for one registered Workspace."""
    _validate_search_query(query)
    _validate_search_limit(limit)
    request_id = uuid4().hex
    params: dict[str, object] = {
        "hints": _workspace_hints_to_wire(hints),
        "query": query,
        "limit": limit,
    }
    if scope is not None:
        if not isinstance(scope, IndexedPathSearchScope):
            raise IpcProtocolError("workspace search scope is unsupported")
        params["scope"] = scope.value
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_search",
            "params": params,
        },
        timeout=timeout,
    )
    return _workspace_search_from_response(response, expected_request_id=request_id)


def request_project_search(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    query: str,
    *,
    scope: ProjectSearchScope,
    limit: int = DEFAULT_SEARCH_LIMIT,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ProjectSearchResult:
    """Request one daemon-owned bounded Project Intelligence search."""
    _validate_search_query(query)
    _validate_search_limit(limit)
    if not isinstance(scope, ProjectSearchScope):
        raise IpcProtocolError("project search scope is unsupported")
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "project_search",
            "params": {
                "hints": _workspace_hints_to_wire(hints),
                "query": query,
                "limit": limit,
                "scope": scope.value,
            },
        },
        timeout=timeout,
    )
    return _project_search_from_response(response, expected_request_id=request_id)


def request_project_context(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    refs: Sequence[str],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> ProjectContextResult:
    """Request exact context only for explicitly selected Project Intelligence refs."""
    normalized = _validate_project_context_refs(refs)
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "project_context",
            "params": {"hints": _workspace_hints_to_wire(hints), "refs": list(normalized)},
        },
        timeout=timeout,
    )
    return _project_context_from_response(response, expected_request_id=request_id)


def request_workspace_index_entry(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    relative_path: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> WorkspaceIndexEntryResult:
    """Request one exact current Structural Index entry through daemon-owned IPC."""
    _validate_index_relative_path(relative_path)
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "workspace_index_entry",
            "params": {
                "hints": _workspace_hints_to_wire(hints),
                "relative_path": relative_path,
            },
        },
        timeout=timeout,
    )
    return _workspace_index_entry_from_response(response, expected_request_id=request_id)


def request_task_start(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    *,
    title: str | None = None,
    stack_hints: Sequence[str] = (),
    task_id: str | None = None,
    expected_revision: int | None = None,
    timeout: float = _TASK_REQUEST_TIMEOUT_SECONDS,
) -> TaskStartResult:
    """Create or resume one explicit Harness Task through daemon-owned IPC."""
    params = _task_start_params_to_wire(
        hints,
        title=title,
        stack_hints=stack_hints,
        task_id=task_id,
        expected_revision=expected_revision,
    )
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "task_start",
            "params": params,
        },
        timeout=timeout,
    )
    return _task_start_from_response(response, expected_request_id=request_id)


def request_task_checkpoint(
    socket_path: Path,
    hints: Sequence[WorkspaceHint],
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    summary: str,
    next_step: str | None = None,
    wait_reason: TaskWaitReason | None = None,
    knowledge: Sequence[KnowledgeDraft] = (),
    timeout: float = _TASK_REQUEST_TIMEOUT_SECONDS,
) -> TaskCheckpointResult:
    """Checkpoint one explicit Harness Task through daemon-owned IPC."""
    params = _task_checkpoint_params_to_wire(
        hints,
        task_id,
        expected_revision=expected_revision,
        state=state,
        summary=summary,
        next_step=next_step,
        wait_reason=wait_reason,
        knowledge=knowledge,
    )
    request_id = uuid4().hex
    response = _request_response(
        socket_path,
        {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "method": "task_checkpoint",
            "params": params,
        },
        timeout=timeout,
    )
    return _task_checkpoint_from_response(response, expected_request_id=request_id)


def receive_request(peer: socket.socket) -> IpcRequest:
    """Receive and validate exactly one bounded request frame."""
    payload = _decode_json(_receive_frame(peer))
    version = payload.get("version")
    request_id = payload.get("request_id")
    method = payload.get("method")
    if isinstance(version, bool) or not isinstance(version, int):
        raise IpcProtocolError("request version must be an integer")
    if version != PROTOCOL_VERSION:
        raise IpcProtocolError(f"unsupported IPC protocol version: {version}")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > _REQUEST_ID_MAX_LENGTH
    ):
        raise IpcProtocolError("request_id must be a non-empty bounded string")
    if not isinstance(method, str):
        raise IpcProtocolError("request method must be a string")

    if method == "status":
        if set(payload) != {"version", "request_id", "method"}:
            raise IpcProtocolError("status request fields do not match the IPC schema")
        return IpcRequest(request_id=request_id, method=method)

    if method == "runtime_diagnostics":
        if set(payload) != {"version", "request_id", "method"}:
            raise IpcProtocolError("runtime diagnostics request fields do not match the IPC schema")
        return IpcRequest(request_id=request_id, method=method)

    if method == "dashboard_url":
        if set(payload) != {"version", "request_id", "method"}:
            raise IpcProtocolError("dashboard URL request fields do not match the IPC schema")
        return IpcRequest(request_id=request_id, method=method)

    if method == "shutdown":
        if set(payload) != {"version", "request_id", "method"}:
            raise IpcProtocolError("shutdown request fields do not match the IPC schema")
        return IpcRequest(request_id=request_id, method=method)

    if method == "workspace_status":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("workspace status request fields do not match the IPC schema")
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=_workspace_hints_from_params(payload["params"]),
        )

    if method == "workspace_task_status":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError(
                "workspace task status request fields do not match the IPC schema"
            )
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=_workspace_hints_from_params(payload["params"]),
        )

    if method == "scan_workspace":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("workspace scan request fields do not match the IPC schema")
        return IpcRequest(
            request_id=request_id,
            method=method,
            scan_path=_scan_path_from_params(payload["params"]),
        )

    if method == "workspace_skills_reconcile":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("workspace skills request fields do not match the IPC schema")
        hints, profiles = _workspace_skills_from_params(payload["params"])
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=hints,
            host_profiles=profiles,
        )

    if method == "skill_cleanup":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("skill cleanup request fields do not match the IPC schema")
        return IpcRequest(
            request_id=request_id,
            method=method,
            host_profiles=_host_profiles_from_params(payload["params"]),
        )

    if method == "workspace_search":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("workspace search request fields do not match the IPC schema")
        hints, query, limit, scope = _workspace_search_from_params(payload["params"])
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=hints,
            search_query=query,
            search_limit=limit,
            search_scope=scope,
        )

    if method == "project_search":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("project search request fields do not match the IPC schema")
        hints, query, limit, project_scope = _project_search_from_params(payload["params"])
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=hints,
            search_query=query,
            search_limit=limit,
            project_search_scope=project_scope,
        )

    if method == "project_context":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("project context request fields do not match the IPC schema")
        hints, refs = _project_context_from_params(payload["params"])
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=hints,
            context_refs=refs,
        )

    if method == "workspace_index_entry":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError(
                "workspace index entry request fields do not match the IPC schema"
            )
        hints, relative_path = _workspace_index_entry_from_params(payload["params"])
        return IpcRequest(
            request_id=request_id,
            method=method,
            workspace_hints=hints,
            index_relative_path=relative_path,
        )

    if method == "task_start":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("task_start request fields do not match the IPC schema")
        task_start = _task_start_from_params(payload["params"])
        return IpcRequest(request_id=request_id, method=method, task_start=task_start)

    if method == "task_checkpoint":
        if set(payload) != {"version", "request_id", "method", "params"}:
            raise IpcProtocolError("task_checkpoint request fields do not match the IPC schema")
        task_checkpoint = _task_checkpoint_from_params(payload["params"])
        return IpcRequest(request_id=request_id, method=method, task_checkpoint=task_checkpoint)

    raise IpcProtocolError("unsupported IPC method")


def send_status_response(peer: socket.socket, request_id: str, status: StatusResult) -> None:
    """Send the exact success contract for the global status path."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": status.schema_version,
                    "project_count": status.project_count,
                    "workspace_count": status.workspace_count,
                },
            }
        )
    )


def send_runtime_diagnostics_response(
    peer: socket.socket, request_id: str, diagnostics: RuntimeDiagnosticsResult
) -> None:
    """Send bounded read-only daemon runtime diagnostics."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": diagnostics.schema_version,
                    "package_version": diagnostics.package_version,
                    "python_executable": diagnostics.python_executable,
                    "code_sha256": diagnostics.code_sha256,
                    "project_count": diagnostics.project_count,
                    "workspace_count": diagnostics.workspace_count,
                    "dashboard_running": diagnostics.dashboard_running,
                },
            }
        )
    )


def send_dashboard_url_response(
    peer: socket.socket, request_id: str, dashboard: DashboardUrlResult
) -> None:
    """Send the exact success contract for daemon-owned dashboard discovery."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {"url": dashboard.url},
            }
        )
    )


def send_shutdown_response(peer: socket.socket, request_id: str) -> None:
    """Acknowledge a clean local daemon shutdown request."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {"accepted": True},
            }
        )
    )


def send_workspace_status_response(
    peer: socket.socket,
    request_id: str,
    status: WorkspaceStatusResult,
) -> None:
    """Send the exact success contract for one Workspace-scoped status request."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": status.schema_version,
                    "workspace_id": status.workspace_id,
                    "project_id": status.project_id,
                    "visibility_mode": status.visibility_mode,
                    "workspace_root": str(status.workspace_root),
                    "head": status.head,
                    "branch": status.branch,
                    "dirty_path_count": status.dirty_path_count,
                    "indexed_file_count": status.indexed_file_count,
                },
            }
        )
    )


def send_workspace_task_status_response(
    peer: socket.socket,
    request_id: str,
    status: WorkspaceTaskStatusResult,
) -> None:
    """Send the exact bounded current/relevant Task status contract."""
    task = status.task
    checkpoint = status.last_checkpoint
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": status.schema_version,
                    "workspace_id": status.workspace_id,
                    "task": (
                        None
                        if task is None
                        else {
                            "task_id": task.task_id,
                            "title": task.title,
                            "state": task.state.value,
                            "wait_reason": (
                                task.wait_reason.value if task.wait_reason is not None else None
                            ),
                            "revision": task.revision,
                        }
                    ),
                    "last_checkpoint": (
                        None
                        if checkpoint is None
                        else {
                            "checkpoint_id": checkpoint.checkpoint_id,
                            "task_revision": checkpoint.task_revision,
                            "state": checkpoint.state.value,
                            "wait_reason": (
                                checkpoint.wait_reason.value
                                if checkpoint.wait_reason is not None
                                else None
                            ),
                            "next_step": checkpoint.next_step,
                        }
                    ),
                    "pending_operator_feedback": status.pending_operator_feedback,
                },
            }
        )
    )


def send_workspace_scan_response(
    peer: socket.socket,
    request_id: str,
    result: WorkspaceScanResult,
) -> None:
    """Send the exact success contract for one deterministic Workspace scan."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "project_id": result.project_id,
                    "visibility_mode": result.visibility_mode,
                    "workspace_root": str(result.workspace_root),
                    "project_created": result.project_created,
                    "workspace_created": result.workspace_created,
                    "file_count": result.file_count,
                    "added": result.added,
                    "updated": result.updated,
                    "removed": result.removed,
                },
            }
        )
    )


def send_workspace_skills_response(
    peer: socket.socket,
    request_id: str,
    result: WorkspaceSkillsResult,
) -> None:
    """Send the exact bounded project-skill reconciliation contract."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "selected_skill_ids": list(result.selected_skill_ids),
                    "materialized": result.materialized,
                    "removed": result.removed,
                    "unchanged": result.unchanged,
                    "exclude_changed": result.exclude_changed,
                },
            }
        )
    )


def send_skill_cleanup_response(
    peer: socket.socket,
    request_id: str,
    result: SkillCleanupResult,
) -> None:
    """Send the exact bounded generated-skill cleanup contract."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_count": result.workspace_count,
                    "cleaned_workspace_count": result.cleaned_workspace_count,
                    "skipped_workspace_count": result.skipped_workspace_count,
                    "removed": result.removed,
                    "exclude_changed_count": result.exclude_changed_count,
                },
            }
        )
    )


def send_workspace_search_response(
    peer: socket.socket,
    request_id: str,
    result: WorkspaceSearchResult,
) -> None:
    """Send the exact success contract for one bounded Workspace search."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "project_id": result.project_id,
                    "workspace_root": str(result.workspace_root),
                    "results": [
                        {
                            "relative_path": hit.relative_path,
                            "kind": hit.kind.value,
                            "size_bytes": hit.size_bytes,
                            "match_kind": hit.match_kind.value,
                        }
                        for hit in result.results
                    ],
                },
            }
        )
    )


def send_project_search_response(
    peer: socket.socket,
    request_id: str,
    result: ProjectSearchResult,
) -> None:
    """Send the strict bounded Project Intelligence search response."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "project_id": result.project_id,
                    "results": [_project_search_hit_to_wire(hit) for hit in result.results],
                },
            }
        )
    )


def send_project_context_response(
    peer: socket.socket,
    request_id: str,
    result: ProjectContextResult,
) -> None:
    """Send the strict selective Project Intelligence context response."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "project_id": result.project_id,
                    "items": [
                        {"ref": item.ref, "kind": item.kind.value, "data": item.data}
                        for item in result.items
                    ],
                },
            }
        )
    )


def send_workspace_index_entry_response(
    peer: socket.socket,
    request_id: str,
    result: WorkspaceIndexEntryResult,
) -> None:
    """Send the exact success contract for one current Structural Index entry."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "project_id": result.project_id,
                    "workspace_root": str(result.workspace_root),
                    "relative_path": result.relative_path,
                    "kind": result.kind.value,
                    "size_bytes": result.size_bytes,
                },
            }
        )
    )


def send_task_start_response(
    peer: socket.socket,
    request_id: str,
    result: TaskStartResult,
) -> None:
    """Send the exact bounded success contract for task_start/resume."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "task_id": result.task_id,
                    "state": result.state.value,
                    "wait_reason": (
                        result.wait_reason.value if result.wait_reason is not None else None
                    ),
                    "revision": result.revision,
                },
            }
        )
    )


def send_task_checkpoint_response(
    peer: socket.socket,
    request_id: str,
    result: TaskCheckpointResult,
) -> None:
    """Send the exact bounded success contract for task_checkpoint."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": {
                    "schema_version": result.schema_version,
                    "workspace_id": result.workspace_id,
                    "task_id": result.task_id,
                    "state": result.state.value,
                    "wait_reason": (
                        result.wait_reason.value if result.wait_reason is not None else None
                    ),
                    "revision": result.revision,
                    "checkpoint_id": result.checkpoint_id,
                    "knowledge_ids": list(result.knowledge_ids),
                },
            }
        )
    )


def send_error_response(
    peer: socket.socket,
    *,
    request_id: str | None,
    code: str,
    message: str,
) -> None:
    """Send a bounded stable error without leaking source/context data."""
    peer.sendall(
        _encode_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": False,
                "error": {"code": code, "message": message},
            }
        )
    )


def _request_response(
    socket_path: Path,
    request: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    _require_posix_transport()
    payload = _encode_json(request)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(payload)
            return _decode_json(_receive_frame(client))
    except TimeoutError as exc:
        raise IpcTransportError("local IPC request timed out") from exc
    except OSError as exc:
        raise IpcTransportError(f"local IPC transport failed: {exc}") from exc


def _workspace_hints_to_wire(hints: Sequence[WorkspaceHint]) -> list[dict[str, str]]:
    if not 1 <= len(hints) <= _MAX_WORKSPACE_HINTS:
        raise IpcProtocolError("workspace status requires between 1 and 4 hints")
    wire_hints: list[dict[str, str]] = []
    for hint in hints:
        path = str(hint.path)
        _validate_hint_fields(path, hint.source, hint.match_mode)
        wire_hints.append(
            {
                "path": path,
                "source": hint.source,
                "match_mode": hint.match_mode.value,
            }
        )
    return wire_hints


def _workspace_hints_from_params(value: object) -> tuple[WorkspaceHint, ...]:
    if not isinstance(value, dict) or set(value) != {"hints"}:
        raise IpcProtocolError("workspace status params do not match the IPC schema")
    raw_hints = value["hints"]
    if not isinstance(raw_hints, list) or not 1 <= len(raw_hints) <= _MAX_WORKSPACE_HINTS:
        raise IpcProtocolError("workspace status requires between 1 and 4 hints")

    hints: list[WorkspaceHint] = []
    for raw_hint in raw_hints:
        if not isinstance(raw_hint, dict) or set(raw_hint) != {
            "path",
            "source",
            "match_mode",
        }:
            raise IpcProtocolError("workspace hint fields do not match the IPC schema")
        path = raw_hint["path"]
        source = raw_hint["source"]
        raw_mode = raw_hint["match_mode"]
        if (
            not isinstance(path, str)
            or not isinstance(source, str)
            or not isinstance(raw_mode, str)
        ):
            raise IpcProtocolError("workspace hint fields have invalid types")
        try:
            match_mode = WorkspaceHintMatchMode(raw_mode)
        except ValueError as exc:
            raise IpcProtocolError("workspace hint uses an unsupported match mode") from exc
        _validate_hint_fields(path, source, match_mode)
        hints.append(WorkspaceHint(path=Path(path), source=source, match_mode=match_mode))
    return tuple(hints)


def _host_profiles_to_wire(profiles: Sequence[str]) -> list[str]:
    return list(_validate_host_profiles(list(profiles)))


def _host_profiles_from_params(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict) or set(value) != {"profiles"}:
        raise IpcProtocolError("host profile params do not match the IPC schema")
    profiles = value["profiles"]
    if not isinstance(profiles, list):
        raise IpcProtocolError("host profiles must be a list")
    return _validate_host_profiles(profiles)


def _workspace_skills_from_params(
    value: object,
) -> tuple[tuple[WorkspaceHint, ...], tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {"hints", "profiles"}:
        raise IpcProtocolError("workspace skills params do not match the IPC schema")
    hints = _workspace_hints_from_params({"hints": value["hints"]})
    profiles = value["profiles"]
    if not isinstance(profiles, list):
        raise IpcProtocolError("host profiles must be a list")
    return hints, _validate_host_profiles(profiles)


def _validate_host_profiles(values: list[object]) -> tuple[str, ...]:
    if not 1 <= len(values) <= _HOST_PROFILE_MAX_ITEMS:
        raise IpcProtocolError("host profiles must contain between 1 and 8 items")
    profiles: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value.encode("utf-8")) > _HOST_PROFILE_MAX_BYTES
        ):
            raise IpcProtocolError("host profile must be bounded non-empty text")
        profiles.append(value)
    if len(set(profiles)) != len(profiles):
        raise IpcProtocolError("host profiles must be unique")
    return tuple(profiles)


def _workspace_search_from_params(
    value: object,
) -> tuple[tuple[WorkspaceHint, ...], str, int, IndexedPathSearchScope]:
    if not isinstance(value, dict) or set(value) not in (
        {"hints", "query", "limit"},
        {"hints", "query", "limit", "scope"},
    ):
        raise IpcProtocolError("workspace search params do not match the IPC schema")
    hints = _workspace_hints_from_params({"hints": value["hints"]})
    query = value["query"]
    limit = value["limit"]
    _validate_search_query(query)
    _validate_search_limit(limit)
    raw_scope = value.get("scope", IndexedPathSearchScope.ALL.value)
    if not isinstance(raw_scope, str):
        raise IpcProtocolError("workspace search scope must be text")
    try:
        scope = IndexedPathSearchScope(raw_scope)
    except ValueError as exc:
        raise IpcProtocolError("workspace search scope is unsupported") from exc
    return hints, cast(str, query), cast(int, limit), scope


def _project_search_from_params(
    value: object,
) -> tuple[tuple[WorkspaceHint, ...], str, int, ProjectSearchScope]:
    if not isinstance(value, dict) or set(value) != {"hints", "query", "limit", "scope"}:
        raise IpcProtocolError("project search params do not match the IPC schema")
    hints = _workspace_hints_from_params({"hints": value["hints"]})
    query = value["query"]
    limit = value["limit"]
    _validate_search_query(query)
    _validate_search_limit(limit)
    raw_scope = value["scope"]
    if not isinstance(raw_scope, str):
        raise IpcProtocolError("project search scope must be text")
    try:
        scope = ProjectSearchScope(raw_scope)
    except ValueError as exc:
        raise IpcProtocolError("project search scope is unsupported") from exc
    return hints, cast(str, query), cast(int, limit), scope


def _project_context_from_params(
    value: object,
) -> tuple[tuple[WorkspaceHint, ...], tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {"hints", "refs"}:
        raise IpcProtocolError("project context params do not match the IPC schema")
    hints = _workspace_hints_from_params({"hints": value["hints"]})
    refs = value["refs"]
    if not isinstance(refs, list):
        raise IpcProtocolError("project context refs must be a list")
    return hints, _validate_project_context_refs(refs)


def _workspace_index_entry_from_params(
    value: object,
) -> tuple[tuple[WorkspaceHint, ...], str]:
    if not isinstance(value, dict) or set(value) != {"hints", "relative_path"}:
        raise IpcProtocolError("workspace index entry params do not match the IPC schema")
    hints = _workspace_hints_from_params({"hints": value["hints"]})
    relative_path = value["relative_path"]
    _validate_index_relative_path(relative_path)
    return hints, cast(str, relative_path)


def _task_start_params_to_wire(
    hints: Sequence[WorkspaceHint],
    *,
    title: str | None,
    stack_hints: Sequence[str],
    task_id: str | None,
    expected_revision: int | None,
) -> dict[str, object]:
    wire_hints = _workspace_hints_to_wire(hints)
    normalized_stack_hints = _validate_task_stack_hints(stack_hints)
    if task_id is None:
        _validate_task_text(title, "task title", MAX_TASK_TITLE_BYTES, required=True)
        if expected_revision is not None:
            raise IpcProtocolError("new task_start must not include expected_revision")
        params: dict[str, object] = {"hints": wire_hints, "title": title}
        if normalized_stack_hints:
            params["stack_hints"] = list(normalized_stack_hints)
        return params
    _validate_task_id(task_id)
    if title is not None:
        raise IpcProtocolError("task_start resume must not include title")
    if normalized_stack_hints:
        raise IpcProtocolError("task_start resume must not include stack_hints")
    if expected_revision is not None:
        _validate_expected_revision(expected_revision)
        return {
            "hints": wire_hints,
            "task_id": task_id,
            "expected_revision": expected_revision,
        }
    return {"hints": wire_hints, "task_id": task_id}


def _task_start_from_params(value: object) -> TaskStartRequestData:
    if not isinstance(value, dict):
        raise IpcProtocolError("task_start params must be an object")
    fields = set(value)
    create_fields = {"hints", "title"}
    create_stack_fields = {"hints", "title", "stack_hints"}
    resume_fields = {"hints", "task_id"}
    resume_revision_fields = {"hints", "task_id", "expected_revision"}
    if fields == create_fields or fields == create_stack_fields:
        hints = _workspace_hints_from_params({"hints": value["hints"]})
        title = value["title"]
        _validate_task_text(title, "task title", MAX_TASK_TITLE_BYTES, required=True)
        stack_hints = _validate_task_stack_hints(value.get("stack_hints", []))
        return TaskStartRequestData(
            workspace_hints=hints,
            title=cast(str, title),
            stack_hints=stack_hints,
            task_id=None,
            expected_revision=None,
        )
    if fields == resume_fields or fields == resume_revision_fields:
        hints = _workspace_hints_from_params({"hints": value["hints"]})
        task_id = value["task_id"]
        _validate_task_id(task_id)
        expected_revision = value.get("expected_revision")
        if expected_revision is not None:
            _validate_expected_revision(expected_revision)
        return TaskStartRequestData(
            workspace_hints=hints,
            title=None,
            stack_hints=(),
            task_id=cast(str, task_id),
            expected_revision=cast(int | None, expected_revision),
        )
    raise IpcProtocolError("task_start params do not match the IPC schema")


def _validate_task_stack_hints(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise IpcProtocolError("task stack_hints must be an array")
    if len(value) > MAX_TASK_STACK_HINTS:
        raise IpcProtocolError(f"task stack_hints exceeds {MAX_TASK_STACK_HINTS} items")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise IpcProtocolError("task stack hint must be text")
        hint = item.strip().casefold()
        _validate_task_text(
            hint,
            "task stack hint",
            MAX_TASK_STACK_HINT_BYTES,
            required=True,
        )
        if hint in seen:
            raise IpcProtocolError("task stack_hints must not contain duplicates")
        seen.add(hint)
        normalized.append(hint)
    return tuple(normalized)


def _task_checkpoint_params_to_wire(
    hints: Sequence[WorkspaceHint],
    task_id: str,
    *,
    expected_revision: int,
    state: TaskState,
    summary: str,
    next_step: str | None,
    wait_reason: TaskWaitReason | None,
    knowledge: Sequence[KnowledgeDraft],
) -> dict[str, object]:
    _validate_task_id(task_id)
    _validate_expected_revision(expected_revision)
    if not isinstance(state, TaskState):
        raise IpcProtocolError("task checkpoint state must be a TaskState")
    if wait_reason is not None and not isinstance(wait_reason, TaskWaitReason):
        raise IpcProtocolError("task checkpoint wait_reason must be a TaskWaitReason")
    _validate_task_text(summary, "checkpoint summary", MAX_CHECKPOINT_SUMMARY_BYTES, required=True)
    _validate_task_text(
        next_step,
        "checkpoint next_step",
        MAX_CHECKPOINT_NEXT_STEP_BYTES,
        required=False,
    )
    return {
        "hints": _workspace_hints_to_wire(hints),
        "task_id": task_id,
        "expected_revision": expected_revision,
        "state": state.value,
        "summary": summary,
        "next_step": next_step,
        "wait_reason": wait_reason.value if wait_reason is not None else None,
        "knowledge": _knowledge_to_wire(knowledge),
    }


def _task_checkpoint_from_params(value: object) -> TaskCheckpointRequestData:
    expected_fields = {
        "hints",
        "task_id",
        "expected_revision",
        "state",
        "summary",
        "next_step",
        "wait_reason",
        "knowledge",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise IpcProtocolError("task_checkpoint params do not match the IPC schema")
    hints = _workspace_hints_from_params({"hints": value["hints"]})
    task_id = value["task_id"]
    expected_revision = value["expected_revision"]
    raw_state = value["state"]
    summary = value["summary"]
    next_step = value["next_step"]
    raw_wait_reason = value["wait_reason"]
    _validate_task_id(task_id)
    _validate_expected_revision(expected_revision)
    if not isinstance(raw_state, str):
        raise IpcProtocolError("task checkpoint state must be text")
    try:
        state = TaskState(raw_state)
    except ValueError as exc:
        raise IpcProtocolError("task checkpoint state is unsupported") from exc
    _validate_task_text(summary, "checkpoint summary", MAX_CHECKPOINT_SUMMARY_BYTES, required=True)
    _validate_task_text(
        next_step,
        "checkpoint next_step",
        MAX_CHECKPOINT_NEXT_STEP_BYTES,
        required=False,
    )
    if raw_wait_reason is None:
        wait_reason = None
    elif isinstance(raw_wait_reason, str):
        try:
            wait_reason = TaskWaitReason(raw_wait_reason)
        except ValueError as exc:
            raise IpcProtocolError("task checkpoint wait_reason is unsupported") from exc
    else:
        raise IpcProtocolError("task checkpoint wait_reason has invalid type")
    knowledge = _knowledge_from_wire(value["knowledge"])
    return TaskCheckpointRequestData(
        hints,
        cast(str, task_id),
        cast(int, expected_revision),
        state,
        cast(str, summary),
        cast(str | None, next_step),
        wait_reason,
        knowledge,
    )


def _knowledge_to_wire(knowledge: Sequence[KnowledgeDraft]) -> list[dict[str, object]]:
    if len(knowledge) > MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT:
        raise IpcProtocolError("task checkpoint knowledge exceeds card limit")
    result: list[dict[str, object]] = []
    for card in knowledge:
        if not isinstance(card, KnowledgeDraft) or not isinstance(card.kind, KnowledgeKind):
            raise IpcProtocolError("task checkpoint knowledge item has invalid type")
        if len(card.anchors) > MAX_KNOWLEDGE_ANCHORS_PER_CARD:
            raise IpcProtocolError("task checkpoint knowledge card exceeds anchor limit")
        wire_anchors: list[dict[str, object]] = []
        for anchor in card.anchors:
            if not isinstance(anchor, KnowledgeAnchorDraft):
                raise IpcProtocolError("task checkpoint knowledge anchor has invalid type")
            wire_anchors.append({"path": anchor.path, "symbol": anchor.symbol})
        result.append(
            {
                "kind": card.kind.value,
                "title": card.title,
                "body": card.body,
                "anchors": wire_anchors,
            }
        )
    return result


def _knowledge_from_wire(value: object) -> tuple[KnowledgeDraft, ...]:
    if not isinstance(value, list) or len(value) > MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT:
        raise IpcProtocolError("task checkpoint knowledge must be a bounded list")
    cards: list[KnowledgeDraft] = []
    for raw_card in value:
        if not isinstance(raw_card, dict) or set(raw_card) != {
            "kind",
            "title",
            "body",
            "anchors",
        }:
            raise IpcProtocolError("task checkpoint knowledge fields do not match the IPC schema")
        raw_kind = raw_card["kind"]
        if not isinstance(raw_kind, str):
            raise IpcProtocolError("task checkpoint knowledge kind has invalid type")
        try:
            kind = KnowledgeKind(raw_kind)
        except ValueError as exc:
            raise IpcProtocolError("task checkpoint knowledge kind is unsupported") from exc
        title = raw_card["title"]
        body = raw_card["body"]
        if not isinstance(title, str) or not isinstance(body, str):
            raise IpcProtocolError("task checkpoint knowledge text has invalid type")
        raw_anchors = raw_card["anchors"]
        if not isinstance(raw_anchors, list) or len(raw_anchors) > MAX_KNOWLEDGE_ANCHORS_PER_CARD:
            raise IpcProtocolError("task checkpoint knowledge anchors must be a bounded list")
        anchors: list[KnowledgeAnchorDraft] = []
        for raw_anchor in raw_anchors:
            if not isinstance(raw_anchor, dict) or set(raw_anchor) != {"path", "symbol"}:
                raise IpcProtocolError(
                    "task checkpoint knowledge anchor fields do not match the IPC schema"
                )
            path = raw_anchor["path"]
            symbol = raw_anchor["symbol"]
            if not isinstance(path, str) or (symbol is not None and not isinstance(symbol, str)):
                raise IpcProtocolError("task checkpoint knowledge anchor has invalid types")
            anchors.append(KnowledgeAnchorDraft(path=path, symbol=symbol))
        cards.append(KnowledgeDraft(kind=kind, title=title, body=body, anchors=tuple(anchors)))
    return tuple(cards)


def _validate_task_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _TASK_ID_MAX_LENGTH
        or "\x00" in value
    ):
        raise IpcProtocolError("task_id must be a non-empty bounded string")


def _validate_expected_revision(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IpcProtocolError("expected_revision must be a positive integer")


def _validate_task_text(
    value: object,
    label: str,
    maximum_bytes: int,
    *,
    required: bool,
) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise IpcProtocolError(f"{label} must be non-empty text")
    try:
        size = len(value.strip().encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise IpcProtocolError(f"{label} must be valid UTF-8 text") from exc
    if size > maximum_bytes:
        raise IpcProtocolError(f"{label} exceeds {maximum_bytes} UTF-8 bytes")


def _scan_path_from_params(value: object) -> Path:
    if not isinstance(value, dict) or set(value) != {"path"}:
        raise IpcProtocolError("workspace scan params do not match the IPC schema")
    path = value["path"]
    if not isinstance(path, str):
        raise IpcProtocolError("workspace scan path has invalid type")
    _validate_scan_path(path)
    return Path(path)


def _validate_scan_path(path: str) -> None:
    if not path or len(path) > _HINT_PATH_MAX_LENGTH or "\x00" in path:
        raise IpcProtocolError("workspace scan path must be a non-empty bounded path")
    if not Path(path).is_absolute():
        raise IpcProtocolError("workspace scan path must be absolute")


def _validate_search_query(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise IpcProtocolError("workspace search query must be a non-empty bounded string")
    try:
        size = len(value.strip().encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise IpcProtocolError("workspace search query must be valid UTF-8 text") from exc
    if size > MAX_SEARCH_QUERY_BYTES:
        raise IpcProtocolError(
            f"workspace search query exceeds {MAX_SEARCH_QUERY_BYTES} UTF-8 bytes"
        )


def _validate_search_limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SEARCH_LIMIT:
        raise IpcProtocolError(
            f"workspace search limit must be an integer between 1 and {MAX_SEARCH_LIMIT}"
        )


def _validate_index_relative_path(value: object) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise IpcProtocolError("workspace index relative_path must be non-empty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise IpcProtocolError("workspace index relative_path must be valid UTF-8 text") from exc
    path = Path(value)
    if (
        size > _INDEX_RELATIVE_PATH_MAX_BYTES
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
    ):
        raise IpcProtocolError("workspace index relative_path is unsafe or too large")


def _validate_hint_fields(
    path: str,
    source: str,
    match_mode: WorkspaceHintMatchMode,
) -> None:
    if not path or len(path) > _HINT_PATH_MAX_LENGTH or "\x00" in path:
        raise IpcProtocolError("workspace hint path must be a non-empty bounded path")
    if not Path(path).is_absolute():
        raise IpcProtocolError("workspace hint path must be absolute")
    if not source or len(source) > _HINT_SOURCE_MAX_LENGTH or "\x00" in source:
        raise IpcProtocolError("workspace hint source must be a non-empty bounded string")
    if not isinstance(match_mode, WorkspaceHintMatchMode):
        raise IpcProtocolError("workspace hint uses an unsupported match mode")


def _receive_frame(peer: socket.socket) -> bytes:
    data = bytearray()
    original_timeout = peer.gettimeout()
    deadline = (
        monotonic() + original_timeout
        if original_timeout is not None and original_timeout > 0
        else None
    )
    try:
        while True:
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("IPC receive deadline exceeded")
                peer.settimeout(remaining)

            chunk = peer.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(data)))
            if not chunk:
                raise IpcProtocolError("IPC peer closed before terminating the message")
            newline_index = chunk.find(b"\n")
            if newline_index >= 0:
                data.extend(chunk[:newline_index])
                if len(data) > MAX_MESSAGE_BYTES:
                    raise IpcMessageTooLargeError("IPC message exceeds the byte limit")
                if chunk[newline_index + 1 :]:
                    raise IpcProtocolError("multiple IPC messages per connection are not supported")
                return bytes(data)
            data.extend(chunk)
            if len(data) > MAX_MESSAGE_BYTES:
                raise IpcMessageTooLargeError("IPC message exceeds the byte limit")
    finally:
        if deadline is not None:
            peer.settimeout(original_timeout)


def _decode_json(payload: bytes) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcProtocolError("IPC message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise IpcProtocolError("IPC message must be a JSON object")
    return cast(dict[str, Any], value)


def _encode_json(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    if len(payload) > MAX_MESSAGE_BYTES:
        raise IpcMessageTooLargeError("IPC response exceeds the byte limit")
    return payload + b"\n"


def _status_from_response(response: dict[str, Any], *, expected_request_id: str) -> StatusResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {"schema_version", "project_count", "workspace_count"}:
        raise IpcProtocolError("daemon status result does not match the IPC schema")
    values = (result["schema_version"], result["project_count"], result["workspace_count"])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise IpcProtocolError("daemon status result has invalid field types")
    return StatusResult(
        schema_version=result["schema_version"],
        project_count=result["project_count"],
        workspace_count=result["workspace_count"],
    )


def _runtime_diagnostics_from_response(
    response: dict[str, Any], *, expected_request_id: str
) -> RuntimeDiagnosticsResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected = {
        "schema_version",
        "package_version",
        "python_executable",
        "code_sha256",
        "project_count",
        "workspace_count",
        "dashboard_running",
    }
    if set(result) != expected:
        raise IpcProtocolError("daemon runtime diagnostics result does not match the IPC schema")
    counts = (result["schema_version"], result["project_count"], result["workspace_count"])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise IpcProtocolError("daemon runtime diagnostics result has invalid counts")
    package_version = _bounded_response_string(result["package_version"], "package_version", 128)
    python_executable = _bounded_response_string(
        result["python_executable"], "python_executable", 4096
    )
    code_sha256 = _bounded_response_string(result["code_sha256"], "code_sha256", 64)
    if len(code_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in code_sha256
    ):
        raise IpcProtocolError("daemon runtime diagnostics has invalid code fingerprint")
    dashboard_running = result["dashboard_running"]
    if not isinstance(dashboard_running, bool):
        raise IpcProtocolError("daemon runtime diagnostics has invalid dashboard state")
    return RuntimeDiagnosticsResult(
        schema_version=result["schema_version"],
        package_version=package_version,
        python_executable=python_executable,
        code_sha256=code_sha256,
        project_count=result["project_count"],
        workspace_count=result["workspace_count"],
        dashboard_running=dashboard_running,
    )


def _dashboard_url_from_response(
    response: dict[str, Any], *, expected_request_id: str
) -> DashboardUrlResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {"url"}:
        raise IpcProtocolError("daemon dashboard URL result does not match the IPC schema")
    url = result["url"]
    if not isinstance(url, str) or len(url) > 512:
        raise IpcProtocolError("daemon dashboard URL result has invalid field types")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise IpcProtocolError("daemon dashboard URL has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or not parsed.path.endswith("/")
    ):
        raise IpcProtocolError("daemon dashboard URL is not a private loopback capability URL")
    return DashboardUrlResult(url=url)


def _shutdown_from_response(
    response: dict[str, Any], *, expected_request_id: str
) -> ShutdownResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {"accepted"} or result["accepted"] is not True:
        raise IpcProtocolError("daemon shutdown result does not match the IPC schema")
    return ShutdownResult(accepted=True)


def _workspace_skills_from_response(
    response: dict[str, Any], *, expected_request_id: str
) -> WorkspaceSkillsResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected = {
        "schema_version",
        "workspace_id",
        "selected_skill_ids",
        "materialized",
        "removed",
        "unchanged",
        "exclude_changed",
    }
    if set(result) != expected:
        raise IpcProtocolError("daemon workspace skills result does not match the IPC schema")
    counts = (
        result["schema_version"],
        result["materialized"],
        result["removed"],
        result["unchanged"],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise IpcProtocolError("daemon workspace skills result has invalid counts")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    raw_ids = result["selected_skill_ids"]
    if not isinstance(raw_ids, list) or len(raw_ids) > 64:
        raise IpcProtocolError("daemon workspace skills result has invalid selected skills")
    selected = tuple(_bounded_response_string(value, "skill_id", 128) for value in raw_ids)
    if len(set(selected)) != len(selected):
        raise IpcProtocolError("daemon workspace skills result contains duplicate skill ids")
    exclude_changed = result["exclude_changed"]
    if not isinstance(exclude_changed, bool):
        raise IpcProtocolError("daemon workspace skills result has invalid exclude_changed")
    return WorkspaceSkillsResult(
        schema_version=result["schema_version"],
        workspace_id=workspace_id,
        selected_skill_ids=selected,
        materialized=result["materialized"],
        removed=result["removed"],
        unchanged=result["unchanged"],
        exclude_changed=exclude_changed,
    )


def _skill_cleanup_from_response(
    response: dict[str, Any], *, expected_request_id: str
) -> SkillCleanupResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected = {
        "schema_version",
        "workspace_count",
        "cleaned_workspace_count",
        "skipped_workspace_count",
        "removed",
        "exclude_changed_count",
    }
    if set(result) != expected:
        raise IpcProtocolError("daemon skill cleanup result does not match the IPC schema")
    values = tuple(result[field] for field in expected)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise IpcProtocolError("daemon skill cleanup result has invalid counts")
    if (
        result["cleaned_workspace_count"] + result["skipped_workspace_count"]
        != result["workspace_count"]
    ):
        raise IpcProtocolError("daemon skill cleanup workspace counts are inconsistent")
    return SkillCleanupResult(
        schema_version=result["schema_version"],
        workspace_count=result["workspace_count"],
        cleaned_workspace_count=result["cleaned_workspace_count"],
        skipped_workspace_count=result["skipped_workspace_count"],
        removed=result["removed"],
        exclude_changed_count=result["exclude_changed_count"],
    )


def _workspace_status_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceStatusResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "project_id",
        "visibility_mode",
        "workspace_root",
        "head",
        "branch",
        "dirty_path_count",
        "indexed_file_count",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon workspace status result does not match the IPC schema")

    schema_version = result["schema_version"]
    dirty_path_count = result["dirty_path_count"]
    indexed_file_count = result["indexed_file_count"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (schema_version, dirty_path_count, indexed_file_count)
    ):
        raise IpcProtocolError("daemon workspace status counts have invalid field types")

    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_response_string(result["project_id"], "project_id", 128)
    visibility_mode = _bounded_response_string(result["visibility_mode"], "visibility_mode", 16)
    if visibility_mode not in {"normal", "hidden"}:
        raise IpcProtocolError("daemon workspace status has unsupported visibility mode")
    workspace_root_value = _bounded_response_string(
        result["workspace_root"],
        "workspace_root",
        _HINT_PATH_MAX_LENGTH,
    )
    workspace_root = Path(workspace_root_value)
    if not workspace_root.is_absolute():
        raise IpcProtocolError("daemon workspace status root must be absolute")

    head = result["head"]
    if head is not None:
        head = _bounded_response_string(head, "head", 64)
        if len(head) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in head
        ):
            raise IpcProtocolError("daemon workspace status has invalid HEAD identity")
    branch = result["branch"]
    if branch is not None:
        branch = _bounded_response_string(branch, "branch", MAX_MESSAGE_BYTES)

    return WorkspaceStatusResult(
        schema_version=schema_version,
        workspace_id=workspace_id,
        project_id=project_id,
        visibility_mode=visibility_mode,
        workspace_root=workspace_root,
        head=head,
        branch=branch,
        dirty_path_count=dirty_path_count,
        indexed_file_count=indexed_file_count,
    )


def _workspace_task_status_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceTaskStatusResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {
        "schema_version",
        "workspace_id",
        "task",
        "last_checkpoint",
        "pending_operator_feedback",
    }:
        raise IpcProtocolError("daemon workspace task status result does not match the IPC schema")
    schema_version = result["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version <= 0
    ):
        raise IpcProtocolError("daemon workspace task status schema version has invalid type")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)

    raw_task = result["task"]
    task: WorkspaceTaskSummary | None
    if raw_task is None:
        task = None
    else:
        if not isinstance(raw_task, dict) or set(raw_task) != {
            "task_id",
            "title",
            "state",
            "wait_reason",
            "revision",
        }:
            raise IpcProtocolError(
                "daemon workspace task status task does not match the IPC schema"
            )
        task_id = _bounded_response_string(raw_task["task_id"], "task_id", _TASK_ID_MAX_LENGTH)
        title = _bounded_response_string(raw_task["title"], "title", MAX_TASK_TITLE_BYTES)
        revision = raw_task["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise IpcProtocolError("daemon workspace task status task has invalid revision")
        state, wait_reason = _task_state_from_response(raw_task["state"], raw_task["wait_reason"])
        if state not in {TaskState.WORKING, TaskState.WAITING}:
            raise IpcProtocolError(
                "daemon workspace task status returned an irrelevant terminal Task"
            )
        task = WorkspaceTaskSummary(task_id, title, state, wait_reason, revision)

    raw_checkpoint = result["last_checkpoint"]
    checkpoint: WorkspaceTaskCheckpointSummary | None
    if raw_checkpoint is None:
        checkpoint = None
    else:
        if task is None:
            raise IpcProtocolError("daemon workspace task status checkpoint has no Task")
        if not isinstance(raw_checkpoint, dict) or set(raw_checkpoint) != {
            "checkpoint_id",
            "task_revision",
            "state",
            "wait_reason",
            "next_step",
        }:
            raise IpcProtocolError(
                "daemon workspace task status checkpoint does not match the IPC schema"
            )
        checkpoint_id = _bounded_response_string(
            raw_checkpoint["checkpoint_id"], "checkpoint_id", 128
        )
        task_revision = raw_checkpoint["task_revision"]
        if (
            isinstance(task_revision, bool)
            or not isinstance(task_revision, int)
            or task_revision <= 0
            or task_revision > task.revision
        ):
            raise IpcProtocolError("daemon workspace task status checkpoint has invalid revision")
        state, wait_reason = _task_state_from_response(
            raw_checkpoint["state"], raw_checkpoint["wait_reason"]
        )
        next_step = raw_checkpoint["next_step"]
        if next_step is not None:
            next_step = _bounded_response_string(
                next_step, "next_step", MAX_CHECKPOINT_NEXT_STEP_BYTES
            )
        checkpoint = WorkspaceTaskCheckpointSummary(
            checkpoint_id,
            task_revision,
            state,
            wait_reason,
            cast(str | None, next_step),
        )
    pending_operator_feedback = result["pending_operator_feedback"]
    if pending_operator_feedback is not None:
        if task is None or task.state is not TaskState.WORKING:
            raise IpcProtocolError(
                "daemon workspace task status feedback requires a current working Task"
            )
        pending_operator_feedback = _bounded_response_string(
            pending_operator_feedback,
            "pending_operator_feedback",
            MAX_OPERATOR_FEEDBACK_BYTES,
        )
    return WorkspaceTaskStatusResult(
        schema_version,
        workspace_id,
        task,
        checkpoint,
        cast(str | None, pending_operator_feedback),
    )


def _workspace_scan_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceScanResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "project_id",
        "visibility_mode",
        "workspace_root",
        "project_created",
        "workspace_created",
        "file_count",
        "added",
        "updated",
        "removed",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon workspace scan result does not match the IPC schema")

    counts = (
        result["schema_version"],
        result["file_count"],
        result["added"],
        result["updated"],
        result["removed"],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise IpcProtocolError("daemon workspace scan counts have invalid field types")
    project_created = result["project_created"]
    workspace_created = result["workspace_created"]
    if not isinstance(project_created, bool) or not isinstance(workspace_created, bool):
        raise IpcProtocolError("daemon workspace scan registration flags have invalid field types")

    workspace_id = _bounded_scan_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_scan_response_string(result["project_id"], "project_id", 128)
    visibility_mode = _bounded_scan_response_string(
        result["visibility_mode"], "visibility_mode", 16
    )
    if visibility_mode not in {"normal", "hidden"}:
        raise IpcProtocolError("daemon workspace scan has unsupported visibility mode")
    workspace_root_value = _bounded_scan_response_string(
        result["workspace_root"],
        "workspace_root",
        _HINT_PATH_MAX_LENGTH,
    )
    workspace_root = Path(workspace_root_value)
    if not workspace_root.is_absolute():
        raise IpcProtocolError("daemon workspace scan root must be absolute")

    return WorkspaceScanResult(
        schema_version=counts[0],
        workspace_id=workspace_id,
        project_id=project_id,
        visibility_mode=visibility_mode,
        workspace_root=workspace_root,
        project_created=project_created,
        workspace_created=workspace_created,
        file_count=counts[1],
        added=counts[2],
        updated=counts[3],
        removed=counts[4],
    )


def _workspace_search_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceSearchResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "project_id",
        "workspace_root",
        "results",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon workspace search result does not match the IPC schema")

    schema_version = result["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 0
    ):
        raise IpcProtocolError("daemon workspace search schema version has invalid type")
    workspace_id = _bounded_search_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_search_response_string(result["project_id"], "project_id", 128)
    workspace_root_value = _bounded_search_response_string(
        result["workspace_root"],
        "workspace_root",
        _HINT_PATH_MAX_LENGTH,
    )
    workspace_root = Path(workspace_root_value)
    if not workspace_root.is_absolute():
        raise IpcProtocolError("daemon workspace search root must be absolute")

    raw_results = result["results"]
    if not isinstance(raw_results, list) or len(raw_results) > MAX_SEARCH_LIMIT:
        raise IpcProtocolError("daemon workspace search results exceed the item limit")
    hits: list[WorkspaceSearchHit] = []
    for raw_hit in raw_results:
        if not isinstance(raw_hit, dict) or set(raw_hit) != {
            "relative_path",
            "kind",
            "size_bytes",
            "match_kind",
        }:
            raise IpcProtocolError("daemon workspace search hit does not match the IPC schema")
        relative_path = _bounded_search_response_string(
            raw_hit["relative_path"],
            "relative_path",
            MAX_MESSAGE_BYTES,
        )
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or "\x00" in relative_path:
            raise IpcProtocolError("daemon workspace search hit has unsafe relative_path")
        try:
            kind = IndexedFileKind(_bounded_search_response_string(raw_hit["kind"], "kind", 16))
        except ValueError as exc:
            raise IpcProtocolError("daemon workspace search hit has unsupported kind") from exc
        size_bytes = raw_hit["size_bytes"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise IpcProtocolError("daemon workspace search hit has invalid size_bytes")
        try:
            match_kind = SearchMatchKind(
                _bounded_search_response_string(raw_hit["match_kind"], "match_kind", 32)
            )
        except ValueError as exc:
            raise IpcProtocolError(
                "daemon workspace search hit has unsupported match_kind"
            ) from exc
        hits.append(
            WorkspaceSearchHit(
                relative_path=relative_path,
                kind=kind,
                size_bytes=size_bytes,
                match_kind=match_kind,
            )
        )

    return WorkspaceSearchResult(
        schema_version=schema_version,
        workspace_id=workspace_id,
        project_id=project_id,
        workspace_root=workspace_root,
        results=tuple(hits),
    )


def _project_search_hit_to_wire(hit: ProjectSearchHit) -> dict[str, object]:
    return {
        "ref": hit.ref,
        "kind": hit.kind.value,
        "title": hit.title,
        "location": hit.location,
        "short_summary": hit.short_summary,
        "match_reason": hit.match_reason,
        "freshness": hit.freshness,
        "path": hit.path,
    }


def _project_search_hit_from_wire(value: object) -> ProjectSearchHit:
    fields = {
        "ref",
        "kind",
        "title",
        "location",
        "short_summary",
        "match_reason",
        "freshness",
        "path",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise IpcProtocolError("daemon project search hit does not match the IPC schema")
    ref = _bounded_response_string(value["ref"], "ref", _PROJECT_CONTEXT_REF_MAX_BYTES)
    try:
        kind = ProjectSearchKind(_bounded_response_string(value["kind"], "kind", 16))
    except ValueError as exc:
        raise IpcProtocolError("daemon project search hit has unsupported kind") from exc
    title = _bounded_response_string(value["title"], "title", 512)
    location = _bounded_response_string(value["location"], "location", 4096)
    reason = _bounded_response_string(value["match_reason"], "match_reason", 128)
    freshness = _bounded_response_string(value["freshness"], "freshness", 64)
    summary = value["short_summary"]
    if summary is not None:
        summary = _bounded_response_string(summary, "short_summary", 1024)
    path = value["path"]
    if path is not None:
        path = _bounded_response_string(path, "path", _INDEX_RELATIVE_PATH_MAX_BYTES)
    return ProjectSearchHit(
        ref,
        kind,
        title,
        location,
        cast(str | None, summary),
        reason,
        freshness,
        cast(str | None, path),
    )


def _validate_project_context_refs(refs: Sequence[object]) -> tuple[str, ...]:
    if not 1 <= len(refs) <= _PROJECT_CONTEXT_MAX_REFS:
        raise IpcProtocolError(
            f"project context refs must contain between 1 and {_PROJECT_CONTEXT_MAX_REFS} items"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref or "\x00" in ref:
            raise IpcProtocolError("project context ref must be non-empty text")
        if len(ref.encode("utf-8")) > _PROJECT_CONTEXT_REF_MAX_BYTES:
            raise IpcProtocolError("project context ref exceeds byte limit")
        if ref in seen:
            raise IpcProtocolError("project context refs must be unique")
        seen.add(ref)
        normalized.append(ref)
    return tuple(normalized)


def _bounded_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IpcProtocolError(f"daemon project retrieval {label} has invalid type")
    return value


def _validate_json_value(value: object, *, depth: int) -> None:
    if depth > 5:
        raise IpcProtocolError("daemon project context data nesting is too deep")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, list):
        if len(value) > 32:
            raise IpcProtocolError("daemon project context data list exceeds item limit")
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 24 or any(not isinstance(key, str) or len(key) > 64 for key in value):
            raise IpcProtocolError("daemon project context data object is invalid")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise IpcProtocolError("daemon project context data contains unsupported value")


def _project_search_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> ProjectSearchResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {"schema_version", "workspace_id", "project_id", "results"}:
        raise IpcProtocolError("daemon project search result does not match the IPC schema")
    schema_version = _bounded_nonnegative_int(result["schema_version"], "schema_version")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_response_string(result["project_id"], "project_id", 128)
    raw_results = result["results"]
    if not isinstance(raw_results, list) or len(raw_results) > MAX_SEARCH_LIMIT:
        raise IpcProtocolError("daemon project search results exceed the item limit")
    hits = tuple(_project_search_hit_from_wire(raw) for raw in raw_results)
    return ProjectSearchResult(schema_version, workspace_id, project_id, hits)


def _project_context_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> ProjectContextResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    if set(result) != {"schema_version", "workspace_id", "project_id", "items"}:
        raise IpcProtocolError("daemon project context result does not match the IPC schema")
    schema_version = _bounded_nonnegative_int(result["schema_version"], "schema_version")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_response_string(result["project_id"], "project_id", 128)
    raw_items = result["items"]
    if not isinstance(raw_items, list) or len(raw_items) > _PROJECT_CONTEXT_MAX_REFS:
        raise IpcProtocolError("daemon project context items exceed the item limit")
    items: list[ProjectContextItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {"ref", "kind", "data"}:
            raise IpcProtocolError("daemon project context item does not match the IPC schema")
        ref = _bounded_response_string(raw["ref"], "ref", _PROJECT_CONTEXT_REF_MAX_BYTES)
        try:
            kind = ProjectSearchKind(_bounded_response_string(raw["kind"], "kind", 16))
        except ValueError as exc:
            raise IpcProtocolError("daemon project context item has unsupported kind") from exc
        data = raw["data"]
        _validate_json_value(data, depth=0)
        assert isinstance(data, dict)
        items.append(ProjectContextItem(ref=ref, kind=kind, data=cast(dict[str, object], data)))
    return ProjectContextResult(schema_version, workspace_id, project_id, tuple(items))


def _workspace_index_entry_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> WorkspaceIndexEntryResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "project_id",
        "workspace_root",
        "relative_path",
        "kind",
        "size_bytes",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon workspace index entry does not match the IPC schema")

    schema_version = result["schema_version"]
    size_bytes = result["size_bytes"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (schema_version, size_bytes)
    ):
        raise IpcProtocolError("daemon workspace index entry has invalid integer fields")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    project_id = _bounded_response_string(result["project_id"], "project_id", 128)
    workspace_root_value = _bounded_response_string(
        result["workspace_root"], "workspace_root", _HINT_PATH_MAX_LENGTH
    )
    workspace_root = Path(workspace_root_value)
    if not workspace_root.is_absolute():
        raise IpcProtocolError("daemon workspace index entry root must be absolute")
    relative_path = _bounded_response_string(
        result["relative_path"], "relative_path", _INDEX_RELATIVE_PATH_MAX_BYTES
    )
    _validate_index_relative_path(relative_path)
    try:
        kind = IndexedFileKind(_bounded_response_string(result["kind"], "kind", 16))
    except ValueError as exc:
        raise IpcProtocolError("daemon workspace index entry has unsupported kind") from exc
    return WorkspaceIndexEntryResult(
        schema_version=schema_version,
        workspace_id=workspace_id,
        project_id=project_id,
        workspace_root=workspace_root,
        relative_path=relative_path,
        kind=kind,
        size_bytes=size_bytes,
    )


def _task_start_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> TaskStartResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "task_id",
        "state",
        "wait_reason",
        "revision",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon task_start result does not match the IPC schema")
    schema_version = result["schema_version"]
    revision = result["revision"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (schema_version, revision)
    ):
        raise IpcProtocolError("daemon task_start result has invalid integer fields")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    task_id = _bounded_response_string(result["task_id"], "task_id", _TASK_ID_MAX_LENGTH)
    state, wait_reason = _task_state_from_response(result["state"], result["wait_reason"])
    return TaskStartResult(schema_version, workspace_id, task_id, state, wait_reason, revision)


def _task_checkpoint_from_response(
    response: dict[str, Any],
    *,
    expected_request_id: str,
) -> TaskCheckpointResult:
    result = _success_result(response, expected_request_id=expected_request_id)
    expected_fields = {
        "schema_version",
        "workspace_id",
        "task_id",
        "state",
        "wait_reason",
        "revision",
        "checkpoint_id",
        "knowledge_ids",
    }
    if set(result) != expected_fields:
        raise IpcProtocolError("daemon task_checkpoint result does not match the IPC schema")
    schema_version = result["schema_version"]
    revision = result["revision"]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (schema_version, revision)
    ):
        raise IpcProtocolError("daemon task_checkpoint result has invalid integer fields")
    workspace_id = _bounded_response_string(result["workspace_id"], "workspace_id", 128)
    task_id = _bounded_response_string(result["task_id"], "task_id", _TASK_ID_MAX_LENGTH)
    checkpoint_id = _bounded_response_string(result["checkpoint_id"], "checkpoint_id", 128)
    state, wait_reason = _task_state_from_response(result["state"], result["wait_reason"])
    raw_knowledge_ids = result["knowledge_ids"]
    if (
        not isinstance(raw_knowledge_ids, list)
        or len(raw_knowledge_ids) > MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT
    ):
        raise IpcProtocolError("daemon task_checkpoint knowledge_ids are invalid")
    knowledge_ids = tuple(
        _bounded_response_string(value, "knowledge_id", 128) for value in raw_knowledge_ids
    )
    return TaskCheckpointResult(
        schema_version,
        workspace_id,
        task_id,
        state,
        wait_reason,
        revision,
        checkpoint_id,
        knowledge_ids,
    )


def _task_state_from_response(
    raw_state: object,
    raw_wait_reason: object,
) -> tuple[TaskState, TaskWaitReason | None]:
    if not isinstance(raw_state, str):
        raise IpcProtocolError("daemon Task state has invalid type")
    try:
        state = TaskState(raw_state)
    except ValueError as exc:
        raise IpcProtocolError("daemon Task state is unsupported") from exc
    if raw_wait_reason is None:
        return state, None
    if not isinstance(raw_wait_reason, str):
        raise IpcProtocolError("daemon Task wait_reason has invalid type")
    try:
        return state, TaskWaitReason(raw_wait_reason)
    except ValueError as exc:
        raise IpcProtocolError("daemon Task wait_reason is unsupported") from exc


def _success_result(response: dict[str, Any], *, expected_request_id: str) -> dict[str, Any]:
    version = response.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != PROTOCOL_VERSION:
        raise IpcProtocolError("daemon response uses an unsupported IPC protocol version")
    if response.get("request_id") != expected_request_id:
        raise IpcProtocolError("daemon response request_id does not match the request")
    ok = response.get("ok")
    if ok is False:
        if set(response) != {"version", "request_id", "ok", "error"}:
            raise IpcProtocolError("daemon error fields do not match the IPC schema")
        error = response["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise IpcProtocolError("daemon error payload does not match the IPC schema")
        code = error["code"]
        message = error["message"]
        if not isinstance(code, str) or not isinstance(message, str):
            raise IpcProtocolError("daemon error payload has invalid field types")
        raise IpcRemoteError(code, message)
    if ok is not True or set(response) != {"version", "request_id", "ok", "result"}:
        raise IpcProtocolError("daemon response fields do not match the IPC schema")

    result = response["result"]
    if not isinstance(result, dict):
        raise IpcProtocolError("daemon result payload must be an object")
    return cast(dict[str, Any], result)


def _bounded_response_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise IpcProtocolError(f"daemon workspace status has invalid {field}")
    return value


def _bounded_scan_response_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise IpcProtocolError(f"daemon workspace scan has invalid {field}")
    return value


def _bounded_search_response_string(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise IpcProtocolError(f"daemon workspace search has invalid {field}")
    return value


def _require_posix_transport() -> None:
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        raise UnsupportedIpcTransportError(
            "the Windows local-user IPC transport is not implemented in this bounded slice"
        )
