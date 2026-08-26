import socket
import sqlite3
from pathlib import Path

import pytest

import harness.doctor as doctor
from harness.doctor import run_doctor_checks
from harness.ipc import IpcRemoteError
from harness.storage import SCHEMA_VERSION, initialize_database


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
    assert by_name["MCP registration"].severity is doctor.DoctorSeverity.WARN


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
