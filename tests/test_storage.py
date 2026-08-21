import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock, get_ident

import pytest

import harness.storage as storage
from harness.storage import (
    SCHEMA_VERSION,
    InvalidSchemaStateError,
    UnsupportedSchemaVersionError,
    connect_database,
    initialize_database,
)


def test_initialize_database_creates_wal_schema_and_reports_capabilities(tmp_path: Path) -> None:
    database = tmp_path / "state" / "harness.db"

    status = initialize_database(database)

    assert database.is_file()
    assert status.schema_version == SCHEMA_VERSION
    assert status.journal_mode == "wal"
    assert status.foreign_keys is True
    assert isinstance(status.fts5_available, bool)
    assert status.sqlite_version == sqlite3.sqlite_version

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    finally:
        connection.close()


def test_initialize_database_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"

    first = initialize_database(database)
    second = initialize_database(database)

    assert second == first
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    finally:
        connection.close()


def test_initialize_database_serializes_concurrent_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "harness.db"
    barrier = Barrier(2)
    seen_threads: set[int] = set()
    seen_lock = Lock()
    original_read_schema_version = storage._read_schema_version

    def synchronized_first_read(connection: sqlite3.Connection) -> int:
        version = original_read_schema_version(connection)
        thread_id = get_ident()
        with seen_lock:
            first_read = thread_id not in seen_threads
            seen_threads.add(thread_id)
        if first_read:
            barrier.wait(timeout=5)
        return version

    monkeypatch.setattr(storage, "_read_schema_version", synchronized_first_read)

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(initialize_database, (database, database)))

    assert statuses[0].schema_version == SCHEMA_VERSION
    assert statuses[1].schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    finally:
        connection.close()


def test_connect_database_enforces_foreign_keys(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)

    connection = connect_database(database)
    try:
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (parent_id INTEGER NOT NULL REFERENCES parent(id))")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child(parent_id) VALUES (1)")
    finally:
        connection.close()


def test_initialize_database_migrates_existing_version_zero_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE legacy_fixture (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_fixture(value) VALUES ('preserved')")
        connection.commit()
    finally:
        connection.close()

    initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM legacy_fixture").fetchone() == ("preserved",)
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    finally:
        connection.close()


def test_initialize_database_rejects_newer_schema_without_changing_journal_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1), (2)")
        connection.commit()
        before = connection.execute("PRAGMA journal_mode").fetchone()
    finally:
        connection.close()

    with pytest.raises(UnsupportedSchemaVersionError):
        initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == before
        versions = connection.execute("SELECT version FROM schema_migrations").fetchall()
        assert versions == [(1,), (2,)]
    finally:
        connection.close()


def test_initialize_database_rejects_non_contiguous_migration_history(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(InvalidSchemaStateError, match="not contiguous"):
        initialize_database(database)
