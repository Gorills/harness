import sqlite3
from dataclasses import dataclass
from pathlib import Path

from harness.storage import (
    DatabaseError,
    DatabaseStatus,
    fts5_available,
    inspect_database,
)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Runtime and optional persisted-database checks performed by ``harness doctor``."""

    sqlite_version: str
    fts5_available: bool | None
    sqlite_error: str | None = None
    database_status: DatabaseStatus | None = None
    database_error: str | None = None


def run_doctor_checks(database_path: Path | None = None) -> DoctorReport:
    """Check implemented prerequisites without creating or migrating durable Harness state."""
    sqlite_error: str | None = None
    has_fts5: bool | None = None
    try:
        connection = sqlite3.connect(":memory:", autocommit=True)
        try:
            has_fts5 = fts5_available(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        sqlite_error = str(exc)

    database_status: DatabaseStatus | None = None
    database_error: str | None = None
    if database_path is not None:
        try:
            database_status = inspect_database(database_path)
        except (DatabaseError, sqlite3.Error, OSError) as exc:
            database_error = str(exc)

    return DoctorReport(
        sqlite_version=sqlite3.sqlite_version,
        fts5_available=has_fts5,
        sqlite_error=sqlite_error,
        database_status=database_status,
        database_error=database_error,
    )
