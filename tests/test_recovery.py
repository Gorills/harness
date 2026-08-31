from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest

import harness.recovery as recovery
from harness.daemon import serve_daemon
from harness.entrypoints import harness_main
from harness.ipc import IpcError, request_status
from harness.recovery import (
    RecoveryError,
    create_database_backup,
    restore_database_backup,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database


def _insert_project(database: Path, project_id: str) -> None:
    connection = connect_database(database)
    try:
        connection.execute(
            "INSERT INTO projects(id, visibility_mode) VALUES (?, 'normal')",
            (project_id,),
        )
    finally:
        connection.close()


def _project_ids(database: Path) -> list[str]:
    connection = connect_database(database)
    try:
        return [row[0] for row in connection.execute("SELECT id FROM projects ORDER BY id")]
    finally:
        connection.close()


def _rewrite_manifest(archive: Path, destination: Path, **updates: object) -> None:
    with zipfile.ZipFile(archive) as source:
        database = source.read("harness.db")
        manifest = json.loads(source.read("manifest.json"))
    manifest.update(updates)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("harness.db", database)
        target.writestr("manifest.json", json.dumps(manifest))


def test_backup_includes_committed_wal_content_and_restore_preserves_previous_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    archive = tmp_path / "state.harness-backup"
    initialize_database(source)
    initialize_database(target)
    _insert_project(target, "previous")

    live = connect_database(source)
    try:
        live.execute("INSERT INTO projects(id, visibility_mode) VALUES ('from-wal', 'normal')")
        result = create_database_backup(source, archive)
    finally:
        live.close()

    assert result.path == archive
    assert result.manifest.schema_version == SCHEMA_VERSION
    assert result.manifest.database_size_bytes > 0
    with zipfile.ZipFile(archive) as backup:
        assert sorted(backup.namelist()) == ["harness.db", "manifest.json"]

    restored = restore_database_backup(target, archive)
    assert _project_ids(target) == ["from-wal"]
    assert restored.previous_state_backup is not None
    assert restored.previous_state_backup.is_file()

    recovered_previous = tmp_path / "previous.db"
    restore_database_backup(recovered_previous, restored.previous_state_backup)
    assert _project_ids(recovered_previous) == ["previous"]


def test_backup_refuses_to_overwrite_existing_archive(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    archive = tmp_path / "state.harness-backup"
    initialize_database(database)
    archive.write_text("operator-owned", encoding="utf-8")

    with pytest.raises(RecoveryError, match="already exists"):
        create_database_backup(database, archive)

    assert archive.read_text(encoding="utf-8") == "operator-owned"


def test_restore_rejects_corrupted_database_payload_without_mutating_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    archive = tmp_path / "state.harness-backup"
    corrupted = tmp_path / "corrupted.harness-backup"
    initialize_database(source)
    initialize_database(target)
    _insert_project(target, "preserved")
    create_database_backup(source, archive)
    with zipfile.ZipFile(archive) as backup:
        manifest = backup.read("manifest.json")
        payload = bytearray(backup.read("harness.db"))
    payload[len(payload) // 2] ^= 0xFF
    with zipfile.ZipFile(corrupted, "w", compression=zipfile.ZIP_DEFLATED) as backup:
        backup.writestr("harness.db", payload)
        backup.writestr("manifest.json", manifest)

    with pytest.raises(RecoveryError, match="checksum"):
        restore_database_backup(target, corrupted)

    assert _project_ids(target) == ["preserved"]


def test_restore_rolls_back_current_database_if_post_install_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    archive = tmp_path / "state.harness-backup"
    initialize_database(source)
    initialize_database(target)
    _insert_project(source, "replacement")
    _insert_project(target, "preserved")
    create_database_backup(source, archive)
    real_validate = recovery._validate_database_connection

    def fail_post_install(
        connection: sqlite3.Connection,
        *,
        expected_schema: int,
    ) -> None:
        row = connection.execute("PRAGMA database_list").fetchone()
        if row is not None and Path(row[2]) == target:
            raise recovery.RecoveryError("synthetic post-install validation failure")
        real_validate(connection, expected_schema=expected_schema)

    monkeypatch.setattr(recovery, "_validate_database_connection", fail_post_install)

    with pytest.raises(RecoveryError, match="synthetic post-install"):
        restore_database_backup(target, archive)

    assert _project_ids(target) == ["preserved"]


def test_restore_validates_schema_and_runtime_identity_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    archive = tmp_path / "state.harness-backup"
    wrong_schema = tmp_path / "wrong-schema.harness-backup"
    wrong_runtime = tmp_path / "wrong-runtime.harness-backup"
    initialize_database(source)
    initialize_database(target)
    _insert_project(source, "restored")
    _insert_project(target, "preserved")
    create_database_backup(source, archive)
    _rewrite_manifest(archive, wrong_schema, schema_version=SCHEMA_VERSION - 1)
    _rewrite_manifest(archive, wrong_runtime, code_sha256="0" * 64)

    with pytest.raises(RecoveryError, match="schema"):
        restore_database_backup(target, wrong_schema)
    with pytest.raises(RecoveryError, match="runtime identity"):
        restore_database_backup(target, wrong_runtime)
    assert _project_ids(target) == ["preserved"]

    restore_database_backup(target, wrong_runtime, allow_runtime_mismatch=True)
    assert _project_ids(target) == ["restored"]


def test_backup_and_restore_cli_support_explicit_isolated_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "harness.db"
    socket = tmp_path / "runtime" / "harness.sock"
    archive = tmp_path / "state.harness-backup"
    initialize_database(database)
    _insert_project(database, "before")
    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "backup", str(archive), "--database", str(database)],
    )
    assert harness_main() == 0
    assert "Harness backup: OK" in capsys.readouterr().out

    connection = connect_database(database)
    try:
        connection.execute("DELETE FROM projects")
    finally:
        connection.close()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harness",
            "restore",
            str(archive),
            "--database",
            str(database),
            "--socket",
            str(socket),
        ],
    )
    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Harness restore: OK" in output
    assert "Previous state backup:" in output
    assert _project_ids(database) == ["before"]


def test_restore_cli_requires_socket_for_explicit_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "restore", str(tmp_path / "backup"), "--database", str(tmp_path / "db")],
    )

    assert harness_main() == 1
    assert "requires the matching --socket" in capsys.readouterr().out


def test_restore_cli_cleanly_stops_live_daemon_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    socket = tmp_path / "runtime" / "harness.sock"
    archive = tmp_path / "state.harness-backup"
    initialize_database(source)
    initialize_database(target)
    _insert_project(source, "replacement")
    _insert_project(target, "previous")
    create_database_backup(source, archive)
    stop = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, target, socket, stop_event=stop)
    deadline = monotonic() + 5
    while True:
        try:
            request_status(socket, timeout=0.1)
            break
        except IpcError:
            if monotonic() >= deadline:
                stop.set()
                executor.shutdown(wait=True)
                pytest.fail("test daemon did not start")
            sleep(0.01)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harness",
            "restore",
            str(archive),
            "--database",
            str(target),
            "--socket",
            str(socket),
        ],
    )
    try:
        assert harness_main() == 0
        future.result(timeout=5)
    finally:
        stop.set()
        executor.shutdown(wait=True)

    assert not socket.exists()
    assert _project_ids(target) == ["replacement"]
