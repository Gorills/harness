from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from harness.skills import SkillProjectionSurface
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_HOST_PROFILE_ENV = "HARNESS_HOST_PROFILE"
_WORKSPACE_ROOT_ENV = "HARNESS_WORKSPACE_ROOT"
_CLAUDE_PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"
_CLAUDE_PROFILE = "claude-code"
_CLAUDE_SERVER_NAME = "harness"
_CLAUDE_COMMAND_TIMEOUT_SECONDS = 10.0


class HostIntegrationError(RuntimeError):
    """Raised when a host integration cannot be resolved or changed safely."""


class HostRegistrationCollisionError(HostIntegrationError):
    """Raised when a host registration name is already owned by another integration."""


class IntegrationChange(StrEnum):
    """Whether an idempotent host integration operation changed host state."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"


class HostAdapter(Protocol):
    """Narrow host-specific integration boundary used by Harness infrastructure."""

    @property
    def profile(self) -> str: ...

    def workspace_hints(self, environment: Mapping[str, str]) -> tuple[WorkspaceHint, ...]: ...

    def register_mcp(self) -> IntegrationChange: ...

    def unregister_mcp(self) -> IntegrationChange: ...

    def skill_projection_surface(self) -> SkillProjectionSurface: ...


@dataclass(frozen=True, slots=True)
class ClaudeCodeAdapter:
    """Claude Code user-scope MCP registration and documented Workspace-root hints."""

    executable: Path
    python_executable: Path

    @property
    def profile(self) -> str:
        return _CLAUDE_PROFILE

    def skill_projection_surface(self) -> SkillProjectionSurface:
        """Return Claude Code's documented project skill visibility surface."""
        root = PurePosixPath(".claude/skills")
        return SkillProjectionSurface(
            profile=self.profile,
            target_root=root,
            visible_roots=(root,),
        )

    def workspace_hints(self, environment: Mapping[str, str]) -> tuple[WorkspaceHint, ...]:
        configured = environment.get(_CLAUDE_PROJECT_DIR_ENV)
        if not configured:
            raise HostIntegrationError(
                "Claude Code integration did not provide the documented CLAUDE_PROJECT_DIR"
            )
        return (
            _directory_hint(
                configured,
                source="claude-project-dir",
                match_mode=WorkspaceHintMatchMode.ROOT,
            ),
        )

    def register_mcp(self) -> IntegrationChange:
        observed = self._inspect_registration()
        previous_registration: dict[str, object] | None = None
        removed_previous = False
        if observed is not None:
            if not self._is_owned_registration(observed):
                raise HostRegistrationCollisionError(
                    "Claude Code already has a non-Harness MCP server named 'harness'"
                )
            if self._is_desired_registration(observed):
                return IntegrationChange.UNCHANGED
            previous_registration = self._owned_registration_config(observed)
            removed_previous = (
                self._remove_owned_registration(expected_registration=previous_registration)
                is IntegrationChange.CHANGED
            )

        completed = self._add_registration(self._desired_registration())
        if completed.returncode != 0:
            # A concurrent installer may have won the name race. Re-read before deciding whether
            # the failed add is harmless, a collision, or safe to roll back.
            observed = self._inspect_registration()
            if observed is not None and self._is_desired_registration(observed):
                return (
                    IntegrationChange.CHANGED if removed_previous else IntegrationChange.UNCHANGED
                )
            if observed is not None and self._is_owned_registration(observed):
                raise HostIntegrationError(
                    "Claude Code acquired a different Harness MCP registration during install"
                )
            if observed is not None:
                raise HostRegistrationCollisionError(
                    "Claude Code acquired a non-Harness MCP server named 'harness' during install"
                )
            self._restore_previous_registration(previous_registration, removed_previous)
            raise HostIntegrationError(
                f"Claude Code MCP registration command failed with exit code {completed.returncode}"
            )

        observed = self._inspect_registration()
        if observed is None or not self._is_desired_registration(observed):
            if observed is None:
                self._restore_previous_registration(previous_registration, removed_previous)
            raise HostIntegrationError(
                "Claude Code did not expose the expected Harness user-scope MCP registration "
                "after adding it"
            )
        return IntegrationChange.CHANGED

    def unregister_mcp(self) -> IntegrationChange:
        observed = self._inspect_registration()
        if observed is None:
            return IntegrationChange.UNCHANGED
        if not self._is_owned_registration(observed):
            raise HostRegistrationCollisionError(
                "Claude Code MCP server named 'harness' is not owned by Harness"
            )

        return self._remove_owned_registration()

    def _inspect_registration(self) -> str | None:
        # A fresh temporary cwd avoids a same-name local/project MCP entry shadowing the user entry
        # during `claude mcp get`. The user-scope registration itself is independent of cwd.
        with tempfile.TemporaryDirectory(prefix="harness-claude-mcp-") as directory:
            completed = self._run(
                [str(self.executable), "mcp", "get", _CLAUDE_SERVER_NAME], cwd=Path(directory)
            )
        if completed.returncode == 0:
            return completed.stdout
        if f'No MCP server found with name: "{_CLAUDE_SERVER_NAME}"' in completed.stdout:
            return None
        raise HostIntegrationError(
            f"Claude Code MCP inspection command failed with exit code {completed.returncode}"
        )

    def _desired_registration(self) -> dict[str, object]:
        return {
            "type": "stdio",
            "command": str(self.python_executable),
            "args": ["-m", "harness.mcp_process"],
            "env": {_HOST_PROFILE_ENV: _CLAUDE_PROFILE},
        }

    def _owned_registration_config(self, output: str) -> dict[str, object]:
        if not self._is_owned_registration(output):
            raise HostIntegrationError("Claude Code Harness MCP registration cannot be backed up")
        commands = [
            line.strip().removeprefix("Command: ")
            for line in output.splitlines()
            if line.strip().startswith("Command: ")
        ]
        if len(commands) != 1 or not commands[0]:
            raise HostIntegrationError("Claude Code Harness MCP registration command is ambiguous")
        environment = self._registration_environment(output)
        if environment.get(_HOST_PROFILE_ENV) != _CLAUDE_PROFILE:
            raise HostIntegrationError(
                "Claude Code Harness MCP registration environment cannot be backed up"
            )
        return {
            "type": "stdio",
            "command": commands[0],
            "args": ["-m", "harness.mcp_process"],
            "env": environment,
        }

    @staticmethod
    def _registration_environment(output: str) -> dict[str, str]:
        lines = output.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != "Environment:":
                continue
            parent_indent = len(line) - len(line.lstrip())
            environment: dict[str, str] = {}
            for candidate in lines[index + 1 :]:
                stripped = candidate.strip()
                if not stripped:
                    continue
                indent = len(candidate) - len(candidate.lstrip())
                if indent <= parent_indent:
                    break
                if "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key:
                    environment[key] = value
            return environment
        raise HostIntegrationError(
            "Claude Code Harness MCP registration environment is unavailable for backup"
        )

    def _is_owned_registration(self, output: str) -> bool:
        expected_lines = {
            "Scope: User config (available in all your projects)",
            "Type: stdio",
            "Args: -m harness.mcp_process",
            f"{_HOST_PROFILE_ENV}={_CLAUDE_PROFILE}",
        }
        actual_lines = {line.strip() for line in output.splitlines()}
        return expected_lines <= actual_lines

    def _is_desired_registration(self, output: str) -> bool:
        return self._registration_matches(output, self._desired_registration())

    def _registration_matches(self, output: str, registration: Mapping[str, object]) -> bool:
        try:
            observed = self._owned_registration_config(output)
        except HostIntegrationError:
            return False
        return observed == dict(registration)

    def _add_registration(
        self, registration: Mapping[str, object]
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            [
                str(self.executable),
                "mcp",
                "add-json",
                _CLAUDE_SERVER_NAME,
                json.dumps(registration, separators=(",", ":")),
                "--scope",
                "user",
            ]
        )

    def _restore_previous_registration(
        self,
        previous_registration: dict[str, object] | None,
        removed_previous: bool,
    ) -> None:
        if previous_registration is None or not removed_previous:
            return
        observed = self._inspect_registration()
        if observed is not None:
            if self._registration_matches(observed, previous_registration):
                return
            raise HostIntegrationError(
                "Claude Code MCP replacement failed and registration ownership changed "
                "before recovery"
            )
        self._add_registration(previous_registration)
        observed = self._inspect_registration()
        if observed is not None and self._registration_matches(observed, previous_registration):
            return
        raise HostIntegrationError(
            "Claude Code MCP replacement failed and the previous Harness registration "
            "could not be restored"
        )

    def _remove_owned_registration(
        self,
        *,
        expected_registration: Mapping[str, object] | None = None,
    ) -> IntegrationChange:
        # Re-read immediately before mutation so stale Harness observations cannot authorize
        # deletion of an entry that has already been replaced by another integration. Claude's
        # CLI does not expose a compare-and-set primitive, so this is the narrowest available
        # fail-closed ownership check around the user-scope removal.
        observed = self._inspect_registration()
        if observed is None:
            return IntegrationChange.UNCHANGED
        if not self._is_owned_registration(observed):
            raise HostRegistrationCollisionError(
                "Claude Code MCP server named 'harness' changed ownership before removal"
            )
        if expected_registration is not None and not self._registration_matches(
            observed, expected_registration
        ):
            raise HostIntegrationError(
                "Claude Code Harness MCP registration changed before removal"
            )

        completed = self._run(
            [str(self.executable), "mcp", "remove", _CLAUDE_SERVER_NAME, "--scope", "user"]
        )
        if completed.returncode != 0:
            observed = self._inspect_registration()
            if observed is None:
                return IntegrationChange.CHANGED
            if not self._is_owned_registration(observed):
                raise HostRegistrationCollisionError(
                    "Claude Code MCP server named 'harness' changed ownership during removal"
                )
            raise HostIntegrationError(
                f"Claude Code MCP removal command failed with exit code {completed.returncode}"
            )

        if self._inspect_registration() is not None:
            raise HostIntegrationError("Claude Code still exposes an MCP server named 'harness'")
        return IntegrationChange.CHANGED

    @staticmethod
    def _run(
        command: list[str],
        *,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=_CLAUDE_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostIntegrationError(
                "Claude Code integration command could not be executed"
            ) from exc


def discover_claude_code_adapter(
    *,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> ClaudeCodeAdapter | None:
    """Discover the Claude Code CLI without mutating host configuration."""
    values = os.environ if environment is None else environment
    executable = shutil.which("claude", path=values.get("PATH"))
    if executable is None:
        return None
    return ClaudeCodeAdapter(
        executable=_absolute_executable_path(executable),
        python_executable=_absolute_executable_path(
            Path(sys.executable) if python_executable is None else python_executable
        ),
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
        if profile != _CLAUDE_PROFILE:
            raise HostIntegrationError(f"unsupported Harness host profile: {profile}")
        adapter = ClaudeCodeAdapter(
            executable=Path("claude"), python_executable=Path(sys.executable)
        )
        return adapter.workspace_hints(values)

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


def _absolute_executable_path(value: str | Path) -> Path:
    """Make an executable path absolute without dereferencing wrappers or virtualenv symlinks."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
