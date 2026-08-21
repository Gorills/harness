import sys

import pytest

import harness.entrypoints as entrypoints
from harness.doctor import DoctorReport
from harness.entrypoints import harness_main, harnessd_main


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
        "Doctor scope: SQLite runtime only; other checks are not implemented yet.",
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
        "Doctor scope: SQLite runtime only; other checks are not implemented yet.",
    ]


def test_harnessd_main_reports_bootstrap_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harnessd"])

    assert harnessd_main() == 0
    assert "Harness daemon runtime is not implemented yet." in capsys.readouterr().out
