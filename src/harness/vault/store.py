from __future__ import annotations

import hashlib
import os
import secrets
import stat
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

from pykeepass import PyKeePass, create_database  # type: ignore[import-untyped]
from pykeepass.entry import Entry  # type: ignore[import-untyped]

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 5000
FIELDS = {
    "title": 256,
    "project_id": 128,
    "project_name": 256,
    "kind": 32,
    "username": 1024,
    "password": 16384,
    "url": 2048,
    "host": 1024,
    "port": 5,
    "private_key": 65536,
    "notes": 65536,
}
KINDS = {"note", "password", "server", "ssh"}
_SETTINGS = "Harness Vault settings v1"
_FORMAT = "Harness Vault v1"


class VaultError(Exception):
    """A bounded public error code; never include secret values or library errors."""


def private_directory(path: Path) -> None:
    if not path.is_absolute():
        raise VaultError("unsafe_path")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise VaultError("unsafe_path")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise VaultError("unsafe_path")


def read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > MAX_FILE_BYTES
        ):
            raise VaultError("unsafe_path")
        value = stream.read(MAX_FILE_BYTES + 1)
    if len(value) > MAX_FILE_BYTES:
        raise VaultError("too_large")
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, value: bytes, *, replace: bool) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".vault-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _open(value: bytes, password: str) -> PyKeePass:
    if not 0 < len(value) <= MAX_FILE_BYTES or len(password) > 1024:
        raise VaultError("too_large")
    try:
        # Inspect before running a KDF. Imports are our bounded KDBX4 backups,
        # not arbitrary KeePass databases with unbounded work factors.
        header = PyKeePass(BytesIO(value), decrypt=False)
        if header.version != (4, 0) or header.kdf_algorithm != "argon2":
            raise VaultError("invalid_backup")
        parameters = header.kdbx.header.value.dynamic_header.kdf_parameters.data.dict
        if not (
            1 <= parameters["I"].value <= 14
            and 8 * 1024 * 1024 <= parameters["M"].value <= 64 * 1024 * 1024
            and 1 <= parameters["P"].value <= 2
        ):
            raise VaultError("invalid_backup")
        database = PyKeePass(BytesIO(value), password=password)
        if database.root_group.notes != _FORMAT or len(database.entries) > MAX_RECORDS + 1:
            raise VaultError("invalid_backup")
        return database
    except VaultError:
        raise
    except Exception:
        raise VaultError("invalid_password_or_backup") from None


def _serialize(database: PyKeePass) -> bytes:
    stream = BytesIO()
    database.save(stream)
    value = stream.getvalue()
    if len(value) > MAX_FILE_BYTES:
        raise VaultError("too_large")
    return value


def _settings(database: PyKeePass) -> Entry:
    entries = database.find_entries(title=_SETTINGS)
    if len(entries) != 1:
        raise VaultError("invalid_backup")
    return entries[0]


def _record(entry: Entry, *, details: bool) -> dict[str, str]:
    result = {"id": str(entry.uuid)}
    names = FIELDS if details else ("title", "project_id", "project_name", "kind", "host")
    for name in names:
        if name in {"title", "username", "password", "url", "notes"}:
            result[name] = str(getattr(entry, name) or "")
        else:
            result[name] = str(entry.get_custom_property(name) or "")
    return result


def _validate_record(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise VaultError("invalid_record")
    result: dict[str, str] = {}
    for key, limit in FIELDS.items():
        item = value[key]
        if not isinstance(item, str) or len(item) > limit or "\x00" in item:
            raise VaultError("invalid_record")
        result[key] = item
    if (
        not result["title"].strip()
        or result["title"] == _SETTINGS
        or not result["project_id"]
        or result["kind"] not in KINDS
        or (
            result["port"]
            and not (
                result["port"].isascii()
                and result["port"].isdigit()
                and 1 <= int(result["port"]) <= 65535
            )
        )
    ):
        raise VaultError("invalid_record")
    return result


class VaultStore:
    """Single-process owner of one independent KDBX file. No Harness imports/state."""

    def __init__(self, root: Path) -> None:
        private_directory(root)
        self.root = root
        self.path = root / "projects.kdbx"
        self.database: PyKeePass | None = None
        self.revision = ""

    def lock(self) -> None:
        self.database = None
        self.revision = ""

    def _database(self) -> PyKeePass:
        if self.database is None:
            raise VaultError("locked")
        return self.database

    def _backup_directory(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute() or not path.is_dir() or path.resolve() == self.root.resolve():
            raise VaultError("backup_directory")
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise VaultError("backup_directory")
        # Backups live in a private subdirectory; never chmod or take ownership
        # of the operator's sync/mount root or unknown existing files.
        destination = path / "harness-vault-backups"
        private_directory(destination)
        return destination

    def _backup(self, value: bytes, folder: str, label: str = "snapshot") -> str:
        destination = self._backup_directory(folder)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        name = f"vault-{stamp}-{label}-{secrets.token_hex(4)}.kdbx"
        _write(destination / name, value, replace=False)
        if read_private(destination / name) != value:
            raise VaultError("backup_failed")
        return name

    def create(self, password: str, folder: str, *, no_password: bool = False) -> None:
        if self.path.exists() or self.path.is_symlink():
            raise VaultError("already_exists")
        if no_password:
            password = ""
        elif not 12 <= len(password) <= 1024:
            raise VaultError("weak_password")
        self._backup_directory(folder)
        database = create_database(BytesIO(), password=password)
        database.root_group.notes = _FORMAT
        settings = database.add_entry(database.root_group, _SETTINGS, "", "")
        settings.set_custom_property("backup_folder", folder)
        self._commit(database, expected="", folder=folder)

    def unlock(self, password: str) -> None:
        value = read_private(self.path)
        database = _open(value, password)
        _settings(database)
        self.database = database
        self.revision = hashlib.sha256(value).hexdigest()

    def state(self) -> dict[str, object]:
        database = self._database()
        settings = _settings(database)
        return {
            "revision": self.revision,
            "records": [
                _record(item, details=False)
                for item in database.entries
                if item.uuid != settings.uuid
            ],
            "backup_folder": str(settings.get_custom_property("backup_folder") or ""),
            "backup_at": str(settings.get_custom_property("backup_at") or ""),
        }

    def _entry(self, identity: str) -> Entry:
        try:
            uuid = UUID(identity)
        except ValueError:
            raise VaultError("not_found") from None
        entries = self._database().find_entries(uuid=uuid)
        if len(entries) != 1 or entries[0].uuid == _settings(self._database()).uuid:
            raise VaultError("not_found")
        return entries[0]

    def detail(self, identity: str) -> dict[str, str]:
        return _record(self._entry(identity), details=True)

    def _check_revision(self, expected: str) -> None:
        if expected != self.revision:
            raise VaultError("conflict")
        actual = hashlib.sha256(read_private(self.path)).hexdigest() if self.path.exists() else ""
        if actual != expected:
            self.lock()
            raise VaultError("conflict")

    def _commit(self, database: PyKeePass, *, expected: str, folder: str) -> None:
        self._check_revision(expected)
        settings = _settings(database)
        settings.set_custom_property("backup_folder", folder)
        settings.set_custom_property("backup_at", datetime.now(UTC).isoformat())
        value = _serialize(database)
        # A successful save is already durably backed up. Fail closed when the
        # external disk is absent/full, before replacing the last good live file.
        self._backup(value, folder)
        self._check_revision(expected)
        _write(self.path, value, replace=bool(expected))
        self.database = database
        self.revision = hashlib.sha256(value).hexdigest()

    def save_record(self, identity: str, data: object, expected: str) -> None:
        record = _validate_record(data)
        self._check_revision(expected)
        database = self._database()
        try:
            if identity:
                entry = self._entry(identity)
            else:
                if len(database.entries) > MAX_RECORDS:
                    raise VaultError("too_large")
                entry = database.add_entry(
                    database.root_group, record["title"], "", "", force_creation=True
                )
            for key, value in record.items():
                if key in {"title", "username", "password", "url", "notes"}:
                    setattr(entry, key, value)
                else:
                    entry.set_custom_property(key, value, protect=key == "private_key")
            folder = str(_settings(database).get_custom_property("backup_folder"))
            self._commit(database, expected=expected, folder=folder)
        except Exception:
            # Never leave a failed edit in the unlocked working copy.
            self.lock()
            raise

    def delete_record(self, identity: str, expected: str) -> None:
        self._check_revision(expected)
        database = self._database()
        try:
            database.delete_entry(self._entry(identity))
            self._commit(
                database,
                expected=expected,
                folder=str(_settings(database).get_custom_property("backup_folder")),
            )
        except Exception:
            self.lock()
            raise

    def backup(self, folder: str, expected: str) -> None:
        self._check_revision(expected)
        self._backup_directory(folder)
        try:
            self._commit(self._database(), expected=expected, folder=folder)
        except Exception:
            self.lock()
            raise

    def restore(self, value: bytes, password: str, folder: str, expected: str) -> None:
        database = _open(value, password)
        _settings(database)
        self._backup_directory(folder)
        self._check_revision(expected)
        if expected:
            self._backup(read_private(self.path), folder, "before-restore")
        self._commit(database, expected=expected, folder=folder)

    def change_password(self, password: str, expected: str) -> None:
        if not 12 <= len(password) <= 1024:
            raise VaultError("weak_password")
        self._change_password(password, expected)

    def disable_password(self, expected: str) -> None:
        """Explicit operator mode: portable empty-password KDBX, no hidden local key."""
        self._check_revision(expected)
        folder = str(_settings(self._database()).get_custom_property("backup_folder"))
        self._backup(read_private(self.path), folder, "before-password-removal")
        self._change_password("", expected)

    def _change_password(self, password: str, expected: str) -> None:
        self._check_revision(expected)
        database = self._database()
        try:
            database.password = password
            self._commit(
                database,
                expected=expected,
                folder=str(_settings(database).get_custom_property("backup_folder")),
            )
        except Exception:
            self.lock()
            raise

    def recover(self, value: bytes, password: str, folder: str) -> None:
        """Recover even when the live file cannot be opened; retain its bytes first."""
        database = _open(value, password)
        _settings(database)
        self._backup_directory(folder)
        current = read_private(self.path) if self.path.exists() else b""
        expected = hashlib.sha256(current).hexdigest() if current else ""
        if current:
            self._backup(current, folder, "before-recovery")
        self.revision = expected
        try:
            self._commit(database, expected=expected, folder=folder)
        except Exception:
            self.lock()
            raise
