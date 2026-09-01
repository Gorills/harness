from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from harness.skills import SkillProjectionSurface
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_HOST_PROFILE_ENV = "HARNESS_HOST_PROFILE"
_WORKSPACE_ROOT_ENV = "HARNESS_WORKSPACE_ROOT"
_CODEX_PROFILE = "codex"
_CURSOR_PROFILE = "cursor"
_ANTIGRAVITY_IDE_PROFILE = "antigravity-ide"
_ANTIGRAVITY_CLI_PROFILE = "antigravity-cli"


class HostIntegrationError(RuntimeError):
    """Raised when a host integration cannot be resolved or changed safely."""


class HostRegistrationCollisionError(HostIntegrationError):
    """Raised when a host registration name is already owned by another integration."""


class IntegrationChange(StrEnum):
    """Whether an idempotent host integration operation changed host state."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"


class HostRegistrationState(StrEnum):
    """Observed ownership state for one host's Harness MCP registration."""

    ABSENT = "absent"
    CURRENT = "current"
    STALE_OWNED = "stale_owned"
    FOREIGN = "foreign"


class HostAdapter(Protocol):
    """Narrow host-specific integration boundary used by Harness infrastructure."""

    @property
    def profile(self) -> str: ...

    def workspace_hints(self, environment: Mapping[str, str]) -> tuple[WorkspaceHint, ...]: ...

    def registration_state(self) -> HostRegistrationState: ...

    def register_mcp(self) -> IntegrationChange: ...

    def unregister_mcp(self) -> IntegrationChange: ...

    def skill_projection_surface(self) -> SkillProjectionSurface: ...


def codex_skill_projection_surface() -> SkillProjectionSurface:
    """Return Codex's documented repository skill visibility surface."""
    root = PurePosixPath(".agents/skills")
    return SkillProjectionSurface(
        profile=_CODEX_PROFILE,
        target_root=root,
        visible_roots=(root,),
        required_frontmatter_fields=("name", "description"),
    )


def cursor_skill_projection_surface() -> SkillProjectionSurface:
    """Return Cursor's documented project and compatibility skill visibility surface."""
    root = PurePosixPath(".agents/skills")
    return SkillProjectionSurface(
        profile=_CURSOR_PROFILE,
        target_root=root,
        visible_roots=(
            root,
            PurePosixPath(".cursor/skills"),
            PurePosixPath(".claude/skills"),
            PurePosixPath(".codex/skills"),
        ),
        required_frontmatter_fields=("name", "description"),
        recursive_visible_roots=(
            root,
            PurePosixPath(".cursor/skills"),
            PurePosixPath(".claude/skills"),
            PurePosixPath(".codex/skills"),
        ),
        frontmatter_name_must_match_skill_id=True,
        frontmatter_name_pattern=r"[a-z0-9-]+",
    )


def antigravity_ide_skill_projection_surface() -> SkillProjectionSurface:
    """Return Antigravity IDE's documented Workspace skill visibility surface."""
    root = PurePosixPath(".agents/skills")
    return SkillProjectionSurface(
        profile=_ANTIGRAVITY_IDE_PROFILE,
        target_root=root,
        visible_roots=(root, PurePosixPath(".agent/skills")),
        required_frontmatter_fields=("description",),
    )


def antigravity_cli_skill_projection_surface() -> SkillProjectionSurface:
    """Return Antigravity CLI's current Workspace skill visibility surface."""
    root = PurePosixPath(".agents/skills")
    return SkillProjectionSurface(
        profile=_ANTIGRAVITY_CLI_PROFILE,
        target_root=root,
        visible_roots=(root,),
        required_frontmatter_fields=("description",),
    )


def workspace_hints_from_environment(
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[WorkspaceHint, ...]:
    """Build normalized Workspace hints from Harness-owned host metadata or generic launch facts."""
    values = os.environ if environment is None else environment
    profile = values.get(_HOST_PROFILE_ENV)
    if profile is not None:
        if profile == _CODEX_PROFILE:
            from harness.codex_adapter import CodexAdapter

            return CodexAdapter(
                executable=Path("codex"), python_executable=Path(sys.executable)
            ).workspace_hints(values)
        if profile == _CURSOR_PROFILE:
            from harness.cursor_adapter import discover_cursor_adapter

            return discover_cursor_adapter(environment=values).workspace_hints(values)
        raise HostIntegrationError(f"unsupported Harness host profile: {profile}")

    configured = values.get(_WORKSPACE_ROOT_ENV)
    if configured:
        return (
            _directory_hint(
                configured,
                source="mcp-configured-root",
                match_mode=WorkspaceHintMatchMode.ROOT,
            ),
        )

    location = Path.cwd() if cwd is None else cwd
    return (
        _directory_hint(
            str(location),
            source="mcp-process-cwd",
            match_mode=WorkspaceHintMatchMode.LOCATION,
        ),
    )


def _directory_hint(
    value: str,
    *,
    source: str,
    match_mode: WorkspaceHintMatchMode,
) -> WorkspaceHint:
    path = Path(value)
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostIntegrationError(
            f"active Workspace hint cannot be resolved: {path}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise HostIntegrationError(f"active Workspace hint is not a directory: {resolved}")
    return WorkspaceHint(path=resolved, source=source, match_mode=match_mode)
