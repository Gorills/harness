from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_search_behavior
from accept_codex import (
    _MODEL_USAGE_DISCLOSURE,
    ACCEPTANCE_NEGATIVE_SKILL_DESCRIPTION,
    ACCEPTANCE_NEGATIVE_SKILL_ID,
    ACCEPTANCE_SKILL_DESCRIPTION,
    ACCEPTANCE_SKILL_ID,
    EXPECTED_GENERATED_SKILLS,
    MCP_SKILL_BODY_DELIVERY_CONTRACT,
    NEGATIVE_SKILL_PREFLIGHT_POLICY,
    CodexAcceptanceError,
    _acceptance_prompt,
    _codex_exec_events,
    _global_install_environment,
    _installed_python_from_console_script,
    _isolated_environment,
    _prepare_temporary_codex_home,
    _prove_native_skill_negative,
    _prove_native_skill_read,
    _skill_negative_prompt,
    _skill_read_prompt,
    _validate_wire_instructions,
    _validate_wire_tools,
    completed_harness_tool_calls,
    discovery_actions_before_task_start,
    evidence_contains_skill_marker,
    generate_acceptance_skill_nonces,
    main,
    project_actions_before_harness_status,
    prompt_input_contains_bootstrap,
    reject_skill_delivery_fields,
    skill_marker_values,
    split_portable_skill_markdown,
    verify_synthetic_skill_projection,
    write_synthetic_acceptance_skills,
)

from harness.builtin_skills import BUILTIN_SKILLS, sync_builtin_skills
from harness.codex_adapter import CODEX_BOOTSTRAP_INSTRUCTION_BODY
from harness.skills import DetectedProjectStack, load_skill_registry, resolve_skills


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


def test_codex_acceptance_rejects_discovery_before_task_start() -> None:
    search_before_task = [
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
                "server": "harness",
                "tool": "project_search",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "harness",
                "tool": "task_start",
                "status": "completed",
            },
        },
    ]
    native_before_task = [
        search_before_task[0],
        {"type": "item.completed", "item": {"type": "command_execution"}},
        search_before_task[2],
    ]

    assert discovery_actions_before_task_start(search_before_task) == (
        "mcp:harness:project_search",
    )
    assert discovery_actions_before_task_start(native_before_task) == ("command_execution",)


def test_codex_acceptance_allows_search_after_task_start() -> None:
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
                "server": "harness",
                "tool": "task_start",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "harness",
                "tool": "project_search",
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "command_execution"}},
    ]

    assert discovery_actions_before_task_start(events) == ()
    assert project_actions_before_harness_status(events) == ()


def test_accept_codex_search_behavior_is_metrics_only_when_present() -> None:
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
                "server": "harness",
                "tool": "task_start",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "harness",
                "tool": "project_search",
                "status": "completed",
                "arguments": {"query": "authenticate user"},
                "result": {
                    "structured_content": {
                        "results": [{"kind": "code", "path": "src/auth.py"}],
                    }
                },
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "cat src/auth.py",
                "status": "completed",
            },
        },
    ]
    payload = eval_search_behavior.sanitized_search_behavior_metrics(
        events, workspace_root=Path("/tmp/ws")
    )
    assert tuple(payload) == eval_search_behavior.SANITIZED_METRIC_KEYS
    assert "evidence" not in payload
    assert "candidate_paths" not in payload
    assert "native_commands" not in payload
    assert "schema_version" not in payload


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
            "description": (
                "Required first repository action. project_status description"
                if name == "project_status"
                else f"{name} description"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {key: {} for key in keys},
                "additionalProperties": False,
            },
        }
        for name, keys in properties.items()
    ]

    assert _validate_wire_tools(tools) == tuple(properties)

    leaked = [
        {
            **tool,
            "description": f"{tool['description']} recommended_skills",
        }
        for tool in tools
    ]
    with pytest.raises(CodexAcceptanceError, match="skill-delivery field"):
        _validate_wire_tools(leaked)


def test_codex_acceptance_locks_mcp_does_not_deliver_skill_bodies() -> None:
    assert MCP_SKILL_BODY_DELIVERY_CONTRACT == "mcp-does-not-deliver-skill-bodies"
    reject_skill_delivery_fields({"task_id": "t", "revision": 1}, surface="task_start")
    with pytest.raises(CodexAcceptanceError, match="recommended_skills"):
        reject_skill_delivery_fields(
            {"task_id": "t", "recommended_skills": ["language-engineering"]},
            surface="task_start",
        )


def test_codex_acceptance_requires_unambiguous_server_bootstrap() -> None:
    instructions = (
        "project_status must be the first repository action. Before any shell command, locate "
        "Harness. Tool discovery is the only allowed pre-status action. After status, "
        "start/resume a Task before diagnosis or edits. Then project_search before broad native "
        "exploration."
    )

    _validate_wire_instructions(instructions)
    with pytest.raises(CodexAcceptanceError, match="strict bootstrap phrase"):
        _validate_wire_instructions("Use project_status before broad work")
    with pytest.raises(CodexAcceptanceError, match="ambiguous broad-work wording"):
        _validate_wire_instructions(
            instructions + " Before broad repository exploration, use project_status."
        )
    with pytest.raises(CodexAcceptanceError, match="search-before-task wording"):
        _validate_wire_instructions(instructions + " After status use project_search extra.")
    with pytest.raises(CodexAcceptanceError, match="Task before project_search"):
        _validate_wire_instructions(
            "project_status must be the first repository action. Before any shell command, "
            "locate Harness. Tool discovery is the only allowed pre-status action. After status, "
            "then project_search, then start/resume a Task."
        )


def test_codex_acceptance_prompt_exercises_natural_discovery_without_tool_hints() -> None:
    prompt = _acceptance_prompt()

    assert "normal repository task" in prompt
    assert "pyproject.toml" in prompt
    assert "README.md" in prompt
    assert "Harness" not in prompt
    create_at = prompt.find("create a Russian-titled work record")
    find_at = prompt.find("find relevant project material")
    assert 0 <= create_at < find_at
    assert "may be read natively" in prompt
    assert "semantic refs" in prompt
    assert "find and expand the relevant project context" not in prompt
    for tool_name in (
        "project_status",
        "project_search",
        "project_context",
        "task_start",
        "task_checkpoint",
    ):
        assert tool_name not in prompt


def test_expected_generated_skills_include_synthetic_acceptance_skill() -> None:
    assert EXPECTED_GENERATED_SKILLS == tuple(sorted(EXPECTED_GENERATED_SKILLS))
    assert ACCEPTANCE_SKILL_ID in EXPECTED_GENERATED_SKILLS
    assert ACCEPTANCE_NEGATIVE_SKILL_ID in EXPECTED_GENERATED_SKILLS
    assert "secure-by-design" in EXPECTED_GENERATED_SKILLS
    assert "testing-strategy" in EXPECTED_GENERATED_SKILLS
    assert "project-architecture" in EXPECTED_GENERATED_SKILLS
    assert "complex-change-planning" in EXPECTED_GENERATED_SKILLS
    assert NEGATIVE_SKILL_PREFLIGHT_POLICY.startswith("projected;")


def test_acceptance_skill_nonces_are_generated_per_run_and_absent_from_prompts() -> None:
    first = generate_acceptance_skill_nonces()
    second = generate_acceptance_skill_nonces()

    assert first.positive != second.positive
    assert first.negative != second.negative
    assert first.positive != first.negative
    assert len(first.positive) == 32
    assert len(first.negative) == 32

    positive_prompt = _skill_read_prompt()
    negative_prompt = _skill_negative_prompt()
    mcp_prompt = _acceptance_prompt()
    for nonce in (first.positive, first.negative, second.positive, second.negative):
        assert nonce not in positive_prompt
        assert nonce not in negative_prompt
        assert nonce not in mcp_prompt
        assert nonce not in _MODEL_USAGE_DISCLOSURE

    assert "synthetic acceptance workflow" in positive_prompt
    assert "skill_marker" in positive_prompt
    assert "synthetic acceptance" not in negative_prompt
    assert "negative-control" not in negative_prompt


def test_evidence_parser_accepts_and_rejects_skill_marker() -> None:
    nonce = "ab" * 16
    accepted = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"workspace": "tmp", "skill_marker": nonce}),
            },
        }
    ]
    nested = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"skill_marker": nonce}),
                    }
                ],
            },
        }
    ]
    wrong = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"skill_marker": "cd" * 16}),
            },
        }
    ]
    missing = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"package_summary": "fixture"}'},
        }
    ]

    assert evidence_contains_skill_marker(accepted, nonce)
    assert skill_marker_values(accepted) == (nonce,)
    assert evidence_contains_skill_marker(nested, nonce)
    assert not evidence_contains_skill_marker(wrong, nonce)
    assert not evidence_contains_skill_marker(missing, nonce)
    assert not evidence_contains_skill_marker([], nonce)
    assert not evidence_contains_skill_marker(accepted, "")


def test_synthetic_skill_projection_keeps_nonce_in_body_not_frontmatter(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "skills"
    workspace = tmp_path / "workspace"
    nonces = generate_acceptance_skill_nonces()
    write_synthetic_acceptance_skills(registry, nonces)

    for skill_id, nonce in (
        (ACCEPTANCE_SKILL_ID, nonces.positive),
        (ACCEPTANCE_NEGATIVE_SKILL_ID, nonces.negative),
    ):
        source = (registry / skill_id / "SKILL.md").read_text(encoding="utf-8")
        frontmatter, body = split_portable_skill_markdown(source)
        assert nonce in body
        assert nonce not in frontmatter
        metadata = (registry / skill_id / "harness.yaml").read_text(encoding="utf-8")
        assert nonce not in metadata
        assert "software-project" in metadata
        assert "python" in metadata
        assert "task_hints:" not in metadata
        projected = workspace / ".agents" / "skills" / skill_id
        projected.mkdir(parents=True)
        (projected / "SKILL.md").write_text(source, encoding="utf-8")

    verify_synthetic_skill_projection(workspace, nonces, positive_prompt=_skill_read_prompt())

    positive_meta = (registry / ACCEPTANCE_SKILL_ID / "harness.yaml").read_text(encoding="utf-8")
    negative_meta = (registry / ACCEPTANCE_NEGATIVE_SKILL_ID / "harness.yaml").read_text(
        encoding="utf-8"
    )
    assert positive_meta.replace(ACCEPTANCE_SKILL_ID, "SKILL") == negative_meta.replace(
        ACCEPTANCE_NEGATIVE_SKILL_ID, "SKILL"
    )


def test_synthetic_acceptance_skills_project_from_applies_not_task_hints(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    write_synthetic_acceptance_skills(registry, generate_acceptance_skill_nonces())
    definitions = load_skill_registry(registry)
    stack = DetectedProjectStack(
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset({"software-project"}),
    )
    builtin_ids = {skill.skill_id for skill in BUILTIN_SKILLS}
    assert ACCEPTANCE_SKILL_ID not in builtin_ids
    assert ACCEPTANCE_NEGATIVE_SKILL_ID not in builtin_ids

    by_id = {definition.skill_id: definition for definition in definitions}
    assert by_id[ACCEPTANCE_SKILL_ID].task_hints == ()
    assert by_id[ACCEPTANCE_NEGATIVE_SKILL_ID].task_hints == ()
    assert by_id[ACCEPTANCE_SKILL_ID].applies.languages == ("python",)
    assert by_id[ACCEPTANCE_SKILL_ID].applies.facets == ("software-project",)
    assert by_id[ACCEPTANCE_NEGATIVE_SKILL_ID].applies == by_id[ACCEPTANCE_SKILL_ID].applies

    selected = tuple(item.definition.skill_id for item in resolve_skills(definitions, stack))
    assert selected == EXPECTED_GENERATED_SKILLS
    assert ACCEPTANCE_SKILL_ID in selected
    assert ACCEPTANCE_NEGATIVE_SKILL_ID in selected


def test_skill_read_prompt_selects_by_description_not_task_metadata() -> None:
    positive = _skill_read_prompt()
    negative = _skill_negative_prompt()
    assert "synthetic acceptance workflow" in ACCEPTANCE_SKILL_DESCRIPTION
    assert ACCEPTANCE_SKILL_DESCRIPTION.split()[0] == "Use"
    assert "unrelated negative-control" in ACCEPTANCE_NEGATIVE_SKILL_DESCRIPTION
    assert "task_hints" not in positive
    assert "stack_hints" not in positive
    assert "task_start" not in positive
    assert ACCEPTANCE_SKILL_ID not in positive
    assert ACCEPTANCE_NEGATIVE_SKILL_ID not in positive
    assert ACCEPTANCE_SKILL_ID not in negative
    assert "synthetic acceptance workflow" in positive
    assert "synthetic acceptance" not in negative


def test_native_skill_read_requires_skill_marker_field_not_jsonl_substring() -> None:
    nonce = "ab" * 16
    substring_only = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": f"When this skill is applied, return this exact marker: {nonce}",
            },
        }
    ]
    field_ok = [
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"skill_marker": nonce}),
            },
        }
    ]

    assert evidence_contains_skill_marker(substring_only, nonce)
    assert skill_marker_values(substring_only) == ()
    _prove_native_skill_read(field_ok, nonce)
    with pytest.raises(CodexAcceptanceError, match="skill_marker"):
        _prove_native_skill_read(substring_only, nonce)
    with pytest.raises(CodexAcceptanceError, match="unmatched skill nonce"):
        _prove_native_skill_negative(substring_only, nonce)
    _prove_native_skill_negative(field_ok, "cd" * 16)


def test_skill_read_exec_does_not_force_harness_mcp_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_run(
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: int = 120,
        show_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        captured.append(tuple(command))
        return subprocess.CompletedProcess(
            tuple(command),
            0,
            stdout='{"type":"item.completed"}\n',
            stderr="",
        )

    monkeypatch.setattr("accept_codex._run", fake_run)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = {"PATH": "/bin"}
    _codex_exec_events(
        Path("/usr/bin/codex"),
        workspace,
        environment,
        prompt="skill-read",
        timeout=1,
        model=None,
        require_harness_mcp=False,
    )
    _codex_exec_events(
        Path("/usr/bin/codex"),
        workspace,
        environment,
        prompt="five-tool",
        timeout=1,
        model=None,
    )
    required = "mcp_servers.harness.required=true"
    assert required not in captured[0]
    assert required in captured[1]


def test_synthetic_skill_projection_rejects_nonce_in_frontmatter(tmp_path: Path) -> None:
    nonces = generate_acceptance_skill_nonces()
    projected = tmp_path / ".agents" / "skills" / ACCEPTANCE_SKILL_ID
    projected.mkdir(parents=True)
    (projected / "SKILL.md").write_text(
        f"---\nname: {ACCEPTANCE_SKILL_ID}\n"
        f"description: leaked {nonces.positive}\n---\n\nbody {nonces.positive}\n",
        encoding="utf-8",
    )
    negative = tmp_path / ".agents" / "skills" / ACCEPTANCE_NEGATIVE_SKILL_ID
    negative.mkdir(parents=True)
    (negative / "SKILL.md").write_text(
        f"---\nname: {ACCEPTANCE_NEGATIVE_SKILL_ID}\ndescription: other\n---\n\n"
        f"{nonces.negative}\n",
        encoding="utf-8",
    )

    with pytest.raises(CodexAcceptanceError, match="frontmatter leaked"):
        verify_synthetic_skill_projection(tmp_path, nonces, positive_prompt=_skill_read_prompt())


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
    assert "each model run" in output
    assert "native skill-read" in output
    assert "No user repository source is included" in output
    assert "Pass --run-model only after approval" in output
    assert "skill-body secrets" in output
    nonces = generate_acceptance_skill_nonces()
    assert nonces.positive not in output
    assert nonces.negative not in output
    assert nonces.positive not in _MODEL_USAGE_DISCLOSURE
    assert "CODEX_API_KEY" in output
    assert "acceptance-secret" not in output


def test_codex_acceptance_requires_explicit_api_key_for_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    assert main(("--run-model",)) == 1
    output = capsys.readouterr().out
    assert "requires CODEX_API_KEY" in output or "set CODEX_API_KEY" in output
    assert "do not paste it into chat" in output
