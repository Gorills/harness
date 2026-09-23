from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

import harness.mcp_bridge as bridge
from harness.ipc import (
    PROTOCOL_VERSION,
    IpcProtocolError,
    WorkspaceStatusResult,
    WorkspaceTaskStatusResult,
    WorkspaceTaskSummary,
    _workspace_task_status_from_response,
)
from harness.tasks import TaskState


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["head", "branch"])
async def test_mcp_status_rejects_git_change_between_private_ipc_responses(
    monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    status = WorkspaceStatusResult(
        schema_version=24,
        workspace_id="workspace",
        project_id="project",
        visibility_mode="normal",
        workspace_root=Path("/repo"),
        head="a" * 40,
        branch="main",
        dirty_path_count=0,
        indexed_file_count=0,
        index_revision=None,
        last_successful_reconcile_at=None,
        last_reconcile_kind=None,
    )
    task_status = WorkspaceTaskStatusResult(
        schema_version=24,
        workspace_id="workspace",
        task=WorkspaceTaskSummary(
            "secret-task", "Unmerged private task", TaskState.WORKING, None, 1
        ),
        last_checkpoint=None,
        pending_operator_feedback=None,
        head=status.head,
        branch=status.branch,
    )
    task_status = (
        replace(task_status, head="b" * 40)
        if changed == "head"
        else replace(task_status, branch="feature")
    )
    monkeypatch.setattr(bridge, "_mcp_tool_refusal", lambda: None)
    monkeypatch.setattr(bridge, "_workspace_hints", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(bridge, "_socket_path", lambda: Path("/unused"))
    monkeypatch.setattr(bridge, "request_workspace_status", lambda *_args: status)
    monkeypatch.setattr(bridge, "request_workspace_task_status", lambda *_args: task_status)
    with pytest.raises(ToolError, match="Workspace changed") as error:
        await bridge.build_mcp_server().call_tool("project_status", {})
    rendered = str(error.value)
    assert "secret-task" not in rendered
    assert "Unmerged private task" not in rendered
    assert "feature" not in rendered


@pytest.mark.anyio
async def test_private_task_status_git_snapshot_is_not_added_to_model_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = WorkspaceStatusResult(
        schema_version=24,
        workspace_id="workspace",
        project_id="project",
        visibility_mode="normal",
        workspace_root=Path("/repo"),
        head=None,
        branch=None,
        dirty_path_count=0,
        indexed_file_count=0,
        index_revision=None,
        last_successful_reconcile_at=None,
        last_reconcile_kind=None,
    )
    task_status = WorkspaceTaskStatusResult(24, "workspace", None, None, None, None, None)
    monkeypatch.setattr(bridge, "_mcp_tool_refusal", lambda: None)
    monkeypatch.setattr(bridge, "_workspace_hints", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(bridge, "_socket_path", lambda: Path("/unused"))
    monkeypatch.setattr(bridge, "request_workspace_status", lambda *_args: status)
    monkeypatch.setattr(bridge, "request_workspace_task_status", lambda *_args: task_status)
    result = await bridge.build_mcp_server().call_tool("project_status", {})
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    payload = result.structured_content
    assert payload is not None
    assert set(payload) == {
        "project_id",
        "workspace_id",
        "visibility_mode",
        "workspace_root",
        "git",
        "index",
        "current_task",
        "relevant_waiting_task",
        "last_checkpoint",
        "next_step",
        "pending_operator_feedback",
        "schema_version",
    }
    assert payload["git"] == {"head": None, "branch": None, "dirty_path_count": 0}


@pytest.mark.parametrize(
    "field,value", [("head", "wrong"), ("head", 4), ("branch", []), ("branch", "")]
)
def test_private_task_status_snapshot_decoder_rejects_bad_types(field: str, value: Any) -> None:
    response = {
        "version": PROTOCOL_VERSION,
        "request_id": "status",
        "ok": True,
        "result": {
            "schema_version": 24,
            "workspace_id": "workspace",
            "task": None,
            "last_checkpoint": None,
            "pending_operator_feedback": None,
            "head": None,
            "branch": None,
        },
    }
    response["result"][field] = value  # type: ignore[index]
    with pytest.raises(IpcProtocolError):
        _workspace_task_status_from_response(response, expected_request_id="status")
