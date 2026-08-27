from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

import harness.entrypoints as entrypoints
from harness.cursor_adapter import (
    find_isolated_development_root,
    is_isolated_development_overlay_entry,
)
from harness.entrypoints import harness_main
from harness.ipc import WorkspaceScanResult
from harness.runtime_paths import default_runtime_paths
from harness.storage import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX isolated-development slice")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SCRIPT = REPO_ROOT / "scripts" / "dev"
DEV_ENV_SCRIPT = REPO_ROOT / "scripts" / "dev-env.sh"
ISOLATED_DOC = REPO_ROOT / "docs" / "development" / "isolated-development.md"


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _console_script(name: str) -> Path:
    candidates = (
        REPO_ROOT / ".venv" / "bin" / name,
        Path(sys.executable).resolve().parent / name,
    )
    for script in candidates:
        if script.is_file():
            return script
    pytest.skip(f"{name} console script is not installed in the project environment")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _git_workspace(root: Path) -> Path:
    root.mkdir()
    _git(root, "init")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )
    return root


def _daemon_pids(socket_path: Path) -> set[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return set()
    runtime_dir = socket_path.parent.parent
    runtime_values = {
        os.fsencode(str(runtime_dir)),
        os.fsencode(str(runtime_dir.resolve())),
    }
    found: set[int] = set()
    for cmdline_path in proc.glob("[0-9]*/cmdline"):
        try:
            pid = int(cmdline_path.parent.name)
            command = cmdline_path.read_bytes()
        except (OSError, ValueError):
            continue
        if b"harness.daemon_process" not in command and b"harnessd" not in command:
            continue
        try:
            environ = (cmdline_path.parent / "environ").read_bytes()
        except OSError:
            continue
        selected = {
            entry.removeprefix(b"XDG_RUNTIME_DIR=")
            for entry in environ.split(b"\0")
            if entry.startswith(b"XDG_RUNTIME_DIR=")
        }
        if selected & runtime_values:
            found.add(pid)
    return found


def _stop_daemon(socket_path: Path) -> None:
    for pid in _daemon_pids(socket_path):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
    deadline = time.monotonic() + 3
    while _daemon_pids(socket_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    for pid in _daemon_pids(socket_path):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
    socket_path.unlink(missing_ok=True)


def test_isolated_development_doc_describes_the_working_workflow() -> None:
    text = ISOLATED_DOC.read_text(encoding="utf-8")
    for needle in (
        "scripts/dev sync",
        "scripts/dev harness doctor",
        "scripts/dev harness scan",
        "scripts/dev harness status",
        "scripts/dev stop",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        ".harness/state/harness/harness.db",
        ".harness/runtime/harness/harness.sock",
        "does not share that process, database, or Unix socket",
        ".cursor/mcp.json",
        "HARNESS_DEV_ROOT",
        "refused",
    ):
        assert needle in text


def test_dev_env_script_must_be_sourced() -> None:
    result = _run(["bash", str(DEV_ENV_SCRIPT)], cwd=REPO_ROOT)
    assert result.returncode == 1
    assert "Source this file" in result.stderr


def test_dev_env_script_keeps_caller_cwd_and_resolves_from_script_location(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run(
        [
            "bash",
            "-c",
            'source "$1" && pwd && printf "%s\\n" "$HARNESS_DEV_ROOT" '
            '"$XDG_STATE_HOME" "$XDG_RUNTIME_DIR"',
            "bash",
            str(DEV_ENV_SCRIPT),
        ],
        cwd=elsewhere,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == str(elsewhere)
    assert lines[1] == str(REPO_ROOT)
    assert lines[2] == str(REPO_ROOT / ".harness" / "state")
    assert lines[3] == str(REPO_ROOT / ".harness" / "runtime")
    assert stat.S_IMODE((REPO_ROOT / ".harness" / "state").stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE((REPO_ROOT / ".harness" / "runtime").stat().st_mode) & 0o077 == 0


def test_dev_wrapper_env_and_help_do_not_require_uv() -> None:
    help_result = _run([str(DEV_SCRIPT), "help"], cwd=REPO_ROOT)
    assert help_result.returncode == 0
    assert "scripts/dev harness doctor" in help_result.stdout
    assert "install/uninstall are refused" in help_result.stdout

    empty = _run([str(DEV_SCRIPT)], cwd=REPO_ROOT)
    assert empty.returncode == 1
    assert "Usage:" in empty.stderr

    env_result = _run([str(DEV_SCRIPT), "env"], cwd=Path("/tmp"))
    assert env_result.returncode == 0, env_result.stderr
    assert f"HARNESS_DEV_ROOT={REPO_ROOT}" in env_result.stdout
    assert f"XDG_STATE_HOME={REPO_ROOT / '.harness' / 'state'}" in env_result.stdout
    assert f"XDG_RUNTIME_DIR={REPO_ROOT / '.harness' / 'runtime'}" in env_result.stdout
    assert f"database={REPO_ROOT / '.harness' / 'state' / 'harness' / 'harness.db'}" in (
        env_result.stdout
    )
    assert f"socket={REPO_ROOT / '.harness' / 'runtime' / 'harness' / 'harness.sock'}" in (
        env_result.stdout
    )


def test_isolated_cli_autostart_does_not_touch_canonical_user_state(tmp_path: Path) -> None:
    harness = _console_script("harness")
    fake_home = tmp_path / "home"
    system_state = fake_home / ".local" / "state" / "harness"
    system_state.mkdir(parents=True)
    marker = system_state / "harness.db"
    marker.write_text("SYSTEM-INSTALL\n", encoding="utf-8")

    isolated_state = tmp_path / "dev-state"
    isolated_runtime = tmp_path / "dev-runtime"
    isolated_state.mkdir()
    isolated_runtime.mkdir()
    os.chmod(isolated_state, 0o700)
    os.chmod(isolated_runtime, 0o700)

    workspace = _git_workspace(tmp_path / "repo")
    paths = default_runtime_paths(
        environment={
            "XDG_STATE_HOME": str(isolated_state),
            "XDG_RUNTIME_DIR": str(isolated_runtime),
        }
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    decoy = fake_bin / "harness"
    decoy.write_text("#!/bin/sh\necho DECOY-SYSTEM-HARNESS >&2\nexit 99\n", encoding="utf-8")
    decoy.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["XDG_STATE_HOME"] = str(isolated_state)
    env["XDG_RUNTIME_DIR"] = str(isolated_runtime)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONPATH", None)

    try:
        doctor = _run([str(harness), "doctor"], cwd=workspace, env=env)
        assert doctor.returncode == 0, doctor.stdout + doctor.stderr
        assert "SQLite runtime: OK" in doctor.stdout
        assert "FTS5: OK" in doctor.stdout
        assert not paths.database.exists()

        scan = _run([str(harness), "scan", str(workspace)], cwd=tmp_path, env=env, timeout=60)
        assert scan.returncode == 0, scan.stdout + scan.stderr
        assert "created" in scan.stdout
        assert "Indexed files:" in scan.stdout
        assert str(workspace.resolve()) in scan.stdout

        status = _run([str(harness), "status", str(workspace)], cwd=tmp_path, env=env)
        assert status.returncode == 0, status.stdout + status.stderr
        assert "Indexed files:" in status.stdout
        assert str(workspace.resolve()) in status.stdout

        search = _run(
            [str(harness), "search", "tracked", str(workspace)],
            cwd=tmp_path,
            env=env,
        )
        assert search.returncode == 0, search.stdout + search.stderr
        assert "tracked.txt" in search.stdout

        inspect = _run(
            [str(harness), "doctor", "--database", str(paths.database)],
            cwd=tmp_path,
            env=env,
        )
        assert inspect.returncode == 0, inspect.stdout + inspect.stderr
        assert f"Database schema: {SCHEMA_VERSION}" in inspect.stdout
    finally:
        _stop_daemon(paths.socket)

    assert not _daemon_pids(paths.socket)
    assert marker.read_text(encoding="utf-8") == "SYSTEM-INSTALL\n"
    assert paths.database.is_file()
    assert paths.database.resolve() != marker.resolve()
    assert not (isolated_state / "harness.db").exists()


def test_dev_wrapper_runs_checkout_harness_not_path_decoy(tmp_path: Path) -> None:
    if not (REPO_ROOT / ".venv" / "bin" / "harness").is_file():
        pytest.skip("project environment is not synced")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    decoy = fake_bin / "harness"
    decoy.write_text("#!/bin/sh\necho DECOY-SYSTEM-HARNESS\nexit 99\n", encoding="utf-8")
    decoy.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = str(tmp_path / "home")
    env.pop("HARNESS_DEV_UV", None)

    result = _run([str(DEV_SCRIPT), "harness", "--version"], cwd=tmp_path, env=env, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DECOY-SYSTEM-HARNESS" not in result.stdout
    assert "DECOY-SYSTEM-HARNESS" not in result.stderr
    assert "0.1.0.dev0" in result.stdout


def _isolated_overlay() -> dict[str, object]:
    return {
        "mcpServers": {
            "harness": {
                "type": "stdio",
                "command": "${workspaceFolder}/scripts/dev",
                "args": ["harness", "mcp"],
                "env": {"HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"},
            }
        }
    }


def test_checkout_cursor_overlay_shadows_global_harness_with_scripts_dev() -> None:
    overlay = json.loads((REPO_ROOT / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    entry = overlay["mcpServers"]["harness"]
    assert is_isolated_development_overlay_entry(entry)
    assert find_isolated_development_root(REPO_ROOT) == REPO_ROOT
    claude = json.loads((REPO_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert claude["mcpServers"]["harness"]["command"] == "./scripts/dev"
    assert claude["mcpServers"]["harness"]["args"] == ["harness", "mcp"]


def test_canonical_scan_refuses_isolated_development_checkout_before_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _git_workspace(tmp_path / "repo")
    cursor = root / ".cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(json.dumps(_isolated_overlay()), encoding="utf-8")
    monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["harness", "scan", str(root)])

    def request_scan(_socket: Path, _path: Path) -> WorkspaceScanResult:
        raise AssertionError("canonical scan must not contact the daemon")

    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)
    monkeypatch.setattr(
        entrypoints,
        "_canonical_socket",
        lambda: (_ for _ in ()).throw(AssertionError("must not autostart")),
    )

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "Harness scan: FAIL" in output
    assert "isolated development" in output


def test_isolated_scan_of_overlay_root_skips_host_skill_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _git_workspace(tmp_path / "repo")
    cursor = root / ".cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(json.dumps(_isolated_overlay()), encoding="utf-8")
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(root))
    socket_path = tmp_path / "ipc" / "harness.sock"
    seen: list[tuple[Path, Path]] = []

    def request_scan(ipc_socket: Path, path: Path) -> WorkspaceScanResult:
        seen.append((ipc_socket, path))
        return WorkspaceScanResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=root.resolve(),
            project_created=True,
            workspace_created=True,
            file_count=1,
            added=1,
            updated=0,
            removed=0,
        )

    def skills_reconcile(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("isolated overlay scan must not project skills")

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "scan", str(root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_scan", request_scan)
    monkeypatch.setattr(entrypoints, "request_workspace_skills_reconcile", skills_reconcile)

    assert harness_main() == 0
    assert seen == [(socket_path, root.resolve())]
    output = capsys.readouterr().out
    assert "Host/skill reconciliation skipped: isolated-development checkout overlay" in output


def test_isolated_env_refuses_install_and_uninstall(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(REPO_ROOT))

    def install(**_kwargs: object) -> None:
        raise AssertionError("install must not run")

    def uninstall(**_kwargs: object) -> None:
        raise AssertionError("uninstall must not run")

    monkeypatch.setattr(entrypoints, "install_harness", install)
    monkeypatch.setattr(entrypoints, "uninstall_harness", uninstall)
    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "cursor"])
    assert harness_main() == 1
    assert "HARNESS_DEV_ROOT" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "all"])
    assert harness_main() == 1
    assert "HARNESS_DEV_ROOT" in capsys.readouterr().out


def test_dev_env_prepends_checkout_venv_when_present(tmp_path: Path) -> None:
    venv_bin = REPO_ROOT / ".venv" / "bin"
    if not venv_bin.is_dir():
        pytest.skip("project environment is not synced")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run(
        [
            "bash",
            "-c",
            'source "$1" && printf "%s\\n" "$PATH"',
            "bash",
            str(DEV_ENV_SCRIPT),
        ],
        cwd=elsewhere,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0].split(":")[0] == str(venv_bin)
