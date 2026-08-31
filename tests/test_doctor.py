import json
import os
import shutil
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.doctor as doctor
from harness.doctor import run_doctor_checks
from harness.git_workspace import GitWorkspaceDeadlineExceededError
from harness.hidden_projection import apply_hidden_projection
from harness.host_adapters import HostIntegrationError, HostRegistrationState
from harness.index import IndexingError, ScanDeadlineExceededError
from harness.ipc import IpcRemoteError
from harness.registry import (
    VisibilityMode,
    WorkspaceRecord,
    create_project,
    register_workspace,
    update_project_visibility,
)
from harness.skill_runtime import SkillRuntimeError
from harness.skills import SkillProjectionError
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.visibility import set_project_visibility


def test_doctor_check_escapes_terminal_control_characters() -> None:
    check = doctor._check(
        "Cursor MCP registration",
        doctor.DoctorSeverity.FAIL,
        "configured Python: /tmp/evil\nFAKE: OK\x1b[2J\u202eruntime",
    )

    assert "\n" not in check.detail
    assert "\x1b" not in check.detail
    assert "\u202e" not in check.detail
    assert r"\n" in check.detail
    assert r"\x1b" in check.detail
    assert r"\u202e" in check.detail
    assert "FAKE: OK" in check.detail


def test_run_doctor_checks_is_ephemeral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    report = run_doctor_checks()

    assert report.sqlite_version == sqlite3.sqlite_version
    assert isinstance(report.fts5_available, bool)
    assert report.sqlite_error is None
    assert report.database_status is None
    assert report.database_error is None
    assert list(tmp_path.iterdir()) == []


def test_run_doctor_checks_normalizes_sqlite_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_probe(_: sqlite3.Connection) -> bool:
        raise sqlite3.OperationalError("probe failed")

    monkeypatch.setattr(doctor, "fts5_available", failed_probe)

    report = run_doctor_checks()

    assert report.sqlite_version == sqlite3.sqlite_version
    assert report.fts5_available is None
    assert report.sqlite_error == "probe failed"
    assert report.database_status is None
    assert report.database_error is None


def test_run_doctor_checks_inspects_initialized_database(tmp_path: Path) -> None:
    database_path = tmp_path / "harness.db"
    initialize_database(database_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    report = run_doctor_checks(database_path)

    assert report.sqlite_error is None
    assert report.database_error is None
    assert report.database_status is not None
    assert report.database_status.schema_version == SCHEMA_VERSION
    assert report.database_status.journal_mode == "wal"
    assert report.database_status.foreign_keys is True
    assert isinstance(report.database_status.fts5_available, bool)
    assert report.database_status.fts5_available == report.fts5_available
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_run_doctor_checks_does_not_create_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.db"

    report = run_doctor_checks(database_path)

    assert report.database_status is None
    assert report.database_error is not None
    assert database_path.exists() is False


def test_run_system_doctor_on_clean_machine_is_read_only_and_warning_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_RUNTIME_DIR": str(runtime_home),
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    report = doctor.run_system_doctor(environment=environment)

    assert report.failure_count == 0
    assert report.warning_count > 0
    assert not state_home.exists()
    assert not runtime_home.exists()
    assert not (home / ".harness").exists()
    by_name = {check.name: check for check in report.checks}
    assert by_name["Database"].severity is doctor.DoctorSeverity.WARN
    assert by_name["Daemon"].severity is doctor.DoctorSeverity.WARN
    assert by_name["Claude Code MCP registration"].severity is doctor.DoctorSeverity.WARN


def test_run_system_doctor_warns_when_claude_registration_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _AbsentClaude:
        executable = tmp_path / "claude"

        def registration_state(self) -> HostRegistrationState:
            return HostRegistrationState.ABSENT

    monkeypatch.setattr(doctor, "discover_claude_code_adapter", lambda **_kwargs: _AbsentClaude())
    home = tmp_path / "home"
    home.mkdir()
    report = doctor.run_system_doctor(
        environment={
            "HOME": str(home),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "PATH": os.environ.get("PATH", ""),
        }
    )
    by_name = {check.name: check for check in report.checks}
    assert by_name["Claude Code MCP registration"].severity is doctor.DoctorSeverity.WARN
    assert "absent" in by_name["Claude Code MCP registration"].detail


def test_run_system_doctor_fails_closed_on_unexpected_claude_inspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BrokenClaude:
        executable = tmp_path / "claude"

        def registration_state(self) -> HostRegistrationState:
            raise HostIntegrationError("Claude Code MCP inspection command failed with exit code 2")

    monkeypatch.setattr(doctor, "discover_claude_code_adapter", lambda **_kwargs: _BrokenClaude())
    home = tmp_path / "home"
    home.mkdir()
    report = doctor.run_system_doctor(
        environment={
            "HOME": str(home),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "PATH": os.environ.get("PATH", ""),
        }
    )
    by_name = {check.name: check for check in report.checks}
    assert by_name["Claude Code MCP registration"].severity is doctor.DoctorSeverity.FAIL
    assert report.failure_count >= 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX isolated-development overlay")
def test_run_system_doctor_reports_isolated_development_overlay_as_preserved(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    state_dir = state_home / "harness"
    state_dir.mkdir(parents=True, mode=0o700)

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    overlay = {
        "mcpServers": {
            "harness-dev": {
                "type": "stdio",
                "command": "${workspaceFolder}/scripts/dev",
                "args": ["harness", "mcp"],
                "env": {"HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"},
            }
        }
    }
    overlay_path = root / ".cursor" / "mcp.json"
    overlay_path.parent.mkdir()
    overlay_text = json.dumps(overlay) + "\n"
    overlay_path.write_text(overlay_text, encoding="utf-8")
    codex_overlay = root / ".codex" / "config.toml"
    codex_overlay.parent.mkdir()
    codex_text = """[mcp_servers.harness-dev]
command = "./scripts/dogfood"
args = ["mcp"]
startup_timeout_sec = 30
required = true

[mcp_servers.harness-dev.env]
HARNESS_WORKSPACE_ROOT = "."
"""
    codex_overlay.write_text(codex_text, encoding="utf-8")
    (root / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "init",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )

    initialize_database(state_dir / "harness.db")
    connection = connect_database(state_dir / "harness.db")
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=root)
    finally:
        connection.close()

    report = doctor.run_system_doctor(
        environment={
            "HOME": str(home),
            "XDG_STATE_HOME": str(state_home),
            "XDG_RUNTIME_DIR": str(runtime_home),
            "PATH": os.environ.get("PATH", ""),
        }
    )
    overlay_checks = [
        check for check in report.checks if check.name.startswith("Cursor project MCP override ")
    ]
    assert overlay_checks
    assert all(check.severity is doctor.DoctorSeverity.OK for check in overlay_checks)
    assert any("isolated-development overlay" in check.detail for check in overlay_checks)
    summary = next(check for check in report.checks if check.name == "Cursor project MCP overrides")
    assert summary.severity is doctor.DoctorSeverity.OK
    assert "isolated-development" in summary.detail
    assert overlay_path.read_text(encoding="utf-8") == overlay_text
    assert all(
        "Cursor project" not in check.name
        for check in report.checks
        if check.severity is doctor.DoctorSeverity.FAIL
    )
    codex_checks = [
        check for check in report.checks if check.name.startswith("Codex project MCP config ")
    ]
    assert codex_checks
    assert all(check.severity is doctor.DoctorSeverity.OK for check in codex_checks)
    assert any("isolated-development overlay" in check.detail for check in codex_checks)
    codex_summary = next(
        check for check in report.checks if check.name == "Codex project MCP configs"
    )
    assert codex_summary.severity is doctor.DoctorSeverity.OK
    assert "isolated-development" in codex_summary.detail
    assert codex_overlay.read_text(encoding="utf-8") == codex_text


def test_run_system_doctor_refuses_database_symlink_without_following_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    state_dir = state_home / "harness"
    state_dir.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"keep")
    (state_dir / "harness.db").symlink_to(outside)
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_RUNTIME_DIR": str(runtime_home),
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    report = doctor.run_system_doctor(environment=environment)

    assert report.failure_count >= 1
    by_name = {check.name: check for check in report.checks}
    assert by_name["Database files"].severity is doctor.DoctorSeverity.FAIL
    assert "unsafe database state" in by_name["Database files"].detail
    assert outside.read_bytes() == b"keep"
    assert (state_dir / "harness.db").is_symlink()


def test_run_system_doctor_warns_on_stale_sqlite_sidecar_without_main_database(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    state_dir = state_home / "harness"
    state_dir.mkdir(parents=True, mode=0o700)
    (state_dir / "harness.db-journal").write_bytes(b"stale")
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_RUNTIME_DIR": str(runtime_home),
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    report = doctor.run_system_doctor(environment=environment)

    by_name = {check.name: check for check in report.checks}
    assert by_name["Database files"].severity is doctor.DoctorSeverity.WARN
    assert "stale sidecars" in by_name["Database files"].detail
    assert by_name["Stale integrations"].severity is doctor.DoctorSeverity.WARN
    assert (state_dir / "harness.db-journal").read_bytes() == b"stale"


def test_run_system_doctor_refuses_group_writable_skill_registry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    registry = home / ".harness" / "skills"
    registry.mkdir(parents=True)
    registry.chmod(0o770)
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    report = doctor.run_system_doctor(environment=environment)

    by_name = {check.name: check for check in report.checks}
    assert by_name["Skill registry permissions"].severity is doctor.DoctorSeverity.FAIL
    assert report.failure_count >= 1


def test_run_system_doctor_reports_relative_home_as_failure_without_mutation(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    runtime_home = tmp_path / "runtime"
    environment = {
        "HOME": "relative-home",
        "XDG_STATE_HOME": str(state_home),
        "XDG_RUNTIME_DIR": str(runtime_home),
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    report = doctor.run_system_doctor(environment=environment)

    by_name = {check.name: check for check in report.checks}
    assert by_name["Skill registry permissions"].severity is doctor.DoctorSeverity.FAIL
    assert "home directory must be absolute" in by_name["Skill registry permissions"].detail
    assert report.failure_count >= 1
    assert not state_home.exists()
    assert not runtime_home.exists()


def test_inspect_daemon_identifies_previous_protocol_v1_runtime_actionably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "harness.sock"
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.bind(str(socket_path))
    socket_path.chmod(0o600)
    checks: list[doctor.DoctorCheck] = []
    stale_notes: list[str] = []
    monkeypatch.setattr(
        doctor,
        "request_runtime_diagnostics",
        lambda _socket: (_ for _ in ()).throw(
            IpcRemoteError("invalid_request", "IPC request is invalid")
        ),
    )
    try:
        result = doctor._inspect_daemon(
            socket_path,
            runtime_dir_ok=True,
            python_executable=None,
            checks=checks,
            stale_notes=stale_notes,
        )
    finally:
        peer.close()

    assert result is None
    assert checks[-1].name == "Daemon"
    assert checks[-1].severity is doctor.DoctorSeverity.FAIL
    assert "predates runtime diagnostics" in checks[-1].detail
    assert "harness install" in checks[-1].detail
    assert "legacy daemon runtime" in stale_notes


def test_run_system_doctor_normalizes_project_registry_database_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    state_dir = state_home / "harness"
    state_dir.mkdir(parents=True, mode=0o700)
    database = state_dir / "harness.db"
    initialize_database(database)
    environment = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    monkeypatch.setattr(
        doctor,
        "list_workspaces",
        lambda _connection, **_kwargs: (_ for _ in ()).throw(
            sqlite3.DatabaseError("registry broken")
        ),
    )

    report = doctor.run_system_doctor(environment=environment)

    by_name = {check.name: check for check in report.checks}
    assert by_name["Projects"].severity is doctor.DoctorSeverity.FAIL
    assert "registry broken" in by_name["Projects"].detail
    assert by_name["Index state"].severity is doctor.DoctorSeverity.WARN
    assert by_name["Generated skills"].severity is doctor.DoctorSeverity.WARN
    assert database.is_file()


def _doctor_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    state_home = tmp_path / "state"
    (state_home / "harness").mkdir(parents=True, mode=0o700)
    return {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state_home),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "PATH": os.environ.get("PATH", ""),
    }


def _git_repository(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "init",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def _register_doctor_workspaces(
    environment: dict[str, str], roots: list[Path]
) -> list[WorkspaceRecord]:
    database = Path(environment["XDG_STATE_HOME"]) / "harness" / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        return [
            register_workspace(connection, project_id=project.project_id, path=root)
            for root in roots
        ]
    finally:
        connection.close()


def _checks_by_name(report: doctor.SystemDoctorReport) -> dict[str, doctor.DoctorCheck]:
    return {check.name: check for check in report.checks}


def test_doctor_workspace_budgets_remain_finite() -> None:
    assert 0 < doctor._DOCTOR_WORKSPACE_DEADLINE_SECONDS < float("inf")
    assert 0 < doctor._DOCTOR_WORKSPACE_TOTAL_SECONDS < float("inf")
    assert doctor._DOCTOR_WORKSPACE_DEADLINE_SECONDS <= doctor._DOCTOR_WORKSPACE_TOTAL_SECONDS
    assert doctor._DOCTOR_WORKSPACE_LIMIT >= 11
    assert doctor._DOCTOR_WORKSPACE_DEADLINE_SECONDS >= 30.0
    assert doctor._DOCTOR_WORKSPACE_TOTAL_SECONDS >= 90.0


def test_isolated_doctor_inspects_development_skill_profiles(tmp_path: Path) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    _register_doctor_workspaces(environment, [root])
    registry = tmp_path / "skills"
    registry.mkdir(mode=0o700)
    environment.update(
        {
            "HARNESS_DEV_ROOT": str(tmp_path),
            "HARNESS_DEV_SKILL_PROFILES": "codex,cursor",
            "HARNESS_SKILL_REGISTRY": str(registry),
        }
    )

    report = doctor.run_system_doctor(environment=environment)

    skills = _checks_by_name(report)["Generated skills"]
    assert skills.severity is doctor.DoctorSeverity.OK
    assert "1 current" in skills.detail


def test_doctor_labels_index_timeout_with_workspace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    monkeypatch.setattr(
        doctor,
        "inspect_workspace_index_freshness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ScanDeadlineExceededError("Workspace scan deadline exceeded")
        ),
    )

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    index = by_name["Index state"]
    stale = by_name["Stale integrations"]
    assert index.severity is doctor.DoctorSeverity.WARN
    assert "timed out" in index.detail
    assert "unavailable" not in index.detail
    assert workspace.workspace_id in index.detail
    assert str(workspace.workspace_root) in index.detail
    assert f"timed out index inspection {workspace.workspace_id}" in stale.detail
    assert str(workspace.workspace_root) in stale.detail
    assert report.failure_count == 0


def test_doctor_labels_skill_timeout_with_workspace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _CurrentClaude:
        executable = tmp_path / "claude"

        def registration_state(self) -> HostRegistrationState:
            return HostRegistrationState.CURRENT

    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    monkeypatch.setattr(doctor, "discover_claude_code_adapter", lambda **_kwargs: _CurrentClaude())

    def fail_skills(*_args: object, **_kwargs: object) -> None:
        raise SkillRuntimeError(
            "Workspace skill integration could not be inspected"
        ) from SkillProjectionError("skill projection inspection deadline exceeded")

    monkeypatch.setattr(doctor, "inspect_workspace_skills", fail_skills)

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    skills = by_name["Generated skills"]
    stale = by_name["Stale integrations"]
    assert skills.severity is doctor.DoctorSeverity.WARN
    assert "timed out" in skills.detail
    assert "unavailable" not in skills.detail
    assert workspace.workspace_id in skills.detail
    assert str(workspace.workspace_root) in skills.detail
    assert f"timed out generated-skill inspection {workspace.workspace_id}" in stale.detail
    assert report.failure_count == 0


def test_doctor_labels_index_inspection_failure_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    monkeypatch.setattr(
        doctor,
        "inspect_workspace_index_freshness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IndexingError("Workspace changed while scanning: README.md")
        ),
    )

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    index = by_name["Index state"]
    stale = by_name["Stale integrations"]
    assert index.severity is doctor.DoctorSeverity.WARN
    assert "1 failed" in index.detail
    assert "unavailable" not in index.detail
    assert workspace.workspace_id in index.detail
    assert str(workspace.workspace_root) in index.detail
    assert f"failed index inspection {workspace.workspace_id}" in stale.detail
    assert report.failure_count == 0


def test_doctor_unavailable_is_only_git_identity_failure(tmp_path: Path) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "missing-checkout")
    [workspace] = _register_doctor_workspaces(environment, [root])
    shutil.rmtree(root)

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    projects = by_name["Projects"]
    index = by_name["Index state"]
    skills = by_name["Generated skills"]
    stale = by_name["Stale integrations"]
    assert projects.severity is doctor.DoctorSeverity.WARN
    assert "1 unavailable" in projects.detail
    assert "timed out" not in projects.detail
    assert workspace.workspace_id in projects.detail
    assert str(workspace.workspace_root) in projects.detail
    assert "unavailable" not in index.detail
    assert "unavailable" not in skills.detail
    assert workspace.workspace_id not in index.detail
    assert f"unavailable Workspace {workspace.workspace_id}" in stale.detail
    assert report.failure_count == 0


def test_doctor_identity_timeout_is_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    monkeypatch.setattr(
        doctor,
        "inspect_git_workspace_runtime_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GitWorkspaceDeadlineExceededError("Git workspace inspection deadline exceeded")
        ),
    )

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    projects = by_name["Projects"]
    index = by_name["Index state"]
    stale = by_name["Stale integrations"]
    assert projects.severity is doctor.DoctorSeverity.WARN
    assert "1 timed out" in projects.detail
    assert "0 unavailable" in projects.detail
    assert workspace.workspace_id in projects.detail
    assert str(workspace.workspace_root) in projects.detail
    assert "unavailable" not in index.detail
    assert f"timed out Workspace identity {workspace.workspace_id}" in stale.detail
    assert report.failure_count == 0


def test_doctor_truncation_names_skipped_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _doctor_environment(tmp_path)
    first = _git_repository(tmp_path / "first")
    second = _git_repository(tmp_path / "second")
    workspaces = _register_doctor_workspaces(environment, [first, second])
    monkeypatch.setattr(doctor, "_DOCTOR_WORKSPACE_LIMIT", 1)

    report = doctor.run_system_doctor(environment=environment)

    inspectable_id = min(workspace.workspace_id for workspace in workspaces)
    skipped = next(
        workspace for workspace in workspaces if workspace.workspace_id != inspectable_id
    )
    by_name = _checks_by_name(report)
    projects = by_name["Projects"]
    index = by_name["Index state"]
    skills = by_name["Generated skills"]
    stale = by_name["Stale integrations"]
    assert projects.severity is doctor.DoctorSeverity.WARN
    assert index.severity is doctor.DoctorSeverity.WARN
    assert "doctor budget" in projects.detail
    assert skipped.workspace_id in projects.detail
    assert str(skipped.workspace_root) in projects.detail
    assert skipped.workspace_id in index.detail
    assert "unavailable" not in index.detail
    assert "unavailable" not in skills.detail
    assert f"doctor budget skipped Workspace {skipped.workspace_id}" in stale.detail
    assert report.failure_count == 0


def test_doctor_aggregate_deadline_still_expires_and_names_remaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _doctor_environment(tmp_path)
    first = _git_repository(tmp_path / "first")
    second = _git_repository(tmp_path / "second")
    workspaces = _register_doctor_workspaces(environment, [first, second])
    monkeypatch.setattr(doctor, "_DOCTOR_WORKSPACE_TOTAL_SECONDS", 0.0)

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    projects = by_name["Projects"]
    index = by_name["Index state"]
    stale = by_name["Stale integrations"]
    assert projects.severity is doctor.DoctorSeverity.WARN
    assert "doctor budget" in projects.detail
    assert "unavailable" not in index.detail
    for workspace in workspaces:
        assert workspace.workspace_id in projects.detail
        assert str(workspace.workspace_root) in projects.detail
        assert workspace.workspace_id in index.detail
        assert f"doctor budget skipped Workspace {workspace.workspace_id}" in stale.detail
    assert report.failure_count == 0


def test_doctor_per_workspace_deadline_still_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    monkeypatch.setattr(doctor, "_DOCTOR_WORKSPACE_DEADLINE_SECONDS", 0.0)

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    projects = by_name["Projects"]
    index = by_name["Index state"]
    stale = by_name["Stale integrations"]
    assert projects.severity is doctor.DoctorSeverity.WARN
    assert "1 timed out" in projects.detail
    assert "0 unavailable" in projects.detail
    assert workspace.workspace_id in projects.detail
    assert str(workspace.workspace_root) in projects.detail
    assert "unavailable" not in index.detail
    assert f"timed out Workspace identity {workspace.workspace_id}" in stale.detail
    assert report.failure_count == 0


def _write_host_profiles(environment: dict[str, str], *profiles: str) -> None:
    database = Path(environment["XDG_STATE_HOME"]) / "harness" / "harness.db"
    path = database.parent / "host-integrations.json"
    path.write_text(
        json.dumps({"version": 1, "profiles": list(profiles)}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_doctor_reports_hidden_projection_and_cursor_scm_gap(tmp_path: Path) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    _write_host_profiles(environment, "cursor")
    database = Path(environment["XDG_STATE_HOME"]) / "harness" / "harness.db"
    connection = connect_database(database)
    try:
        set_project_visibility(
            connection,
            mode=VisibilityMode.HIDDEN,
            host_profiles=("cursor",),
            project_id=workspace.project_id,
        )
    finally:
        connection.close()

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    assert by_name["Hidden projection"].severity is doctor.DoctorSeverity.OK
    assert "ignored Harness-owned instructions" in by_name["Hidden projection"].detail
    assert by_name["Hidden SCM enforcement"].severity is doctor.DoctorSeverity.WARN
    assert "does not host-block" in by_name["Hidden SCM enforcement"].detail


def test_doctor_warns_on_hidden_leftovers_in_normal_mode(tmp_path: Path) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    _register_doctor_workspaces(environment, [root])
    apply_hidden_projection((root,), ("cursor",))

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    assert by_name["Hidden leftovers"].severity is doctor.DoctorSeverity.WARN
    assert "still have Harness-owned Hidden instructions" in by_name["Hidden leftovers"].detail


def test_doctor_fails_when_hidden_instructions_are_missing(tmp_path: Path) -> None:
    environment = _doctor_environment(tmp_path)
    root = _git_repository(tmp_path / "repo")
    [workspace] = _register_doctor_workspaces(environment, [root])
    _write_host_profiles(environment, "cursor")
    database = Path(environment["XDG_STATE_HOME"]) / "harness" / "harness.db"
    connection = connect_database(database)
    try:
        update_project_visibility(connection, workspace.project_id, VisibilityMode.HIDDEN)
    finally:
        connection.close()

    report = doctor.run_system_doctor(environment=environment)

    by_name = _checks_by_name(report)
    assert by_name["Hidden projection"].severity is doctor.DoctorSeverity.FAIL
    assert "incomplete" in by_name["Hidden projection"].detail
    assert report.failure_count >= 1
