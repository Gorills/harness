"""Import-safe Codex exec JSONL helpers shared by acceptance and search-behavior eval.

Event shapes match ``scripts/accept_codex.py`` and Codex ``exec --json``:

- ``item.completed`` with ``item.type`` ``mcp_tool_call`` uses ``server`` or
  ``server_name``, and ``tool`` or ``name``.
- ``command_execution`` / ``file_change`` are native host items. Codex emits
  ``command`` as a string (for example ``bash -lc ls``). A missing command is
  still a native action for ordering metrics.

This module must not import ``accept_codex`` or ``eval_search_behavior``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

_NATIVE_ITEM_TYPES = frozenset({"command_execution", "file_change"})


def iter_completed_items(
    events: Sequence[Mapping[str, Any]],
) -> Iterator[Mapping[str, Any]]:
    """Yield ``item`` objects from ``item.completed`` events."""
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict):
            yield item


def mcp_server_and_tool(item: Mapping[str, Any]) -> tuple[object, object]:
    """Return Codex/alias MCP identity fields from a completed item."""
    return item.get("server") or item.get("server_name"), item.get("tool") or item.get("name")


def project_actions_before_harness_status(
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return repository/tool actions completed before the first Harness project_status call."""
    actions: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "mcp_tool_call":
            server, tool = mcp_server_and_tool(item)
            if server == "harness" and tool == "project_status":
                return tuple(actions)
            actions.append(f"mcp:{server}:{tool}")
        elif item_type in _NATIVE_ITEM_TYPES:
            actions.append(str(item_type))
    return tuple(actions)


def discovery_actions_before_task_start(
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return diagnosis/discovery actions after project_status and before task_start."""
    seen_status = False
    actions: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "mcp_tool_call":
            server, tool = mcp_server_and_tool(item)
            if server == "harness" and tool == "project_status":
                seen_status = True
                continue
            if server == "harness" and tool == "task_start":
                return tuple(actions)
            if not seen_status:
                continue
            actions.append(f"mcp:{server}:{tool}")
        elif item_type in _NATIVE_ITEM_TYPES:
            if not seen_status:
                continue
            actions.append(str(item_type))
    return tuple(actions)
