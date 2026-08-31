from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from harness.host_adapters import (
    HostIntegrationError,
    cursor_skill_projection_surface,
    workspace_hints_from_environment,
)
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode


def test_retired_claude_code_host_profile_is_unsupported() -> None:
    with pytest.raises(HostIntegrationError, match="unsupported Harness host profile: claude-code"):
        workspace_hints_from_environment(environment={"HARNESS_HOST_PROFILE": "claude-code"})


def test_unknown_registered_host_profile_fails_closed() -> None:
    with pytest.raises(HostIntegrationError, match="unsupported Harness host profile"):
        workspace_hints_from_environment(environment={"HARNESS_HOST_PROFILE": "unknown-host"})


def test_generic_workspace_hints_preserve_configured_root_and_cwd_fallback(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    cwd = tmp_path / "cwd"
    configured.mkdir()
    cwd.mkdir()

    assert workspace_hints_from_environment(
        environment={"HARNESS_WORKSPACE_ROOT": str(configured)}, cwd=cwd
    ) == (
        WorkspaceHint(
            path=configured.resolve(),
            source="mcp-configured-root",
            match_mode=WorkspaceHintMatchMode.ROOT,
        ),
    )
    assert workspace_hints_from_environment(environment={}, cwd=cwd) == (
        WorkspaceHint(
            path=cwd.resolve(),
            source="mcp-process-cwd",
            match_mode=WorkspaceHintMatchMode.LOCATION,
        ),
    )


def test_workspace_hint_rejects_missing_or_non_directory_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(HostIntegrationError, match="cannot be resolved"):
        workspace_hints_from_environment(environment={"HARNESS_WORKSPACE_ROOT": str(missing)})
    with pytest.raises(HostIntegrationError, match="not a directory"):
        workspace_hints_from_environment(environment={"HARNESS_WORKSPACE_ROOT": str(file_path)})


def test_cursor_skill_projection_surface_still_lists_leftover_claude_skills() -> None:
    surface = cursor_skill_projection_surface()

    assert PurePosixPath(".claude/skills") in surface.visible_roots
    assert surface.target_root == PurePosixPath(".agents/skills")
