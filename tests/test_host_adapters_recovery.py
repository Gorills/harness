from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from harness.host_adapters import (
    ClaudeCodeAdapter,
    HostIntegrationError,
    HostRegistrationCollisionError,
    IntegrationChange,
)


def _registration(command: str) -> dict[str, object]:
    return {
        "type": "stdio",
        "command": command,
        "args": ["-m", "harness.mcp_process"],
        "env": {"HARNESS_HOST_PROFILE": "claude-code"},
    }


def _write_fake_claude(tmp_path: Path) -> tuple[Path, Path, Path]:
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "commands.jsonl"
    executable = tmp_path / "claude"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_CLAUDE_STATE"])
log_path = Path(os.environ["FAKE_CLAUDE_LOG"])
with log_path.open("a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if args[:2] == ["mcp", "get"]:
    foreign_on_get = os.environ.get("FAKE_CLAUDE_FOREIGN_ON_GET")
    owned_on_get = os.environ.get("FAKE_CLAUDE_OWNED_ON_GET")
    if foreign_on_get is not None or owned_on_get is not None:
        commands = [json.loads(line) for line in log_path.read_text().splitlines()]
        get_count = sum(command[:2] == ["mcp", "get"] for command in commands)
        if foreign_on_get is not None and get_count == int(foreign_on_get):
            state_path.write_text(json.dumps({
                "type": "stdio",
                "command": "/foreign/tool",
                "args": ["serve"],
                "env": {},
            }))
        if owned_on_get is not None and get_count == int(owned_on_get):
            state_path.write_text(json.dumps({
                "type": "stdio",
                "command": os.environ["FAKE_CLAUDE_OWNED_COMMAND"],
                "args": ["-m", "harness.mcp_process"],
                "env": {"HARNESS_HOST_PROFILE": "claude-code"},
            }))
    if not state_path.exists():
        print('No MCP server found with name: "harness"')
        raise SystemExit(1)
    config = json.loads(state_path.read_text())
    print("Scope: User config (available in all your projects)")
    print(f"Type: {config['type']}")
    print(f"Command: {config['command']}")
    print("Args: " + " ".join(config["args"]))
    print("Environment:")
    for key, value in config["env"].items():
        print(f"  {key}={value}")
    raise SystemExit(0)

if args[:2] == ["mcp", "remove"]:
    state_path.unlink(missing_ok=True)
    raise SystemExit(0)

if args[:2] == ["mcp", "add-json"]:
    config = json.loads(args[3])
    mode = os.environ.get("FAKE_CLAUDE_ADD_MODE", "ok")
    old_command = os.environ.get("FAKE_CLAUDE_OLD_COMMAND")
    if config["command"] != old_command and mode in {"fail-new", "fail-all", "drop-extra-restore"}:
        raise SystemExit(7)
    if config["command"] != old_command and mode == "drop-new":
        raise SystemExit(0)
    if config["command"] != old_command and mode == "foreign-new":
        state_path.write_text(json.dumps({
            "type": "stdio",
            "command": "/foreign/tool",
            "args": ["serve"],
            "env": {},
        }))
        raise SystemExit(9)
    if config["command"] == old_command and mode == "fail-all":
        raise SystemExit(8)
    if config["command"] == old_command and mode == "drop-extra-restore":
        config["env"].pop("USER_DEFINED", None)
    state_path.write_text(json.dumps(config))
    raise SystemExit(0)

raise SystemExit(2)
"""
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, state_path, log_path


def _adapter(executable: Path) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(executable=executable, python_executable=Path("/new/venv/bin/python"))


def _set_fake_environment(
    monkeypatch: pytest.MonkeyPatch,
    state_path: Path,
    log_path: Path,
    *,
    old_command: str,
    add_mode: str,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state_path))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    monkeypatch.setenv("FAKE_CLAUDE_OLD_COMMAND", old_command)
    monkeypatch.setenv("FAKE_CLAUDE_ADD_MODE", add_mode)


def test_extra_environment_is_not_accepted_as_canonical_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    command = "/new/venv/bin/python"
    registration = _registration(command)
    env = registration["env"]
    assert isinstance(env, dict)
    env["PYTHONPATH"] = "/tmp/injected"
    state_path.write_text(json.dumps(registration))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=command,
        add_mode="ok",
    )

    result = _adapter(executable).register_mcp()

    assert result is IntegrationChange.CHANGED
    assert json.loads(state_path.read_text()) == _registration(command)
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(command[:2] == ["mcp", "remove"] for command in commands)
    assert sum(command[:2] == ["mcp", "add-json"] for command in commands) == 1


def test_replacement_does_not_remove_concurrent_different_owned_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    concurrent_command = "/concurrent/venv/bin/python"
    state_path.write_text(json.dumps(_registration(old_command)))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="ok",
    )
    monkeypatch.setenv("FAKE_CLAUDE_OWNED_ON_GET", "2")
    monkeypatch.setenv("FAKE_CLAUDE_OWNED_COMMAND", concurrent_command)

    with pytest.raises(HostIntegrationError, match="changed before removal"):
        _adapter(executable).register_mcp()

    assert json.loads(state_path.read_text()) == _registration(concurrent_command)
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert not any(command[:2] == ["mcp", "remove"] for command in commands)
    assert not any(command[:2] == ["mcp", "add-json"] for command in commands)


def test_stale_registration_is_restored_when_replacement_add_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    old_registration = _registration(old_command)
    env = old_registration["env"]
    assert isinstance(env, dict)
    env["USER_DEFINED"] = "preserve me"
    state_path.write_text(json.dumps(old_registration))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="fail-new",
    )

    with pytest.raises(
        HostIntegrationError,
        match="registration command failed with exit code 7",
    ):
        _adapter(executable).register_mcp()

    assert json.loads(state_path.read_text()) == old_registration
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    add_commands = [command for command in commands if command[:2] == ["mcp", "add-json"]]
    assert len(add_commands) == 2
    assert json.loads(add_commands[0][3])["command"] == "/new/venv/bin/python"
    assert json.loads(add_commands[1][3]) == old_registration


def test_recovery_verifies_the_exact_backed_up_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    old_registration = _registration(old_command)
    env = old_registration["env"]
    assert isinstance(env, dict)
    env["USER_DEFINED"] = "preserve me"
    state_path.write_text(json.dumps(old_registration))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="drop-extra-restore",
    )

    with pytest.raises(
        HostIntegrationError, match=r"previous Harness registration.*could not be restored"
    ):
        _adapter(executable).register_mcp()

    restored = json.loads(state_path.read_text())
    assert restored["env"] == {"HARNESS_HOST_PROFILE": "claude-code"}


def test_stale_registration_is_restored_when_successful_add_is_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    state_path.write_text(json.dumps(_registration(old_command)))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="drop-new",
    )

    with pytest.raises(HostIntegrationError, match="did not expose the expected Harness"):
        _adapter(executable).register_mcp()

    assert json.loads(state_path.read_text()) == _registration(old_command)


def test_failed_replacement_does_not_overwrite_concurrent_foreign_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    state_path.write_text(json.dumps(_registration(old_command)))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="foreign-new",
    )

    with pytest.raises(HostRegistrationCollisionError, match="non-Harness MCP server"):
        _adapter(executable).register_mcp()

    assert json.loads(state_path.read_text()) == {
        "type": "stdio",
        "command": "/foreign/tool",
        "args": ["serve"],
        "env": {},
    }
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    add_commands = [command for command in commands if command[:2] == ["mcp", "add-json"]]
    assert len(add_commands) == 1


def test_recovery_rechecks_name_before_restoring_previous_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    state_path.write_text(json.dumps(_registration(old_command)))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="fail-new",
    )
    monkeypatch.setenv("FAKE_CLAUDE_FOREIGN_ON_GET", "5")

    with pytest.raises(HostIntegrationError, match="ownership changed before recovery"):
        _adapter(executable).register_mcp()

    assert json.loads(state_path.read_text()) == {
        "type": "stdio",
        "command": "/foreign/tool",
        "args": ["serve"],
        "env": {},
    }
    commands = [json.loads(line) for line in log_path.read_text().splitlines()]
    add_commands = [command for command in commands if command[:2] == ["mcp", "add-json"]]
    assert len(add_commands) == 1


def test_failed_replacement_reports_failed_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, log_path = _write_fake_claude(tmp_path)
    old_command = "/old/venv/bin/python"
    state_path.write_text(json.dumps(_registration(old_command)))
    _set_fake_environment(
        monkeypatch,
        state_path,
        log_path,
        old_command=old_command,
        add_mode="fail-all",
    )

    with pytest.raises(
        HostIntegrationError, match=r"previous Harness registration.*could not be restored"
    ):
        _adapter(executable).register_mcp()

    assert not state_path.exists()
