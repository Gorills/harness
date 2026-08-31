from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from harness.daemon import hold_database_maintenance_lock
from harness.runtime_identity import RuntimeIdentity, current_runtime_identity
from harness.storage import SCHEMA_VERSION, connect_database

BACKUP_FORMAT_VERSION = 1
_DATABASE_ENTRY = "harness.db"
_MANIFEST_ENTRY = "manifest.json"
_MANIFEST_MAX_BYTES = 64 * 1024
_COPY_CHUNK_BYTES = 128 * 1024
_MAX_BACKUP_DATABASE_BYTES = 16 * 1024 * 1024 * 1024


class RecoveryError(RuntimeError):
    """Raised when a durable-state backup or restore cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    schema_version: int
    package_version: str
    code_sha256: str
    created_at: str
    database_sha256: str
    database_size_bytes: int


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    manifest: BackupManifest


@dataclass(frozen=True, slots=True)
class RestoreResult:
    database_path: Path
    manifest: BackupManifest
    previous_state_backup: Path | None


def create_database_backup(database_path: Path, output_path: Path) -> BackupResult:
    """Create an atomic, consistent SQLite backup archive, including live WAL content."""
    output = output_path.absolute()
    _prepare_new_output(output)
    identity = current_runtime_identity()
    with tempfile.TemporaryDirectory(prefix="harness-backup-") as temporary:
        snapshot = Path(temporary) / _DATABASE_ENTRY
        source = connect_database(database_path)
        destination = sqlite3.connect(snapshot, autocommit=True)
        try:
            source.backup(destination)
            _validate_database_connection(destination, expected_schema=SCHEMA_VERSION)
        except (OSError, sqlite3.DatabaseError) as exc:
            raise RecoveryError("Harness database backup could not be created") from exc
        finally:
            destination.close()
            source.close()

        database_sha256, database_size = _hash_file(snapshot)
        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            schema_version=SCHEMA_VERSION,
            package_version=identity.package_version,
            code_sha256=identity.code_sha256,
            created_at=datetime.now(UTC).isoformat(),
            database_sha256=database_sha256,
            database_size_bytes=database_size,
        )
        temporary_archive = _new_sibling_file(output, prefix=".harness-backup-")
        try:
            with zipfile.ZipFile(
                temporary_archive,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.write(snapshot, _DATABASE_ENTRY)
                archive.writestr(
                    _MANIFEST_ENTRY,
                    json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")),
                )
            os.chmod(temporary_archive, 0o600)
            _install_new_file(temporary_archive, output)
        except (OSError, zipfile.BadZipFile) as exc:
            raise RecoveryError("Harness backup archive could not be written") from exc
        finally:
            _unlink_if_exists(temporary_archive)
    return BackupResult(path=output, manifest=manifest)


def restore_database_backup(
    database_path: Path,
    archive_path: Path,
    *,
    allow_runtime_mismatch: bool = False,
) -> RestoreResult:
    """Validate and atomically restore an archive while the daemon is stopped."""
    identity = current_runtime_identity()
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = _new_sibling_file(database_path, prefix=".harness-restore-")
    previous_backup: Path | None = None
    try:
        manifest = _extract_validated_archive(
            archive_path,
            staged,
            identity=identity,
            allow_runtime_mismatch=allow_runtime_mismatch,
        )
        _prepare_restored_database(staged, expected_schema=manifest.schema_version)
        with hold_database_maintenance_lock(database_path):
            _require_database_artifacts_safe(database_path)
            if database_path.exists():
                previous_backup = _unused_previous_backup_path(database_path)
                create_database_backup(database_path, previous_backup)
            _replace_database_with_rollback(database_path, staged)
    except RecoveryError:
        raise
    except (OSError, sqlite3.DatabaseError, zipfile.BadZipFile) as exc:
        raise RecoveryError("Harness database restore could not be completed") from exc
    finally:
        _unlink_if_exists(staged)
        _remove_database_sidecars(staged)
    return RestoreResult(
        database_path=database_path,
        manifest=manifest,
        previous_state_backup=previous_backup,
    )


def _extract_validated_archive(
    archive_path: Path,
    destination: Path,
    *,
    identity: RuntimeIdentity,
    allow_runtime_mismatch: bool,
) -> BackupManifest:
    _require_regular_file(archive_path, label="backup archive")
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if sorted(names) != [_DATABASE_ENTRY, _MANIFEST_ENTRY] or len(set(names)) != 2:
                raise RecoveryError("Harness backup archive has an invalid entry set")
            manifest_info = archive.getinfo(_MANIFEST_ENTRY)
            if manifest_info.file_size > _MANIFEST_MAX_BYTES:
                raise RecoveryError("Harness backup manifest exceeds its size limit")
            manifest = _parse_manifest(archive.read(manifest_info))
            _validate_manifest(
                manifest,
                identity=identity,
                allow_runtime_mismatch=allow_runtime_mismatch,
            )
            database_info = archive.getinfo(_DATABASE_ENTRY)
            if database_info.file_size != manifest.database_size_bytes:
                raise RecoveryError("Harness backup database size does not match its manifest")
            digest = hashlib.sha256()
            copied = 0
            with archive.open(database_info, mode="r") as source, destination.open("wb") as target:
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > manifest.database_size_bytes:
                        raise RecoveryError("Harness backup database exceeds its declared size")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if (
                copied != manifest.database_size_bytes
                or digest.hexdigest() != manifest.database_sha256
            ):
                raise RecoveryError("Harness backup database checksum does not match its manifest")
    except RecoveryError:
        raise
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise RecoveryError("Harness backup archive is unreadable") from exc
    return manifest


def _parse_manifest(raw: bytes) -> BackupManifest:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("Harness backup manifest is invalid JSON") from exc
    expected = {field.name for field in BackupManifest.__dataclass_fields__.values()}
    if not isinstance(value, dict) or set(value) != expected:
        raise RecoveryError("Harness backup manifest fields do not match the format")
    if any(
        isinstance(value[name], bool)
        for name in ("format_version", "schema_version", "database_size_bytes")
    ):
        raise RecoveryError("Harness backup manifest has invalid numeric fields")
    try:
        return BackupManifest(**value)
    except TypeError as exc:
        raise RecoveryError("Harness backup manifest values do not match the format") from exc


def _validate_manifest(
    manifest: BackupManifest,
    *,
    identity: RuntimeIdentity,
    allow_runtime_mismatch: bool,
) -> None:
    if manifest.format_version != BACKUP_FORMAT_VERSION:
        raise RecoveryError(f"Harness backup format {manifest.format_version!r} is unsupported")
    if manifest.schema_version != SCHEMA_VERSION:
        raise RecoveryError(
            f"Harness backup schema {manifest.schema_version!r} does not match supported schema {SCHEMA_VERSION}"
        )
    if (
        not isinstance(manifest.database_size_bytes, int)
        or manifest.database_size_bytes <= 0
        or manifest.database_size_bytes > _MAX_BACKUP_DATABASE_BYTES
    ):
        raise RecoveryError("Harness backup database size is invalid")
    for label, value, limit in (
        ("package version", manifest.package_version, 128),
        ("created timestamp", manifest.created_at, 64),
    ):
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
            raise RecoveryError(f"Harness backup {label} is invalid")
    for label, value in (
        ("runtime fingerprint", manifest.code_sha256),
        ("database checksum", manifest.database_sha256),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise RecoveryError(f"Harness backup {label} is invalid")
    try:
        created_at = datetime.fromisoformat(manifest.created_at)
    except ValueError as exc:
        raise RecoveryError("Harness backup created timestamp is invalid") from exc
    if created_at.tzinfo is None:
        raise RecoveryError("Harness backup created timestamp must include a UTC offset")
    if not allow_runtime_mismatch and (
        manifest.package_version != identity.package_version
        or manifest.code_sha256 != identity.code_sha256
    ):
        raise RecoveryError(
            "Harness backup runtime identity does not match the current runtime; "
            "use --allow-runtime-mismatch only after compatibility review"
        )


def _prepare_restored_database(path: Path, *, expected_schema: int) -> None:
    try:
        connection = sqlite3.connect(path, autocommit=True)
        try:
            _validate_database_connection(connection, expected_schema=expected_schema)
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise RecoveryError("restored Harness database could not enable WAL mode")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RecoveryError("restored Harness database is invalid") from exc


def _validate_database_connection(
    connection: sqlite3.Connection,
    *,
    expected_schema: int,
) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise RecoveryError("Harness backup database failed SQLite integrity_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RecoveryError("Harness backup database failed SQLite foreign_key_check")
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RecoveryError("Harness backup schema metadata is unreadable") from exc
    versions = [row[0] for row in rows]
    if versions != list(range(1, expected_schema + 1)):
        raise RecoveryError("Harness backup schema metadata is inconsistent")


def _prepare_new_output(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RecoveryError("Harness backup destination could not be inspected") from exc
    raise RecoveryError(f"Harness backup destination already exists: {path}")


def _new_sibling_file(path: Path, *, prefix: str) -> Path:
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    except OSError as exc:
        raise RecoveryError("Harness recovery temporary file could not be created") from exc
    os.close(descriptor)
    return Path(raw_path)


def _install_new_file(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise RecoveryError(f"Harness backup destination already exists: {destination}") from exc
    except OSError as exc:
        raise RecoveryError("Harness backup archive could not be installed atomically") from exc
    _fsync_directory(destination.parent)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise RecoveryError("Harness backup snapshot could not be read") from exc
    return digest.hexdigest(), size


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryError(f"Harness {label} could not be inspected") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise RecoveryError(f"Harness {label} must be a current-user regular file with one link")
    return metadata


def _require_database_artifacts_safe(database_path: Path) -> None:
    for candidate in (
        database_path,
        database_path.with_name(f"{database_path.name}-wal"),
        database_path.with_name(f"{database_path.name}-shm"),
    ):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        _require_regular_file(candidate, label="database artifact")


def _replace_database_with_rollback(database_path: Path, staged: Path) -> None:
    rollback: Path | None = None
    original_moved = False
    installed = False
    try:
        if database_path.exists():
            rollback = _new_sibling_file(database_path, prefix=".harness-restore-rollback-")
            os.replace(database_path, rollback)
            original_moved = True
        _remove_database_sidecars(database_path)
        os.replace(staged, database_path)
        installed = True
        os.chmod(database_path, 0o600)
        _fsync_directory(database_path.parent)
        restored = connect_database(database_path)
        try:
            _validate_database_connection(restored, expected_schema=SCHEMA_VERSION)
        finally:
            restored.close()
    except Exception:
        if installed:
            _remove_database_sidecars(database_path)
            _unlink_if_exists(database_path)
        if original_moved and rollback is not None:
            try:
                os.replace(rollback, database_path)
                _fsync_directory(database_path.parent)
            except OSError as rollback_error:
                raise RecoveryError(
                    f"Harness restore rollback failed; previous database is preserved at {rollback}"
                ) from rollback_error
            rollback = None
        elif rollback is not None:
            _unlink_if_exists(rollback)
        raise
    else:
        if rollback is not None:
            _unlink_if_exists(rollback)


def _remove_database_sidecars(database_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        _unlink_if_exists(database_path.with_name(f"{database_path.name}{suffix}"))


def _unused_previous_backup_path(database_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for counter in range(1000):
        suffix = "" if counter == 0 else f"-{counter}"
        candidate = database_path.with_name(
            f"{database_path.name}.pre-restore-{timestamp}{suffix}.harness-backup"
        )
        if not candidate.exists():
            return candidate
    raise RecoveryError("Harness could not allocate a pre-restore backup path")


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
