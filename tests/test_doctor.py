import sqlite3
from pathlib import Path

import pytest

import harness.doctor as doctor
from harness.doctor import run_doctor_checks
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

    report = run_doctor_checks(database_path)

    assert report.sqlite_error is None
    assert report.database_error is None
    assert report.database_status is not None
    assert report.database_status.schema_version == SCHEMA_VERSION
    assert report.database_status.journal_mode == "wal"
    assert report.database_status.foreign_keys is True
    assert report.database_status.fts5_available is True


def test_run_doctor_checks_does_not_create_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.db"

    report = run_doctor_checks(database_path)

    assert report.database_status is None
    assert report.database_error is not None
    assert database_path.exists() is False
