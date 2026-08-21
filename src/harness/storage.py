from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import sleep

SCHEMA_VERSION = 1
_MIGRATIONS_TABLE = "schema_migrations"
_FTS5_PROBE_TABLE = "__harness_fts5_probe"
_WAL_LOCK_RETRY_ATTEMPTS = 5
_WAL_LOCK_RETRY_DELAY_SECONDS = 0.02


class DatabaseError(RuntimeError):
    """Base class for Harness database bootstrap errors."""


class UnsupportedSchemaVersionError(DatabaseError):
    """Raised when a database was created by a newer Harness schema."""


class InvalidSchemaStateError(DatabaseError):
    """Raised when migration metadata is missing or inconsistent."""


class WalModeUnavailableError(DatabaseError):
    """Raised when SQLite cannot enable WAL for the database."""


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """Observed runtime capabilities of an initialized Harness database."""

    schema_version: int
    sqlite_version: str
    journal_mode: str
    foreign_keys: bool
    fts5_available: bool


def initialize_database(path: Path) -> DatabaseStatus:
    """Create or migrate a Harness database and report its runtime capabilities."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    try:
        current_version = _read_schema_version(connection)
        _ensure_supported_schema(current_version)
        journal_mode = _enable_wal(connection)
        _apply_migrations(connection)
        return _status(connection, journal_mode=journal_mode)
    finally:
        connection.close()


def connect_database(path: Path) -> sqlite3.Connection:
    """Open an initialized Harness database with required connection settings."""
    connection = _connect(path, must_exist=True)
    try:
        current_version = _read_schema_version(connection)
        _ensure_supported_schema(current_version)
        if current_version != SCHEMA_VERSION:
            raise InvalidSchemaStateError(
                f"database schema is {current_version}; initialize it before opening"
            )
        _require_wal(connection)
    except Exception:
        connection.close()
        raise
    return connection


def inspect_database(path: Path) -> DatabaseStatus:
    """Inspect an initialized Harness database without creating or migrating it."""
    connection = connect_database(path)
    try:
        journal_mode = _journal_mode_from_row(connection.execute("PRAGMA journal_mode").fetchone())
        return _status(connection, journal_mode=journal_mode)
    finally:
        connection.close()


def fts5_available(connection: sqlite3.Connection) -> bool:
    """Return whether the runtime SQLite connection can create an FTS5 table."""
    try:
        connection.execute(f"CREATE VIRTUAL TABLE temp.{_FTS5_PROBE_TABLE} USING fts5(content)")
    except sqlite3.OperationalError as exc:
        if "no such module: fts5" in str(exc).lower():
            return False
        raise
    finally:
        connection.execute(f"DROP TABLE IF EXISTS temp.{_FTS5_PROBE_TABLE}")
    return True


def _connect(path: Path, *, must_exist: bool = False) -> sqlite3.Connection:
    database: str | Path = path
    uri = False
    if must_exist:
        database = f"{path.absolute().as_uri()}?mode=rw"
        uri = True

    connection = sqlite3.connect(database, uri=uri, autocommit=True)
    connection.execute("PRAGMA foreign_keys = ON")
    foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
    if foreign_keys_row != (1,):
        connection.close()
        raise DatabaseError("SQLite foreign key enforcement could not be enabled")
    return connection


def _read_schema_version(connection: sqlite3.Connection) -> int:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (_MIGRATIONS_TABLE,),
    ).fetchone()
    if table_exists is None:
        return 0

    try:
        rows = connection.execute(
            f"SELECT version FROM {_MIGRATIONS_TABLE} ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise InvalidSchemaStateError("schema migration metadata is unreadable") from exc

    versions: list[int] = []
    for row in rows:
        version = row[0]
        if not isinstance(version, int) or version <= 0:
            raise InvalidSchemaStateError("schema migration versions must be positive integers")
        versions.append(version)

    if not versions:
        raise InvalidSchemaStateError("schema migration metadata is empty")

    current_version = versions[-1]
    if versions != list(range(1, current_version + 1)):
        raise InvalidSchemaStateError("schema migration versions are not contiguous")
    return current_version


def _ensure_supported_schema(current_version: int) -> None:
    if current_version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"database schema {current_version} is newer than supported schema {SCHEMA_VERSION}"
        )


def _enable_wal(connection: sqlite3.Connection) -> str:
    last_lock_error: sqlite3.OperationalError | None = None
    for attempt in range(_WAL_LOCK_RETRY_ATTEMPTS):
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise WalModeUnavailableError("SQLite WAL mode could not be enabled") from exc
            last_lock_error = exc
            if attempt + 1 < _WAL_LOCK_RETRY_ATTEMPTS:
                sleep(_WAL_LOCK_RETRY_DELAY_SECONDS)
            continue

        journal_mode = _journal_mode_from_row(row)
        if journal_mode == "wal":
            return journal_mode
        raise WalModeUnavailableError(
            f"SQLite WAL mode is required; database reported journal_mode={journal_mode!r}"
        )

    raise WalModeUnavailableError(
        "SQLite WAL mode remained locked during initialization"
    ) from last_lock_error


def _require_wal(connection: sqlite3.Connection) -> None:
    journal_mode = _journal_mode_from_row(connection.execute("PRAGMA journal_mode").fetchone())
    if journal_mode != "wal":
        raise WalModeUnavailableError(
            f"SQLite WAL mode is required; database reported journal_mode={journal_mode!r}"
        )


def _journal_mode_from_row(row: tuple[object, ...] | None) -> str:
    if row is None or not isinstance(row[0], str):
        return ""
    return row[0].lower()


def _apply_migrations(connection: sqlite3.Connection) -> None:
    while True:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current_version = _read_schema_version(connection)
            _ensure_supported_schema(current_version)
            if current_version == SCHEMA_VERSION:
                connection.execute("COMMIT")
                return

            target_version = current_version + 1
            _apply_migration(connection, target_version)
            connection.execute(
                f"INSERT INTO {_MIGRATIONS_TABLE}(version) VALUES (?)",
                (target_version,),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _apply_migration(connection: sqlite3.Connection, target_version: int) -> None:
    if target_version == 1:
        connection.execute(
            f"CREATE TABLE {_MIGRATIONS_TABLE} (version INTEGER PRIMARY KEY CHECK (version > 0))"
        )
        return
    raise InvalidSchemaStateError(f"no migration registered for schema {target_version}")


def _status(connection: sqlite3.Connection, *, journal_mode: str) -> DatabaseStatus:
    current_version = _read_schema_version(connection)
    foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
    return DatabaseStatus(
        schema_version=current_version,
        sqlite_version=sqlite3.sqlite_version,
        journal_mode=journal_mode,
        foreign_keys=foreign_keys_row == (1,),
        fts5_available=fts5_available(connection),
    )
