from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from accept_codex import (
    EXPECTED_GENERATED_SKILLS,
    CodexAcceptanceError,
    _acceptance_prompt,
    _global_install_environment,
    _installed_python_from_console_script,
    _isolated_environment,
    _prepare_temporary_codex_home,
    _validate_wire_tools,
    completed_harness_tool_calls,
    main,
    project_actions_before_harness_status,
    prompt_input_contains_bootstrap,
)

from harness.codex_adapter import CODEX_BOOTSTRAP_INSTRUCTION_BODY


def test_codex_acceptance_scopes_api_key_away_from_local_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "acceptance-secret")

    environment = _isolated_environment(tmp_path, tmp_path / "codex")

    assert "CODEX_API_KEY" not in environment


def test_codex_global_install_scopes_api_key_and_pythonpath_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "acceptance-secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/source")

    environment = _global_install_environment()

    assert "CODEX_API_KEY" not in environment
    assert "PYTHONPATH" not in environment


def test_codex_global_runtime_resolves_console_script_interpreter(tmp_path: Path) -> None:
    physical_python = tmp_path / "python3.13"
    physical_python.write_text("#!/bin/sh\n", encoding="utf-8")
    physical_python.chmod(0o755)
    python = tmp_path / "python"
    python.symlink_to(physical_python.name)
    harness = tmp_path / "harness"
    harness.write_text(f"#!{python}\n", encoding="utf-8")
    harness.chmod(0o755)

    assert _installed_python_from_console_script(harness) == python


def test_codex_global_runtime_rejects_console_script_without_shebang(tmp_path: Path) -> None:
    harness = tmp_path / "harness"
    harness.write_text("not a console script\n", encoding="utf-8")

    with pytest.raises(CodexAcceptanceError, match="no shebang"):
        _installed_python_from_console_script(harness)


def test_codex_acceptance_extracts_only_successful_completed_harness_calls() -> None:
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "harness",
                "tool": "project_status",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "other",
                "tool": "ignored",
                "status": "completed",
            },
        },
        {"type": "item.started", "item": {"type": "mcp_tool_call"}},
    ]
    assert completed_harness_tool_calls(events) == ("project_status",)


def test_codex_acceptance_rejects_failed_harness_call() -> None:
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server_name": "harness",
                "name": "project_status",
                "status": "failed",
                "error": "boom",
            },
        }
    ]
    with pytest.raises(CodexAcceptanceError, match="tool call failed"):
        completed_harness_tool_calls(events)


def test_codex_acceptance_detects_project_actions_before_harness_status() -> None:
    events = [
        {"type": "item.completed", "item": {"type": "command_execution"}},
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "harness",
                "tool": "project_status",
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "command_execution"}},
    ]

    assert project_actions_before_harness_status(events) == ("command_execution",)


def test_codex_acceptance_validates_exact_fail_closed_wire_catalog() -> None:
    properties = {
        "project_status": (),
        "project_search": ("query", "scope", "limit"),
        "project_context": ("refs",),
        "task_start": ("title", "stack_hints", "task_id", "expected_revision"),
        "task_checkpoint": (
            "task_id",
            "expected_revision",
            "state",
            "summary",
            "next_step",
            "wait_reason",
            "verification",
            "knowledge",
        ),
    }
    tools = [
        {
            "name": name,
            "description": f"{name} description",
            "inputSchema": {
                "type": "object",
                "properties": {key: {} for key in keys},
                "additionalProperties": False,
            },
        }
        for name, keys in properties.items()
    ]

    assert _validate_wire_tools(tools) == tuple(properties)


def test_codex_acceptance_prompt_exercises_natural_discovery_without_tool_hints() -> None:
    prompt = _acceptance_prompt()

    assert "normal repository task" in prompt
    assert "pyproject.toml" in prompt
    assert "README.md" in prompt
    assert "Harness" not in prompt
    for tool_name in (
        "project_status",
        "project_search",
        "project_context",
        "task_start",
        "task_checkpoint",
    ):
        assert tool_name not in prompt
    assert EXPECTED_GENERATED_SKILLS == ("secure-by-design", "testing-strategy")


def test_codex_acceptance_detects_bootstrap_in_rendered_prompt_input() -> None:
    payload = [
        {"role": "developer", "content": [{"type": "text", "text": "other"}]},
        {
            "role": "developer",
            "content": [
                {
                    "type": "text",
                    "text": "prefix\n" + CODEX_BOOTSTRAP_INSTRUCTION_BODY + "\nsuffix",
                }
            ],
        },
    ]

    assert prompt_input_contains_bootstrap(payload)
    assert not prompt_input_contains_bootstrap({"role": "developer", "content": "other"})


def test_codex_acceptance_uses_private_temporary_trust(tmp_path: Path) -> None:
    workspaces = (tmp_path / "repo.with.dot", tmp_path / "second repo")
    for workspace in workspaces:
        workspace.mkdir()
    codex_home = _prepare_temporary_codex_home(tmp_path, workspaces)

    assert (codex_home.stat().st_mode & 0o777) == 0o700
    config = codex_home / "config.toml"
    assert (config.stat().st_mode & 0o777) == 0o600
    assert config.read_text(encoding="utf-8") == "\n".join(
        f'[projects."{workspace.resolve()}"]\ntrust_level = "trusted"\n' for workspace in workspaces
    )


def test_codex_acceptance_requires_explicit_model_usage_acknowledgement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(()) == 2
    output = capsys.readouterr().out
    assert "External destination: the OpenAI Codex service" in output
    assert "one model run" in output
    assert "No user repository source is included" in output
    assert "Pass --run-model only after approval" in output


def test_codex_acceptance_requires_explicit_api_key_for_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    assert main(("--run-model",)) == 1
    output = capsys.readouterr().out
    assert "requires CODEX_API_KEY" in output or "set CODEX_API_KEY" in output
    assert "do not paste it into chat" in output
