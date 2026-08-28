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
    connect_database_read_only,
    initialize_database,
)


def test_connect_database_read_only_rejects_writes(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)

    connection = connect_database_read_only(database)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO projects(id) VALUES ('should-not-write')")
    finally:
        connection.close()


def test_connect_database_read_only_does_not_create_sidecars_for_quiescent_wal_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    connection = connect_database_read_only(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone() == (0,)
    finally:
        connection.close()

    status = storage.inspect_database(database)
    assert status.schema_version == SCHEMA_VERSION
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before
    assert not database.with_name(f"{database.name}-wal").exists()
    assert not database.with_name(f"{database.name}-shm").exists()


def test_connect_database_read_only_observes_uncheckpointed_live_wal_frames(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    writer = connect_database(database)
    try:
        writer.execute("INSERT INTO projects(id) VALUES ('live-project')")
        assert database.with_name(f"{database.name}-wal").exists()
        reader = connect_database_read_only(database)
        try:
            assert reader.execute("SELECT COUNT(*) FROM projects").fetchone() == (1,)
        finally:
            reader.close()
    finally:
        writer.close()


def test_connect_database_read_only_refuses_older_schema_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    current = storage.SCHEMA_VERSION
    assert current > 2
    monkeypatch.setattr(storage, "SCHEMA_VERSION", current - 1)
    initialize_database(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", current)

    with pytest.raises(InvalidSchemaStateError, match="initialize it before opening"):
        connect_database_read_only(database)

    connection = connect_database_read_only(database, allow_older_schema=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (0,)
    finally:
        connection.close()


def test_connect_database_read_only_refuses_newer_schema_even_when_older_allowed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    versions = tuple(range(1, SCHEMA_VERSION + 2))
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            [(version,) for version in versions],
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(UnsupportedSchemaVersionError):
        connect_database_read_only(database, allow_older_schema=True)


def test_connect_database_read_only_refuses_live_wal_without_existing_shm(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    writer = connect_database(database)
    shm = database.with_name(f"{database.name}-shm")
    try:
        writer.execute("INSERT INTO projects(id) VALUES ('live-project')")
        assert database.with_name(f"{database.name}-wal").exists()
        shm.unlink()

        with pytest.raises(storage.DatabaseError, match="existing SHM sidecar"):
            connect_database_read_only(database)
        assert not shm.exists()
    finally:
        writer.close()


def test_connect_database_read_only_refuses_symlinked_live_shm_without_touching_target(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    writer = connect_database(database)
    shm = database.with_name(f"{database.name}-shm")
    target = tmp_path / "outside-shm"
    try:
        writer.execute("INSERT INTO projects(id) VALUES ('live-project')")
        shm.unlink()
        target.write_bytes(b"user-data")
        shm.symlink_to(target)

        with pytest.raises(storage.DatabaseError, match="SHM sidecar is unsafe"):
            connect_database_read_only(database)
        assert target.read_bytes() == b"user-data"
    finally:
        shm.unlink(missing_ok=True)
        writer.close()


def test_connect_database_read_only_refuses_symlinked_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    link = tmp_path / "linked.db"
    link.symlink_to(database)

    with pytest.raises(storage.DatabaseError, match="real regular file"):
        connect_database_read_only(link)


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
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "projects",
            "workspaces",
            "indexed_files",
            "tasks",
            "task_baselines",
            "task_baseline_dirty_paths",
            "task_checkpoints",
            "task_checkpoint_changed_paths",
            "task_checkpoint_verification",
            "task_events",
        } <= tables
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
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
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
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
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
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
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
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        assert connection.execute("SELECT value FROM legacy_fixture").fetchone() == ("preserved",)
        for table in (
            "projects",
            "workspaces",
            "indexed_files",
            "tasks",
            "task_baselines",
            "task_baseline_dirty_paths",
        ):
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() == (table,)
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
        ).fetchall() == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        for table in (
            "indexed_files",
            "tasks",
            "task_baselines",
            "task_baseline_dirty_paths",
            "task_checkpoints",
            "task_checkpoint_changed_paths",
            "task_events",
        ):
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() == (table,)
    finally:
        connection.close()


def test_initialize_database_migrates_existing_version_three_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY CHECK (version > 0))"
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1), (2), (3)")
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
        connection.execute(
            """
            CREATE TABLE indexed_files (
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL CHECK (relative_path <> ''),
                kind TEXT NOT NULL CHECK (kind IN ('file', 'symlink')),
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
                PRIMARY KEY (workspace_id, relative_path)
            )
            """
        )
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace', 'project', '/repo', '/repo/.git')
            """
        )
        connection.execute(
            """
            INSERT INTO indexed_files(
                workspace_id, relative_path, kind, size_bytes, content_sha256
            ) VALUES ('workspace', 'README.md', 'file', 7, ?)
            """,
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT relative_path FROM indexed_files").fetchall() == [
            ("README.md",)
        ]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        for table in (
            "tasks",
            "task_baselines",
            "task_baseline_dirty_paths",
            "task_checkpoints",
            "task_checkpoint_changed_paths",
            "task_events",
        ):
            assert connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() == (table,)
    finally:
        connection.close()


def test_initialize_database_migrates_existing_version_four_database(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY CHECK (version > 0))"
        )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1), (2), (3), (4)")
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
        connection.execute(
            """
            CREATE TABLE indexed_files (
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL CHECK (relative_path <> ''),
                kind TEXT NOT NULL CHECK (kind IN ('file', 'symlink')),
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
                PRIMARY KEY (workspace_id, relative_path)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                title TEXT NOT NULL
                    CHECK (title <> '' AND length(CAST(title AS BLOB)) <= 256),
                state TEXT NOT NULL
                    CHECK (state IN ('working', 'waiting', 'completed', 'cancelled')),
                wait_reason TEXT,
                revision INTEGER NOT NULL CHECK (revision > 0),
                created_at TEXT NOT NULL CHECK (created_at <> ''),
                updated_at TEXT NOT NULL CHECK (updated_at <> ''),
                CHECK (
                    (
                        state = 'waiting'
                        AND wait_reason IS NOT NULL
                        AND wait_reason IN ('operator_review', 'operator_input', 'external')
                    )
                    OR (state <> 'waiting' AND wait_reason IS NULL)
                )
            )
            """
        )
        connection.execute("CREATE INDEX tasks_workspace_id_idx ON tasks(workspace_id)")
        connection.execute(
            """
            CREATE UNIQUE INDEX tasks_one_working_per_workspace_idx
            ON tasks(workspace_id)
            WHERE state = 'working'
            """
        )
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace', 'project', '/repo', '/repo/.git')
            """
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
            ) VALUES (
                'task', 'workspace', 'Existing task', 'waiting', 'external', 2, 'created', 'updated'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT id, state, revision FROM tasks").fetchall() == [
            ("task", "waiting", 2)
        ]
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        assert connection.execute("SELECT COUNT(*) FROM task_baselines").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_baseline_dirty_paths").fetchone() == (
            0,
        )
    finally:
        connection.close()


def test_initialize_database_rejects_newer_schema_without_changing_journal_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            ((version,) for version in range(1, SCHEMA_VERSION + 2)),
        )
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
        assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 2)]
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


def test_initialize_database_migrates_existing_version_five_checkpoint_foundation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project-v5')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace-v5', 'project-v5', '/v5/repo', '/v5/repo/.git')
            """
        )
        connection.execute(
            """
            INSERT INTO tasks(
                id, workspace_id, title, state, wait_reason, revision, created_at, updated_at
            ) VALUES (
                'task-v5', 'workspace-v5', 'Existing v5 task', 'working', NULL, 1,
                'created', 'updated'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO task_baselines(
                task_id, head, branch, captured_at, index_is_fresh,
                index_file_count, index_snapshot_sha256
            ) VALUES ('task-v5', NULL, 'main', 'captured', 1, 0, ?)
            """,
            ("0" * 64,),
        )
        connection.execute("DROP TABLE task_stack_hints")
        connection.execute("DROP TABLE knowledge_anchors")
        connection.execute("DROP TABLE knowledge_cards")
        connection.execute("DROP TABLE task_events")
        connection.execute("DROP TABLE task_checkpoint_changed_paths")
        connection.execute("DROP TABLE task_checkpoints")
        for (trigger_name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE '%_search_%'"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute("DROP TABLE task_search")
        connection.execute("DROP TABLE knowledge_search")
        connection.execute("DROP TABLE task_checkpoint_verification")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 6")
        connection.commit()
    finally:
        connection.close()

    status = initialize_database(database)

    assert status.schema_version == SCHEMA_VERSION
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT id, revision FROM tasks WHERE id = 'task-v5'"
        ).fetchone() == ("task-v5", 1)
        assert connection.execute(
            "SELECT task_id, branch FROM task_baselines WHERE task_id = 'task-v5'"
        ).fetchone() == ("task-v5", "main")
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
        assert connection.execute("SELECT COUNT(*) FROM task_checkpoints").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM task_events").fetchone() == (0,)
    finally:
        connection.close()
