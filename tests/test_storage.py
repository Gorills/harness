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
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [(1,), (2,), (3,)]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert {"projects", "workspaces", "indexed_files"} <= tables
    finally:
        connection.close()


def test_initialize_database_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"

    first = initialize_database(database)
    second = initialize_database(database)

    assert second == first
    connection = sqlite3.connect(database)
    try:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [(1,), (2,), (3,)]
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
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [(1,), (2,), (3,)]
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
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [(1,), (2,), (3,)]
    finally:
        connection.close()


def test_initialize_database_migrates_existing_version_one_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY CHECK (version > 0))"
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute("CREATE TABLE legacy_fixture (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_fixture(value) VALUES ('preserved')")
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    try:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [(1,), (2,), (3,)]
        assert connection.execute("SELECT value FROM legacy_fixture").fetchone() == ("preserved",)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'projects'"
        ).fetchone() == ("projects",)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'workspaces'"
        ).fetchone() == ("workspaces",)
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'indexed_files'"
        ).fetchone() == ("indexed_files",)
    finally:
        connection.close()


def test_initialize_database_migrates_existing_version_two_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY CHECK (version > 0))"
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1), (2)")
        connection.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                visibility_mode TEXT NOT NULL DEFAULT 'normal'
                    CHECK (visibility_mode IN ('normal', 'hidden'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                workspace_root TEXT NOT NULL UNIQUE,
                git_common_dir TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX workspaces_project_id_idx ON workspaces(project_id)")
        connection.execute(
            "CREATE INDEX workspaces_git_common_dir_idx ON workspaces(git_common_dir)"
        )
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace', 'project', '/repo', '/repo/.git')
            """
        )
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("project",)]
        assert connection.execute("SELECT id FROM workspaces").fetchall() == [("workspace",)]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = 'indexed_files'"
        ).fetchone() == ("indexed_files",)
    finally:
        connection.close()


def test_initialize_database_rejects_newer_schema_without_changing_journal_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1), (2), (3), (4)")
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
        assert versions == [(1,), (2,), (3,), (4,)]
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
