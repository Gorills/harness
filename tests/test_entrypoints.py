import sys
from pathlib import Path

import pytest

import harness.entrypoints as entrypoints
from harness.doctor import DoctorReport
from harness.entrypoints import harness_main, harnessd_main
from harness.storage import DatabaseStatus


def test_harness_main_lists_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harness"])

    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Product runtime is under implementation." in output
    assert "doctor" in output


def test_harness_doctor_reports_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def successful_checks() -> DoctorReport:
        return DoctorReport(sqlite_version="3.50.4", fts5_available=True)

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", successful_checks)

    assert harness_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: OK (version 3.50.4)",
        "FTS5: OK",
        "Doctor scope: SQLite runtime only; pass --database PATH to inspect an initialized "
        "Harness database.",
    ]


def test_harness_doctor_fails_when_fts5_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_fts5() -> DoctorReport:
        return DoctorReport(sqlite_version="3.50.4", fts5_available=False)

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", missing_fts5)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "SQLite runtime: OK (version 3.50.4)" in output
    assert "FTS5: FAIL" in output


def test_harness_doctor_reports_sqlite_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed_checks() -> DoctorReport:
        return DoctorReport(
            sqlite_version="3.50.4",
            fts5_available=None,
            sqlite_error="probe failed",
        )

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", failed_checks)

    assert harness_main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: FAIL (probe failed)",
        "FTS5: UNKNOWN",
        "Doctor scope: SQLite runtime only; pass --database PATH to inspect an initialized "
        "Harness database.",
    ]


def test_harness_doctor_inspects_selected_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "harness.db"

    def successful_checks(path: Path) -> DoctorReport:
        assert path == database_path
        return DoctorReport(
            sqlite_version="3.50.4",
            fts5_available=True,
            database_status=DatabaseStatus(
                schema_version=1,
                sqlite_version="3.50.4",
                journal_mode="wal",
                foreign_keys=True,
                fts5_available=True,
            ),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "doctor", "--database", str(database_path)],
    )
    monkeypatch.setattr(entrypoints, "run_doctor_checks", successful_checks)

    assert harness_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: OK (version 3.50.4)",
        "FTS5: OK",
        f"Database: OK ({database_path})",
        "Database schema: 1",
        "Database journal mode: wal",
        "Database foreign keys: OK",
        "Database FTS5: OK",
        "Doctor scope: SQLite runtime + selected initialized database; other checks are not "
        "implemented yet.",
    ]


def test_harness_doctor_reports_database_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing.db"

    def failed_checks(path: Path) -> DoctorReport:
        assert path == database_path
        return DoctorReport(
            sqlite_version="3.50.4",
            fts5_available=True,
            database_error="unable to open database file",
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "doctor", "--database", str(database_path)],
    )
    monkeypatch.setattr(entrypoints, "run_doctor_checks", failed_checks)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert f"Database: FAIL ({database_path}: unable to open database file)" in output


def test_harnessd_main_lists_bounded_serve_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harnessd"])

    assert harnessd_main() == 0
    output = capsys.readouterr().out
    assert "Broader product runtime is under implementation." in output
    assert "serve" in output


def test_harnessd_serve_dispatches_explicit_database_and_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    seen: list[tuple[Path, Path]] = []

    def run_daemon(database: Path, ipc_socket: Path) -> int:
        seen.append((database, ipc_socket))
        return 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harnessd",
            "serve",
            "--database",
            str(database_path),
            "--socket",
            str(socket_path),
        ],
    )
    monkeypatch.setattr(entrypoints, "_run_daemon", run_daemon)

    assert harnessd_main() == 0
    assert seen == [(database_path, socket_path)]
