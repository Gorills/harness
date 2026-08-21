import sqlite3
from dataclasses import dataclass

from harness.storage import fts5_available


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Runtime prerequisites currently checked by ``harness doctor``."""

    sqlite_version: str
    fts5_available: bool | None
    sqlite_error: str | None = None


def run_doctor_checks() -> DoctorReport:
    """Check implemented runtime prerequisites without creating durable Harness state."""
    try:
        connection = sqlite3.connect(":memory:", autocommit=True)
        try:
            has_fts5 = fts5_available(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return DoctorReport(
            sqlite_version=sqlite3.sqlite_version,
            fts5_available=None,
            sqlite_error=str(exc),
        )

    return DoctorReport(sqlite_version=sqlite3.sqlite_version, fts5_available=has_fts5)
