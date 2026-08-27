from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from harness.host_adapters import (
    ClaudeCodeAdapter,
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    discover_claude_code_adapter,
    workspace_hints_from_environment,
)
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode


def _claude_get_output(python: Path) -> str:
    return "\n".join(
        [
            "harness:",
            "  Scope: User config (available in all your projects)",
            "  Status: ✓ Connected",
            "  Type: stdio",
            f"  Command: {python}",
            "  Args: -m harness.mcp_process",
            "  Environment:",
            "    HARNESS_HOST_PROFILE=claude-code",
            'To remove this server, run: claude mcp remove "harness" -s user',
        ]
    )


def _completed(
    command: list[str], returncode: int, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout)


def test_discover_claude_code_adapter_uses_path_and_selected_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude_target = tmp_path / "claude-real"
    claude_target.write_text("#!/bin/sh\n", encoding="utf-8")
    claude_target.chmod(0o755)
    claude = bin_dir / "claude"
    claude.symlink_to(claude_target)

    python_target = tmp_path / "python-real"
    python_target.touch()
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(python_target)

    adapter = discover_claude_code_adapter(
        environment={"PATH": str(bin_dir)}, python_executable=python
    )

    assert adapter == ClaudeCodeAdapter(executable=claude, python_executable=python)


def test_claude_code_skill_projection_surface_uses_native_project_root(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")

    surface = adapter.skill_projection_surface()

    assert surface.profile == "claude-code"
    assert surface.target_root == PurePosixPath(".claude/skills")
    assert surface.visible_roots == (PurePosixPath(".claude/skills"),)


def test_discover_claude_code_adapter_returns_none_when_cli_is_absent(tmp_path: Path) -> None:
    assert discover_claude_code_adapter(environment={"PATH": str(tmp_path)}) is None


def test_claude_workspace_hint_uses_documented_root_over_generic_override(tmp_path: Path) -> None:
    claude_root = tmp_path / "claude-root"
    generic_root = tmp_path / "generic-root"
    claude_root.mkdir()
    generic_root.mkdir()

    assert workspace_hints_from_environment(
        environment={
            "HARNESS_HOST_PROFILE": "claude-code",
            "CLAUDE_PROJECT_DIR": str(claude_root),
            "HARNESS_WORKSPACE_ROOT": str(generic_root),
        }
    ) == (
        WorkspaceHint(
            path=claude_root.resolve(),
            source="claude-project-dir",
            match_mode=WorkspaceHintMatchMode.ROOT,
        ),
    )


def test_registered_claude_profile_fails_closed_without_project_dir() -> None:
    with pytest.raises(HostIntegrationError, match="CLAUDE_PROJECT_DIR"):
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


def test_claude_registration_adds_user_scope_json_and_verifies_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = tmp_path / "claude"
    python = tmp_path / "python"
    adapter = ClaudeCodeAdapter(claude, python)
    calls: list[tuple[list[str], Path | None]] = []
    responses = iter(
        [
            _completed([], 1, 'No MCP server found with name: "harness"'),
            _completed([], 0, "Added stdio MCP server harness"),
            _completed([], 0, _claude_get_output(python)),
        ]
    )

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return next(responses)

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    assert adapter.register_mcp() is IntegrationChange.CHANGED
    assert calls[0][0] == [str(claude), "mcp", "get", "harness"]
    add = calls[1][0]
    assert add[:4] == [str(claude), "mcp", "add-json", "harness"]
    assert add[5:] == ["--scope", "user"]
    assert '"command":"' + str(python) + '"' in add[4]
    assert '"args":["-m","harness.mcp_process"]' in add[4]
    assert '"HARNESS_HOST_PROFILE":"claude-code"' in add[4]
    assert calls[0][1] is not None
    assert calls[2][1] is not None


def test_claude_registration_is_idempotent_when_owned_entry_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    calls = 0

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return _completed(command, 0, _claude_get_output(adapter.python_executable))

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    assert adapter.register_mcp() is IntegrationChange.UNCHANGED
    assert calls == 1


def test_claude_registration_preserves_foreign_same_name_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return _completed(
            command,
            0,
            "\n".join(
                [
                    "harness:",
                    "  Scope: User config (available in all your projects)",
                    "  Type: stdio",
                    "  Command: other-harness",
                    "  Args: serve",
                ]
            ),
        )

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    with pytest.raises(HostRegistrationCollisionError, match="non-Harness"):
        adapter.register_mcp()


def test_claude_registration_treats_concurrent_identical_add_as_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    responses = iter(
        [
            _completed([], 1, 'No MCP server found with name: "harness"'),
            _completed([], 1, "MCP server harness already exists in user config"),
            _completed([], 0, _claude_get_output(adapter.python_executable)),
        ]
    )
    monkeypatch.setattr(
        ClaudeCodeAdapter, "_run", staticmethod(lambda command, cwd=None: next(responses))
    )

    assert adapter.register_mcp() is IntegrationChange.UNCHANGED


def test_claude_registration_fails_closed_when_added_entry_cannot_be_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    calls: list[list[str]] = []
    responses = iter(
        [
            _completed([], 1, 'No MCP server found with name: "harness"'),
            _completed([], 0),
            _completed([], 1, 'No MCP server found with name: "harness"'),
        ]
    )

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    with pytest.raises(HostIntegrationError, match="did not expose"):
        adapter.register_mcp()
    assert all("remove" not in call for call in calls)


def test_claude_unregistration_removes_only_owned_user_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    calls: list[list[str]] = []
    responses = iter(
        [
            _completed([], 0, _claude_get_output(adapter.python_executable)),
            _completed([], 0, _claude_get_output(adapter.python_executable)),
            _completed([], 0, "Removed MCP server"),
            _completed([], 1, 'No MCP server found with name: "harness"'),
        ]
    )

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    assert adapter.unregister_mcp() is IntegrationChange.CHANGED
    assert calls[2] == [str(adapter.executable), "mcp", "remove", "harness", "--scope", "user"]


def test_claude_unregistration_rechecks_ownership_before_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    calls: list[list[str]] = []
    foreign = "\n".join(
        [
            "harness:",
            "  Scope: User config (available in all your projects)",
            "  Type: stdio",
            "  Command: foreign-server",
            "  Args: serve",
        ]
    )
    responses = iter(
        [
            _completed([], 0, _claude_get_output(adapter.python_executable)),
            _completed([], 0, foreign),
        ]
    )

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    with pytest.raises(HostRegistrationCollisionError, match="changed ownership before removal"):
        adapter.unregister_mcp()
    assert all("remove" not in command for command in calls)


def test_claude_unregistration_is_idempotent_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    monkeypatch.setattr(
        ClaudeCodeAdapter,
        "_run",
        staticmethod(
            lambda command, cwd=None: _completed(
                command, 1, 'No MCP server found with name: "harness"'
            )
        ),
    )

    assert adapter.unregister_mcp() is IntegrationChange.UNCHANGED


def test_claude_registration_replaces_stale_owned_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "new-python")
    old_python = tmp_path / "old-python"
    calls: list[list[str]] = []
    responses = iter(
        [
            _completed([], 0, _claude_get_output(old_python)),
            _completed([], 0, _claude_get_output(old_python)),
            _completed([], 0, "Removed MCP server"),
            _completed([], 1, 'No MCP server found with name: "harness"'),
            _completed([], 0, "Added stdio MCP server harness"),
            _completed([], 0, _claude_get_output(adapter.python_executable)),
        ]
    )

    def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(ClaudeCodeAdapter, "_run", staticmethod(run))

    assert adapter.register_mcp() is IntegrationChange.CHANGED
    assert calls[2] == [str(adapter.executable), "mcp", "remove", "harness", "--scope", "user"]
    assert "add-json" in calls[4]


def test_claude_inspection_failure_is_not_treated_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    monkeypatch.setattr(
        ClaudeCodeAdapter,
        "_run",
        staticmethod(lambda command, cwd=None: _completed(command, 2, "configuration error")),
    )

    with pytest.raises(HostIntegrationError, match="inspection command failed"):
        adapter.unregister_mcp()


def test_claude_inspect_treats_unquoted_2_1_109_absent_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    outputs = iter(
        [
            _completed([], 1, "No MCP server found with name: harness\n"),
            _completed([], 1, 'No MCP server found with name: "harness"\n'),
            _completed([], 1, "unexpected claude mcp get failure\n"),
        ]
    )
    monkeypatch.setattr(
        ClaudeCodeAdapter,
        "_run",
        staticmethod(lambda command, cwd=None: next(outputs)),
    )

    assert adapter.registration_state() is HostRegistrationState.ABSENT
    assert adapter.registration_state() is HostRegistrationState.ABSENT
    with pytest.raises(HostIntegrationError, match="inspection command failed"):
        adapter.registration_state()


def test_claude_command_execution_errors_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "missing-claude", tmp_path / "python")

    def explode(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("secret host detail")

    monkeypatch.setattr(subprocess, "run", explode)

    with pytest.raises(HostIntegrationError) as raised:
        adapter.register_mcp()
    assert str(raised.value) == "Claude Code integration command could not be executed"
    assert "secret" not in str(raised.value)


def test_claude_registration_state_distinguishes_absent_current_stale_and_foreign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ClaudeCodeAdapter(tmp_path / "claude", tmp_path / "python")
    outputs = iter(
        [
            _completed([], 1, 'No MCP server found with name: "harness"'),
            _completed([], 0, _claude_get_output(adapter.python_executable)),
            _completed([], 0, _claude_get_output(tmp_path / "old-python")),
            _completed(
                [],
                0,
                "\n".join(
                    [
                        "Scope: User config (available in all your projects)",
                        "Type: stdio",
                        "Command: /foreign/tool",
                        "Args: serve",
                    ]
                ),
            ),
        ]
    )
    monkeypatch.setattr(
        ClaudeCodeAdapter,
        "_run",
        staticmethod(lambda command, cwd=None: next(outputs)),
    )

    assert adapter.registration_state() is HostRegistrationState.ABSENT
    assert adapter.registration_state() is HostRegistrationState.CURRENT
    assert adapter.registration_state() is HostRegistrationState.STALE_OWNED
    assert adapter.registration_state() is HostRegistrationState.FOREIGN
