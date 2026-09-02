from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from fake_hosts import path_without_agent, write_fake_codex, write_fake_cursor_agent

from harness.builtin_skills import BUILTIN_SKILLS
from harness.codex_adapter import CODEX_BOOTSTRAP_INSTRUCTION_BODY, codex_developer_instructions
from harness.daemon_autostart import DaemonAutostartError
from harness.entrypoints import harness_main
from harness.installation import (
    InstallationError,
    _preflight_skill_registry_purge,
    install_harness,
    uninstall_harness,
)
from harness.registry import VisibilityMode, create_project, register_workspace
from harness.runtime_paths import default_runtime_paths
from harness.storage import connect_database, initialize_database
from harness.visibility import set_project_visibility

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
    (home / ".harness").chmod(0o700)
    (home / ".harness" / "skills").chmod(0o700)
    (skill / "SKILL.md").write_text(
        "---\nname: python-helper\ndescription: Python conventions\n---\n\n# Python helper\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text(
        "id: python-helper\napplies:\n  languages:\n    - python\n",
        encoding="utf-8",
    )


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
    write_fake_cursor_agent(fake_bin, tmp_path / "agent-state.json")
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(tmp_path / "agent-state.json"))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    install_output = capsys.readouterr().out
    assert "MCP registration: changed" in install_output
    expected = len(BUILTIN_SKILLS)
    assert f"Built-in skills: {expected} (installed {expected}, updated 0)" in install_output
    assert (home / ".harness" / "skills" / "testing-strategy" / "SKILL.md").is_file()
    assert "Harness install: OK" in install_output
    integration_state = state_home / "harness" / "host-integrations.json"
    assert json.loads(integration_state.read_text(encoding="utf-8"))["profiles"] == ["cursor"]

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    assert "MCP registration: unchanged" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    scan_output = capsys.readouterr().out
    projected_skill_count = 6
    assert f"Relevant skills: {projected_skill_count}" in scan_output
    assert (repo / ".agents" / "skills" / "python-helper" / "SKILL.md").exists()
    language_skill = repo / ".agents" / "skills" / "language-engineering"
    assert (language_skill / "references" / "python.md").exists()
    secure_skill = repo / ".agents" / "skills" / "secure-by-design"
    assert (secure_skill / "references" / "verification.md").exists()
    testing_skill = repo / ".agents" / "skills" / "testing-strategy"
    assert (testing_skill / "SKILL.md").exists()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Daemon: OK" in doctor_output
    assert "Cursor MCP registration: OK" in doctor_output
    assert "Claude Code MCP registration" not in doctor_output
    assert "Projects: OK" in doctor_output
    assert "Index state: OK" in doctor_output
    assert "Generated skills: OK" in doctor_output
    assert "Dashboard: OK" in doctor_output or (
        "Dashboard: WARN (daemon is running but dashboard listener is not; "
        "expected 127.0.0.1:17373)" in doctor_output
    )
    assert "Stale integrations: OK" in doctor_output
    assert "0 FAIL" in doctor_output

    paths = default_runtime_paths()
    assert paths.database.exists()
    assert paths.socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall"])
    assert harness_main() == 0
    uninstall_output = capsys.readouterr().out
    assert f"Generated skills removed: {projected_skill_count}" in uninstall_output
    assert "Project Intelligence: preserved" in uninstall_output
    assert not integration_state.exists()
    assert not (repo / ".agents" / "skills" / "python-helper").exists()
    assert not language_skill.exists()
    assert not secure_skill.exists()
    assert not testing_skill.exists()
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


def test_install_harness_refuses_retired_claude_code() -> None:
    with pytest.raises(InstallationError, match="no longer a supported Harness host"):
        install_harness(host="claude-code")
    with pytest.raises(InstallationError, match="no longer a supported Harness host"):
        uninstall_harness(host="claude-code")


def test_install_reports_daemon_prepare_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _skill_registry(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PATH", path_without_agent())

    def fail_daemon(*_args: object, **_kwargs: object) -> None:
        raise DaemonAutostartError(
            "Harness daemon exited before becoming ready (exit 1): No module named uvicorn"
        )

    monkeypatch.setattr("harness.installation.ensure_canonical_daemon", fail_daemon)
    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "Harness daemon could not be prepared" in output
    assert "No module named uvicorn" in output


def test_install_foreign_registration_fails_before_daemon_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    cursor_config = home / ".cursor" / "mcp.json"
    cursor_config.parent.mkdir(parents=True)
    cursor_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness": {
                        "type": "stdio",
                        "command": "/foreign/tool",
                        "args": ["serve"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 1
    assert "non-Harness MCP server" in capsys.readouterr().out
    assert not (state_home / "harness" / "harness.db").exists()
    assert not (runtime_home / "harness" / "harness.sock").exists()
    assert (
        json.loads(cursor_config.read_text(encoding="utf-8"))["mcpServers"]["harness"]["command"]
        == "/foreign/tool"
    )


def test_install_refuses_unsafe_canonical_database_before_daemon_or_registration_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

    paths = default_runtime_paths()
    paths.database.parent.mkdir(mode=0o700, parents=True)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"user-data")
    paths.database.symlink_to(outside)

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 1
    assert "unsafe database state" in capsys.readouterr().out
    assert not (state_home / "harness" / "host-integrations.json").exists()
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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    capsys.readouterr()
    paths = default_runtime_paths()
    assert paths.database.exists()
    assert paths.socket.exists()
    integration_state = state_home / "harness" / "host-integrations.json"
    registration_before = integration_state.read_bytes()

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
    assert integration_state.read_bytes() == registration_before
    assert paths.database.exists()
    assert paths.socket.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"

    (harness_home / "skills").unlink()
    monkeypatch.setattr(sys, "argv", ["harness", "uninstall"])
    assert harness_main() == 0
    capsys.readouterr()


def test_purge_preflight_refuses_group_writable_skill_registry_before_uninstall_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    registry = home / ".harness" / "skills"
    registry.mkdir(parents=True)
    (home / ".harness").chmod(0o700)
    registry.chmod(0o700)
    sentinel = registry / "user-owned.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    registry.chmod(0o770)
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }

    with pytest.raises(InstallationError, match="unsafe skill registry"):
        _preflight_skill_registry_purge(environment)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert stat.S_IMODE(registry.stat().st_mode) & 0o022


def test_purge_preflight_allows_missing_skill_registry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _preflight_skill_registry_purge({"HOME": str(home)})
    assert not (home / ".harness" / "skills").exists()


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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    capsys.readouterr()
    paths = default_runtime_paths()
    integration_state = state_home / "harness" / "host-integrations.json"
    registration_before = integration_state.read_bytes()
    unsafe_candidate = paths.database.with_name(f"{paths.database.name}-journal")
    unsafe_candidate.mkdir()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--purge"])
    assert harness_main() == 1
    assert "unsafe database state" in capsys.readouterr().out
    assert integration_state.read_bytes() == registration_before
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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent())

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


def test_codex_install_scan_uninstall_owns_only_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 0
    install_output = capsys.readouterr().out
    assert "Harness host: codex" in install_output
    assert "Codex project overrides changed: 0" in install_output
    assert json.loads(
        (state_home / "harness" / "host-integrations.json").read_text(encoding="utf-8")
    )["profiles"] == ["codex"]
    assert not (home / ".codex" / "config.toml").exists()

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    scan_output = capsys.readouterr().out
    assert "Codex restart required" in scan_output
    assert "fully quit and reopen" in scan_output
    assert "create a new task" in scan_output
    assert "existing tasks keep their original instruction snapshot" in scan_output
    assert "Codex trust" in scan_output
    assert "`project_status` must be the first project action" in scan_output
    config = tomllib.loads((repo / ".codex" / "config.toml").read_text(encoding="utf-8"))
    entry = config["mcp_servers"]["harness"]
    assert entry["url"].startswith("http://127.0.0.1:")
    assert entry["url"].endswith("/mcp")
    assert entry["required"] is True
    assert entry["http_headers"]["X-Harness-Workspace-Root"] == str(repo.resolve())
    assert entry["http_headers"]["Authorization"].startswith("Bearer ")
    assert "command" not in entry
    assert "cwd" not in entry
    assert "env" not in entry
    assert (repo / ".agents" / "skills" / "python-helper" / "SKILL.md").is_file()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Codex adapter: OK" in doctor_output
    assert "Codex host integration: OK" in doctor_output
    assert "Codex project MCP configs: OK (1 current" in doctor_output

    config_path = repo / ".codex" / "config.toml"
    stale_url = "http://127.0.0.1:1/mcp"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(entry["url"], stale_url),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 1
    broken_doctor = capsys.readouterr().out
    assert "Codex project MCP configs: FAIL" in broken_doctor
    assert f"configured endpoint: {stale_url}" in broken_doctor
    assert "remediation: harness install --host codex" in broken_doctor
    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 0
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "codex"])
    assert harness_main() == 0
    uninstall_output = capsys.readouterr().out
    assert "Codex project overrides changed: 1" in uninstall_output
    assert not (repo / ".codex" / "config.toml").exists()
    assert not (repo / ".codex" / ".harness-mcp-owner.json").exists()
    assert not (repo / ".agents" / "skills" / "python-helper").exists()
    assert not (state_home / "harness" / "host-integrations.json").exists()


def test_codex_install_preserves_registered_source_checkout_without_projecting_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)

    codex_path = repo / ".codex" / "config.toml"
    codex_path.parent.mkdir()
    codex_text = f"""developer_instructions = {json.dumps(CODEX_BOOTSTRAP_INSTRUCTION_BODY)}

[mcp_servers.harness-dev]
command = "./scripts/dogfood"
args = ["mcp"]
required = true
experimental_environment = "local"

[mcp_servers.harness-dev.env]
HARNESS_WORKSPACE_ROOT = "."
"""
    codex_path.write_text(codex_text, encoding="utf-8")
    cursor_path = repo / ".cursor" / "mcp.json"
    cursor_path.parent.mkdir()
    cursor_text = (
        json.dumps(
            {
                "mcpServers": {
                    "harness-dev": {
                        "type": "stdio",
                        "command": "${workspaceFolder}/scripts/dogfood",
                        "args": ["mcp"],
                        "env": {"HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"},
                    }
                }
            }
        )
        + "\n"
    )
    cursor_path.write_text(cursor_text, encoding="utf-8")
    _git(repo, "add", ".codex/config.toml", ".cursor/mcp.json")
    _git(
        repo,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "commit",
        "-m",
        "source checkout overlays",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    paths = default_runtime_paths()
    paths.database.parent.mkdir(parents=True, mode=0o700)
    paths.database.parent.chmod(0o700)
    initialize_database(paths.database)
    connection = connect_database(paths.database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=repo)
    finally:
        connection.close()

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Codex project overrides changed: 0" in output
    assert codex_path.read_text(encoding="utf-8") == codex_text
    assert cursor_path.read_text(encoding="utf-8") == cursor_text
    assert not (repo / ".agents" / "skills" / "python-helper").exists()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Codex project MCP configs: OK (0 current, 1 isolated-development" in doctor_output
    assert "Generated skills: OK (0 current, 0 stale" in doctor_output
    assert "1 source-checkout overlay(s) skipped" in doctor_output


def test_install_all_installs_codex_and_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    write_fake_cursor_agent(fake_bin, tmp_path / "agent-state.json")
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(tmp_path / "agent-state.json"))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "all"])
    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Harness host: all" in output
    assert "Harness install: OK" in output
    assert json.loads(
        (state_home / "harness" / "host-integrations.json").read_text(encoding="utf-8")
    )["profiles"] == ["codex", "cursor"]
    assert default_runtime_paths().database.exists()
    assert default_runtime_paths().socket.exists()


def test_codex_install_supports_existing_hidden_project_without_changing_agents_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    repo = tmp_path / "repo"
    _repo(repo)
    agents = repo / "AGENTS.md"
    agents.write_text("# User-owned project instructions\n", encoding="utf-8")
    agents_before = agents.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    paths = default_runtime_paths()
    paths.database.parent.mkdir(parents=True, mode=0o700)
    initialize_database(paths.database)
    connection = connect_database(paths.database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=repo)
        set_project_visibility(
            connection,
            mode=VisibilityMode.HIDDEN,
            host_profiles=("codex",),
            project_id=project.project_id,
        )
    finally:
        connection.close()

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Harness host: codex" in output
    state = json.loads(
        (state_home / "harness" / "host-integrations.json").read_text(encoding="utf-8")
    )
    assert state["profiles"] == ["codex"]
    config = tomllib.loads((repo / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert config["developer_instructions"] == codex_developer_instructions(hidden=True)
    assert agents.read_bytes() == agents_before
    assert paths.socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Codex project MCP configs: OK" in doctor_output
    assert "Hidden projection: OK" in doctor_output
    assert "Codex does not host-block git commit, push, or pull requests" in doctor_output


def test_codex_install_hidden_user_config_collision_fails_before_host_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    repo = tmp_path / "repo"
    _repo(repo)
    config = repo / ".codex" / "config.toml"
    config.parent.mkdir()
    original = b'model = "user-owned"\n'
    config.write_bytes(original)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    paths = default_runtime_paths()
    paths.database.parent.mkdir(parents=True, mode=0o700)
    initialize_database(paths.database)
    connection = connect_database(paths.database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=repo)
        set_project_visibility(
            connection,
            mode=VisibilityMode.HIDDEN,
            host_profiles=("cursor",),
            project_id=project.project_id,
        )
    finally:
        connection.close()

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "existing .codex/config.toml is user-owned" in output
    assert config.read_bytes() == original
    assert not (state_home / "harness" / "host-integrations.json").exists()
    assert not paths.socket.exists()


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


def test_multi_host_codex_cursor_install_scan_uninstall_preserves_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    agent_state = tmp_path / "agent-state.json"
    write_fake_cursor_agent(fake_bin, agent_state)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(agent_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
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
    assert host_state["profiles"] == ["codex", "cursor"]

    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    capsys.readouterr()
    assert (repo / ".agents" / "skills" / "python-helper" / "SKILL.md").is_file()
    assert not (repo / ".claude" / "skills" / "python-helper").exists()
    assert (repo / ".codex" / "config.toml").is_file()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    doctor_output = capsys.readouterr().out
    assert "Claude Code MCP registration" not in doctor_output
    assert "Cursor MCP registration: OK" in doctor_output
    assert "no user-harness" in doctor_output
    assert "Cursor project MCP overrides: OK" in doctor_output
    assert "Cursor project MCP tools: OK" in doctor_output
    assert "Codex host integration: OK" in doctor_output
    assert "Generated skills: OK" in doctor_output

    broken_project = json.loads(project_config.read_text(encoding="utf-8"))
    broken_project["mcpServers"]["harness"]["command"] = "/stale/cursor/python"
    project_config.write_text(json.dumps(broken_project), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 1
    broken_doctor = capsys.readouterr().out
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
    assert (repo / ".codex" / "config.toml").is_file()
    assert paths.socket.exists()
    assert (repo / ".agents" / "skills" / "python-helper" / "SKILL.md").is_file()

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    assert harness_main() == 0
    after = capsys.readouterr().out
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
    assert not (repo / ".codex" / "config.toml").exists()
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
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(agent_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
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


def test_cursor_install_skips_deleted_registered_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    agent_state = tmp_path / "agent-state.json"
    write_fake_cursor_agent(fake_bin, agent_state)
    _skill_registry(home)
    kept = tmp_path / "kept"
    gone = tmp_path / "gone"
    _repo(kept)
    _repo(gone)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("HARNESS_FAKE_AGENT_STATE", str(agent_state))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()
    gone_workspace_id = ""
    for root in (kept, gone):
        monkeypatch.setattr(sys, "argv", ["harness", "scan", str(root)])
        assert harness_main() == 0
        scan_output = capsys.readouterr().out
        if root == gone:
            for line in scan_output.splitlines():
                if line.startswith("Workspace: "):
                    gone_workspace_id = line.split()[1]
                    break
    shutil.rmtree(gone)

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    install_output = capsys.readouterr().out
    assert "Unavailable workspaces skipped: 1" in install_output
    assert gone_workspace_id
    assert f"{gone_workspace_id} ({gone.resolve()})" in install_output
    assert "Harness install: OK" in install_output
    assert (kept / ".cursor" / "mcp.json").is_file()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "cursor"])
    assert harness_main() == 0
    uninstall_output = capsys.readouterr().out
    assert "Unavailable workspaces skipped: 1" in uninstall_output
    assert "Harness uninstall: OK" in uninstall_output
    assert not (kept / ".cursor" / "mcp.json").exists()


def test_uninstall_codex_reprojects_skills_for_remaining_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    write_fake_codex(fake_bin)
    _skill_registry(home)
    repo = tmp_path / "repo"
    _repo(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_home))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(repo)])
    assert harness_main() == 0
    capsys.readouterr()
    assert (repo / ".agents" / "skills" / "python-helper").is_dir()
    assert (repo / ".codex" / "config.toml").is_file()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "codex"])
    assert harness_main() == 0
    capsys.readouterr()
    assert not (repo / ".codex" / "config.toml").exists()
    assert not (home / ".cursor" / "mcp.json").exists() or "harness" not in json.loads(
        (home / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    ).get("mcpServers", {})
    assert (repo / ".cursor" / "mcp.json").is_file()
    assert json.loads(
        (state_home / "harness" / "host-integrations.json").read_text(encoding="utf-8")
    )["profiles"] == ["cursor"]
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
    write_fake_codex(fake_bin)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PATH", path_without_agent(fake_bin))

    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "codex"])
    assert harness_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 0
    capsys.readouterr()
    host_state = tmp_path / "state" / "harness" / "host-integrations.json"
    assert json.loads(host_state.read_text(encoding="utf-8"))["profiles"] == [
        "codex",
        "cursor",
    ]

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "uninstall", "--host", "cursor", "--purge"],
    )
    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "--purge refused" in output
    assert json.loads(host_state.read_text(encoding="utf-8"))["profiles"] == [
        "codex",
        "cursor",
    ]
    assert default_runtime_paths().socket.exists()

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "all", "--purge"])
    assert harness_main() == 0
    capsys.readouterr()
