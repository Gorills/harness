import sqlite3
from pathlib import Path

import pytest

import harness.doctor as doctor
from harness.doctor import run_doctor_checks


def test_run_doctor_checks_is_ephemeral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    report = run_doctor_checks()

    assert report.sqlite_version == sqlite3.sqlite_version
    assert isinstance(report.fts5_available, bool)
    assert report.sqlite_error is None
    assert list(tmp_path.iterdir()) == []


def test_run_doctor_checks_normalizes_sqlite_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_probe(_: sqlite3.Connection) -> bool:
        raise sqlite3.OperationalError("probe failed")

    monkeypatch.setattr(doctor, "fts5_available", failed_probe)

    report = run_doctor_checks()

    assert report.sqlite_version == sqlite3.sqlite_version
    assert report.fts5_available is None
    assert report.sqlite_error == "probe failed"
