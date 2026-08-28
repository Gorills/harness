from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fake_hosts import path_without_agent, write_fake_cursor_agent

from harness.entrypoints import harness_main
from harness.runtime_paths import default_runtime_paths

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX installation lifecycle")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repo(root: Path) -> None:
    root.mkdir()
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "commit",
        "-m",
        "init",
    )


def _skill_registry(home: Path) -> None:
    skill = home / ".harness" / "skills" / "python-helper"
    skill.mkdir(parents=True)
    (home / ".harness" / "skills").chmod(0o700)
    (skill / "SKILL.md").write_text(
        "---\nname: python-helper\ndescription: Python conventions\n---\n\n# Python helper\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text(
        "id: python-helper\napplies:\n  languages:\n    - python\n",
        encoding="utf-8",
    )


def _fake_claude(bin_dir: Path, state_path: Path) -> Path:
    executable = bin_dir / "claude"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_CLAUDE_STATE"])
args = sys.argv[1:]
if args[:3] == ["mcp", "get", "harness"]:
    if not state.exists():
        print("No MCP server found with name: harness")
        raise SystemExit(1)
    config = json.loads(state.read_text(encoding="utf-8"))
    print("Scope: User config (available in all your projects)")
    print("Type: " + config["type"])
    print("Command: " + config["command"])
    print("Args: " + " ".join(config["args"]))
    print("Environment:")
    for key, value in config["env"].items():
        print(f"  {key}={value}")
    raise SystemExit(0)
if args[:3] == ["mcp", "add-json", "harness"]:
    state.write_text(json.dumps(json.loads(args[3])), encoding="utf-8")
    raise SystemExit(0)
if args[:3] == ["mcp", "remove", "harness"]:
    state.unlink(missing_ok=True)
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_linux_install_scan_uninstall_and_purge_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    install_output = capsys.readouterr().out
    assert "MCP registration: changed" in install_output
    assert "Built-in skills: 12 (installed 12, updated 0)" in install_output
    assert (home / ".harness" / "skills" / "backend-security" / "SKILL.md").is_file()
    assert "Harness install: OK" in install_output
    registration = json.loads(claude_state.read_text(encoding="utf-8"))
    assert registration == {
        "type": "stdio",
        "command": os.path.abspath(sys.executable),
        "args": ["-m", "harness.mcp_process"],
        "env": {"HARNESS_HOST_PROFILE": "claude-code"},
    }

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    assert "MCP registration: unchanged" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    scan_output = capsys.readouterr().out
    assert "Relevant skills: 1" in scan_output
    assert (repo / ".claude" / "skills" / "python-helper" / "SKILL.md").exists()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Daemon: OK" in doctor_output
    assert "MCP registration: OK" in doctor_output
    assert "Projects: OK" in doctor_output
    assert "Index state: OK" in doctor_output
    assert "Generated skills: OK" in doctor_output
    assert "Dashboard: OK" in doctor_output
    assert "Stale integrations: OK" in doctor_output
    assert "0 FAIL" in doctor_output

    paths = default_runtime_paths()
    assert paths.database.exists()
    assert paths.socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall"])
    assert harness_main() == 0
    uninstall_output = capsys.readouterr().out
    assert "Generated skills removed: 1" in uninstall_output
    assert "Project Intelligence: preserved" in uninstall_output
    assert not claude_state.exists()
    assert not (repo / ".claude" / "skills" / "python-helper").exists()
    assert paths.database.exists()
    assert not paths.socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 0
    purge_output = capsys.readouterr().out
    assert "Project Intelligence: purged" in purge_output
    assert not paths.database.exists()
    assert not (home / ".harness" / "skills").exists()


def test_install_foreign_registration_fails_before_daemon_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    claude_state.write_text(
        json.dumps({"type": "stdio", "command": "/foreign/tool", "args": ["serve"], "env": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 1
    assert "non-Harness MCP server" in capsys.readouterr().out
    assert not (state_home / "harness" / "harness.db").exists()
    assert not (runtime_home / "harness" / "harness.sock").exists()


def test_install_refuses_unsafe_canonical_database_before_daemon_or_registration_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    paths = default_runtime_paths()
    paths.database.parent.mkdir(mode=0o700, parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"user-data")
    paths.database.symlink_to(outside)

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 1
    assert "unsafe database state" in capsys.readouterr().out
    assert not claude_state.exists()
    assert not paths.socket.exists()
    assert paths.database.is_symlink()
    assert outside.read_bytes() == b"user-data"


def test_uninstall_when_nothing_is_installed_is_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall"])
    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "MCP registration removal: unchanged" in output
    assert "Harness uninstall: OK" in output
    assert not (state_home / "harness" / "harness.db").exists()
    assert not (runtime_home / "harness" / "harness.sock").exists()


def test_purge_without_daemon_state_still_removes_canonical_skill_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _skill_registry(home)
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 0
    assert "Project Intelligence: purged" in capsys.readouterr().out
    assert not (home / ".harness" / "skills").exists()
    assert not (state_home / "harness" / "harness.db").exists()


def test_purge_preflight_refuses_unsafe_skill_registry_before_uninstall_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    capsys.readouterr()
    paths = default_runtime_paths()
    assert paths.database.exists()
    assert paths.socket.exists()
    registration_before = claude_state.read_bytes()

    outside = tmp_path / "outside-skills"
    outside.mkdir()
    sentinel = outside / "user.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    harness_home = home / ".harness"
    harness_home.mkdir(exist_ok=True)
    shutil.rmtree(harness_home / "skills")
    (harness_home / "skills").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 1
    assert "unsafe skill registry" in capsys.readouterr().out
    assert claude_state.read_bytes() == registration_before
    assert paths.database.exists()
    assert paths.socket.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"

    (harness_home / "skills").unlink()
    monkeypatch.setattr(sys, "argv", ["harness", "uninstall"])
    assert harness_main() == 0
    capsys.readouterr()


def test_purge_preflight_refuses_unsafe_database_candidate_before_registry_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _skill_registry(home)
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    capsys.readouterr()
    paths = default_runtime_paths()
    registration_before = claude_state.read_bytes()
    unsafe_candidate = paths.database.with_name(f"{paths.database.name}-journal")
    unsafe_candidate.mkdir()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 1
    assert "unsafe database state" in capsys.readouterr().out
    assert claude_state.read_bytes() == registration_before
    assert paths.database.exists()
    assert paths.socket.exists()
    assert (home / ".harness" / "skills" / "python-helper" / "SKILL.md").exists()

    unsafe_candidate.rmdir()
    monkeypatch.setattr(sys, "argv", ["harness", "uninstall"])
    assert harness_main() == 0
    capsys.readouterr()


def test_purge_without_database_removes_known_sidecar_under_maintenance_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    paths = default_runtime_paths()
    paths.database.parent.mkdir(mode=0o700, parents=True)
    sidecar = paths.database.with_name(f"{paths.database.name}-journal")
    sidecar.write_bytes(b"stale-sidecar")

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 0
    assert "Project Intelligence: purged" in capsys.readouterr().out
    assert not sidecar.exists()
    assert not paths.database.exists()
    assert paths.database.with_name(f"{paths.database.name}.lock").is_file()


def test_purge_preflight_refuses_database_symlink_without_unlinking_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    paths = default_runtime_paths()
    paths.database.parent.mkdir(mode=0o700, parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"user-data")
    paths.database.symlink_to(outside)

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 1
    assert "unsafe database state" in capsys.readouterr().out
    assert paths.database.is_symlink()
    assert outside.read_bytes() == b"user-data"


def test_cursor_scan_reports_restart_when_project_override_is_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PATH", path_without_agent())

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    output = capsys.readouterr().out

    assert "Cursor restart required: fully quit and reopen Cursor" in output
    assert "Cursor CLI was not found" in output
    assert "agent mcp enable harness" in output
    assert "Leftover user-harness is not Workspace identity" in output
    assert "harness-dev" in output
    assert (repo / ".cursor" / "mcp.json").is_file()


def test_cursor_doctor_marks_global_workspace_folder_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PATH", path_without_agent())

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()

    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    leftover = {
        "mcpServers": {
            "harness": {
                "type": "stdio",
                "command": os.path.abspath(sys.executable),
                "args": ["-m", "harness.mcp_process"],
                "env": {
                    "HARNESS_HOST_PROFILE": "cursor",
                    "HARNESS_WORKSPACE_ROOT": "${workspaceFolder}",
                },
            }
        }
    }
    config.write_text(json.dumps(leftover), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "Cursor MCP registration: FAIL" in output
    assert "leftover owned user-harness" in output
    assert "configured HARNESS_WORKSPACE_ROOT=${workspaceFolder}" in output


def test_multi_host_cursor_install_scan_uninstall_preserves_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    agent_state = tmp_path / "agent-state.json"
    write_fake_cursor_agent(fake_bin, agent_state)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(agent_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    cursor_install = capsys.readouterr().out
    assert "Harness host: cursor" in cursor_install
    assert "Cursor project overrides changed: 1" in cursor_install
    assert "Cursor project MCP tools verified: 1" in cursor_install
    assert "Cursor restart required: fully quit and reopen Cursor" in cursor_install
    assert "Leftover user-harness is not Workspace identity" in cursor_install
    cursor_global = home / ".cursor" / "mcp.json"
    if cursor_global.is_file():
        global_value = json.loads(cursor_global.read_text(encoding="utf-8"))
        assert "harness" not in global_value.get("mcpServers", {})
    project_config = repo / ".cursor" / "mcp.json"
    project_value = json.loads(project_config.read_text(encoding="utf-8"))
    assert project_value["mcpServers"]["harness"]["env"] == {
        "HARNESS_HOST_PROFILE": "cursor",
        "HARNESS_WORKSPACE_ROOT": "${workspaceFolder}",
    }
    host_state = json.loads(
        (state_home / "harness" / "host-integrations.json").read_text(encoding="utf-8")
    )
    assert host_state["profiles"] == ["cursor"]

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    capsys.readouterr()
    assert (repo / ".claude" / "skills" / "python-helper" / "SKILL.md").is_file()
    assert not (repo / ".agents" / "skills" / "python-helper").exists()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Claude Code MCP registration: OK" in doctor_output
    assert "Cursor MCP registration: OK" in doctor_output
    assert "no user-harness" in doctor_output
    assert "Cursor project MCP overrides: OK" in doctor_output
    assert "Cursor project MCP tools: OK" in doctor_output
    assert "Generated skills: OK" in doctor_output

    broken_project = json.loads(project_config.read_text(encoding="utf-8"))
    broken_project["mcpServers"]["harness"]["command"] = "/stale/cursor/python"
    project_config.write_text(json.dumps(broken_project), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 1
    broken_doctor = capsys.readouterr().out
    assert "Claude Code MCP registration: OK" in broken_doctor
    assert "Cursor project MCP overrides: FAIL" in broken_doctor
    assert "Cursor project MCP override " in broken_doctor
    assert str(project_config) in broken_doctor
    assert f"expected Python: {os.path.abspath(sys.executable)}" in broken_doctor
    assert "configured Python: /stale/cursor/python" in broken_doctor
    assert "expected HARNESS_WORKSPACE_ROOT=${workspaceFolder}" in broken_doctor
    assert "remediation: harness install --host cursor" in broken_doctor
    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()

    paths = default_runtime_paths()
    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "cursor"])
    assert harness_main() == 0
    uninstall_output = capsys.readouterr().out
    assert "Project Intelligence: preserved" in uninstall_output
    assert "Cursor verification: agent mcp list (confirm Harness is absent)" in uninstall_output
    assert project_config.exists() is False
    assert claude_state.is_file()
    assert paths.socket.exists()
    assert (repo / ".claude" / "skills" / "python-helper" / "SKILL.md").is_file()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    after = capsys.readouterr().out
    assert "Claude Code MCP registration: OK" in after
    assert "Cursor MCP registration: OK" in after
    assert "Harness Cursor integration is not configured" in after
    assert "remediation: harness install --host cursor" in after
    assert "Generated skills: OK" in after

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "all"])
    assert harness_main() == 0
    capsys.readouterr()
    assert not claude_state.exists()
    assert not project_config.exists()
    assert not paths.socket.exists()


def test_cursor_install_enables_independent_workspaces_and_linked_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    agent_state = tmp_path / "agent-state.json"
    write_fake_cursor_agent(fake_bin, agent_state)
    _skill_registry(home)
    repo_a = tmp_path / "repo-a"
    repo_c = tmp_path / "repo-c"
    _repo(repo_a)
    _repo(repo_c)
    worktree = tmp_path / "repo-a-worktree"
    _git(repo_a, "worktree", "add", "-b", "linked", str(worktree))

    leftover = home / ".cursor" / "mcp.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness": {
                        "type": "stdio",
                        "command": os.path.abspath(sys.executable),
                        "args": ["-m", "harness.mcp_process"],
                        "env": {"HARNESS_HOST_PROFILE": "cursor"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(agent_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "all"])
    assert harness_main() == 0
    capsys.readouterr()
    for root in (repo_a, repo_c, worktree):
        monkeypatch.setattr(sys, "argv", ["harness", "scan", str(root)])
        assert harness_main() == 0
        capsys.readouterr()
        project = json.loads((root / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        assert project["mcpServers"]["harness"]["env"]["HARNESS_WORKSPACE_ROOT"] == (
            "${workspaceFolder}"
        )

    enabled = json.loads(agent_state.read_text(encoding="utf-8"))["enabled"]
    assert enabled[str(repo_a.resolve())] is True
    assert enabled[str(repo_c.resolve())] is True
    assert enabled[str(worktree.resolve())] is True
    global_value = json.loads(leftover.read_text(encoding="utf-8"))
    assert "harness" not in global_value.get("mcpServers", {})

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Cursor project MCP overrides: OK" in doctor_output
    assert "Cursor project MCP tools: OK" in doctor_output
    assert "no user-harness" in doctor_output


def test_uninstall_claude_reprojects_skills_for_remaining_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "all"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    capsys.readouterr()
    assert (repo / ".claude" / "skills" / "python-helper").is_dir()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "claude-code"])
    assert harness_main() == 0
    capsys.readouterr()
    assert not claude_state.exists()
    assert not (home / ".cursor" / "mcp.json").exists() or "harness" not in json.loads(
        (home / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    ).get("mcpServers", {})
    assert (repo / ".cursor" / "mcp.json").is_file()
    assert json.loads(
        (state_home / "harness" / "host-integrations.json").read_text(encoding="utf-8")
    )["profiles"] == ["cursor"]
    assert not (repo / ".claude" / "skills" / "python-helper").exists()
    assert (repo / ".agents" / "skills" / "python-helper" / "SKILL.md").is_file()
    assert default_runtime_paths().socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Cursor MCP registration: OK" in output
    assert "no user-harness" in output
    assert "Cursor project MCP overrides: OK" in output
    assert "Generated skills: OK" in output

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()


def test_purge_is_refused_while_another_host_remains_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_state = tmp_path / "claude-state.json"
    _fake_claude(fake_bin, claude_state)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(claude_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "all"])
    assert harness_main() == 0
    capsys.readouterr()
    host_state = tmp_path / "state" / "harness" / "host-integrations.json"
    assert json.loads(host_state.read_text(encoding="utf-8"))["profiles"] == ["cursor"]

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "uninstall", "--host", "cursor", "--purge"],
    )
    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "--purge refused" in output
    assert claude_state.is_file()
    assert json.loads(host_state.read_text(encoding="utf-8"))["profiles"] == ["cursor"]
    assert default_runtime_paths().socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "all", "--purge"])
    assert harness_main() == 0
    capsys.readouterr()
