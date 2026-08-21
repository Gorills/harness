import sqlite3
from pathlib import Path

import pytest

from harness.storage import (
    InvalidSchemaStateError,
    connect_database,
    initialize_database,
    inspect_database,
)


def test_connect_database_does_not_create_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"

    with pytest.raises(sqlite3.OperationalError):
        connect_database(database)

    assert not database.exists()


def test_inspect_database_reports_initialized_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    expected = initialize_database(database)

    assert inspect_database(database) == expected


def test_inspect_database_does_not_create_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"

    with pytest.raises(sqlite3.OperationalError):
        inspect_database(database)

    assert not database.exists()


def test_inspect_database_does_not_migrate_uninitialized_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE legacy_fixture (value TEXT NOT NULL)")
        connection.commit()
        before_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()

    with pytest.raises(InvalidSchemaStateError, match="initialize it before opening"):
        inspect_database(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == before_journal_mode
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()
