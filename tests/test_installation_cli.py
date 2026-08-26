from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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
    (skill / "SKILL.md").write_text(
        "# Python helper\n\nUse Python conventions.\n", encoding="utf-8"
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
        print('No MCP server found with name: "harness"')
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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    monkeypatch.setattr(sys, "argv", ["harness", "install"])
    assert harness_main() == 0
    install_output = capsys.readouterr().out
    assert "MCP registration: changed" in install_output
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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    harness_home.mkdir()
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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

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
