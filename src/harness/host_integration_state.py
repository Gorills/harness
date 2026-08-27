from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from harness.host_adapters import HostIntegrationError, IntegrationChange
from harness.runtime_paths import RuntimePathError, RuntimePaths, ensure_private_state_directory

HOST_INTEGRATION_STATE_VERSION = 1
HOST_INTEGRATION_STATE_FILENAME = "host-integrations.json"
SUPPORTED_HOST_PROFILES = frozenset({"claude-code", "cursor"})
_WRITE_MODE = 0o600
_TEMPORARY_PREFIX = ".harness-host-integrations-"


class HostIntegrationStateError(HostIntegrationError):
    """Raised when Harness-owned host integration state cannot be read or updated."""


@dataclass(frozen=True, slots=True)
class HostIntegrationState:
    """Durable intent for which supported host profiles Harness currently owns."""

    profiles: frozenset[str]

    def includes(self, profile: str) -> bool:
        return profile in self.profiles


def host_integration_state_path(paths: RuntimePaths) -> Path:
    """Return the Harness-owned host-profile registry next to the canonical database."""
    return paths.database.parent / HOST_INTEGRATION_STATE_FILENAME


def load_host_integration_state(paths: RuntimePaths) -> HostIntegrationState:
    """Read the host-profile registry, or empty intent when the file is absent."""
    path = host_integration_state_path(paths)
    raw = _read_state_bytes(path)
    if raw is None:
        return HostIntegrationState(profiles=frozenset())
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostIntegrationStateError(
            f"Harness host integration state is not valid UTF-8 JSON: {path}"
        ) from exc
    return _parse_state(value, path)


def write_host_integration_state(
    paths: RuntimePaths, state: HostIntegrationState
) -> IntegrationChange:
    """Replace the host-profile registry atomically, deleting it when intent is empty."""
    _prepare_state_directory(paths)
    path = host_integration_state_path(paths)
    current = load_host_integration_state(paths)
    if current.profiles == state.profiles:
        if not state.profiles and not _path_exists(path):
            return IntegrationChange.UNCHANGED
        if state.profiles:
            return IntegrationChange.UNCHANGED
    if not state.profiles:
        return _delete_state_file(path)
    payload = _encode_state(state)
    existing = _read_state_bytes(path)
    if existing == payload:
        return IntegrationChange.UNCHANGED
    mode = _WRITE_MODE
    if existing is not None:
        mode = stat.S_IMODE(path.lstat().st_mode)
    _replace_if_unchanged(path, existing, payload, mode)
    return IntegrationChange.CHANGED


def add_host_profiles(paths: RuntimePaths, profiles: Iterable[str]) -> IntegrationChange:
    """Record host-profile intent without removing other recorded profiles."""
    requested = _require_supported_profiles(profiles)
    current = load_host_integration_state(paths)
    return write_host_integration_state(
        paths, HostIntegrationState(profiles=current.profiles | requested)
    )


def remove_host_profiles(paths: RuntimePaths, profiles: Iterable[str]) -> IntegrationChange:
    """Clear selected host-profile intent, deleting the registry when none remain."""
    requested = _require_supported_profiles(profiles)
    current = load_host_integration_state(paths)
    return write_host_integration_state(
        paths, HostIntegrationState(profiles=current.profiles - requested)
    )


def _require_supported_profiles(profiles: Iterable[str]) -> frozenset[str]:
    requested = frozenset(profiles)
    unknown = requested - SUPPORTED_HOST_PROFILES
    if unknown:
        raise HostIntegrationStateError(
            "unsupported Harness host profile in integration state: " + ", ".join(sorted(unknown))
        )
    return requested


def _parse_state(value: object, path: Path) -> HostIntegrationState:
    if not isinstance(value, dict):
        raise HostIntegrationStateError(
            f"Harness host integration state top level must be an object: {path}"
        )
    expected_keys = {"version", "profiles"}
    if set(value) != expected_keys:
        raise HostIntegrationStateError(
            f"Harness host integration state keys are unsupported: {path}"
        )
    if value.get("version") != HOST_INTEGRATION_STATE_VERSION:
        raise HostIntegrationStateError(
            f"Harness host integration state version is unsupported: {path}"
        )
    observed = value.get("profiles")
    if not isinstance(observed, list) or any(not isinstance(item, str) for item in observed):
        raise HostIntegrationStateError(
            f"Harness host integration state profiles must be a string array: {path}"
        )
    profiles = _require_supported_profiles(observed)
    if len(observed) != len(profiles):
        raise HostIntegrationStateError(
            f"Harness host integration state profiles must be unique: {path}"
        )
    return HostIntegrationState(profiles=profiles)


def _encode_state(state: HostIntegrationState) -> bytes:
    value = {
        "version": HOST_INTEGRATION_STATE_VERSION,
        "profiles": sorted(state.profiles),
    }
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _prepare_state_directory(paths: RuntimePaths) -> None:
    try:
        ensure_private_state_directory(paths.database.parent)
    except RuntimePathError as exc:
        raise HostIntegrationStateError(
            "Harness host integration state directory is unsafe"
        ) from exc


def _read_state_bytes(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HostIntegrationStateError(
            f"Harness host integration state cannot be inspected: {path}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HostIntegrationStateError(
            f"Harness host integration state is not a real regular file: {path}"
        )
    if metadata.st_uid != os.geteuid():
        raise HostIntegrationStateError(
            f"Harness host integration state is not owned by the current user: {path}"
        )
    if metadata.st_nlink != 1:
        raise HostIntegrationStateError(
            f"Harness host integration state must not be a hard link: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o177:
        raise HostIntegrationStateError(
            f"Harness host integration state must not be group/other accessible: {path}"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HostIntegrationStateError(
            f"Harness host integration state cannot be read: {path}"
        ) from exc


def _delete_state_file(path: Path) -> IntegrationChange:
    expected = _read_state_bytes(path)
    if expected is None:
        return IntegrationChange.UNCHANGED
    _delete_if_unchanged(path, expected)
    return IntegrationChange.CHANGED


def _require_parent_safe(path: Path) -> None:
    parent = path.parent
    if parent.exists():
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise HostIntegrationStateError(
                f"Harness host integration state directory cannot be inspected: {parent}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HostIntegrationStateError(
                f"Harness host integration state directory is unsafe: {parent}"
            )
        return
    try:
        parent.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise HostIntegrationStateError(
            f"Harness host integration state directory cannot be created: {parent}"
        ) from exc


def _replace_if_unchanged(
    path: Path, expected: bytes | None, replacement: bytes, mode: int
) -> None:
    _require_parent_safe(path)
    fd, temporary_name = tempfile.mkstemp(prefix=_TEMPORARY_PREFIX, dir=path.parent)
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if expected is None:
            if not _move_if_absent(temporary, path):
                raise HostIntegrationStateError(
                    f"Harness host integration state appeared before mutation: {path}"
                )
            _fsync_directory(path.parent)
            return

        current = _read_state_bytes(path)
        if current != expected:
            raise HostIntegrationStateError(
                f"Harness host integration state changed before mutation: {path}"
            )
        backup = _unused_sibling(path, ".harness-host-integrations-backup-")
        if not _move_if_absent(path, backup):
            raise HostIntegrationStateError(
                f"Harness host integration state backup path appeared: {backup}"
            )
        moved = _read_state_bytes(backup)
        if moved != expected:
            _restore_backup(path, backup)
            backup = None
            raise HostIntegrationStateError(
                f"Harness host integration state changed during mutation: {path}"
            )
        if not _move_if_absent(temporary, path):
            _restore_backup(path, backup, preserve_if_occupied=True)
            backup = None
            raise HostIntegrationStateError(
                "Harness host integration state appeared during mutation; "
                f"previous content was preserved: {path}"
            )
        backup.unlink()
        backup = None
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _delete_if_unchanged(path: Path, expected: bytes) -> None:
    current = _read_state_bytes(path)
    if current is None:
        return
    if current != expected:
        raise HostIntegrationStateError(
            f"Harness host integration state changed before removal: {path}"
        )
    backup = _unused_sibling(path, ".harness-host-integrations-delete-")
    if not _move_if_absent(path, backup):
        raise HostIntegrationStateError(
            f"Harness host integration state removal backup path appeared: {backup}"
        )
    if _read_state_bytes(backup) != expected:
        _restore_backup(path, backup)
        raise HostIntegrationStateError(
            f"Harness host integration state changed during removal: {path}"
        )
    backup.unlink()
    _fsync_directory(path.parent)


def _restore_backup(path: Path, backup: Path, *, preserve_if_occupied: bool = False) -> None:
    if _path_exists(path):
        if preserve_if_occupied:
            return
        raise HostIntegrationStateError(
            f"Harness host integration recovery could not restore {path}; "
            f"backup preserved at {backup}"
        )
    try:
        restored = _move_if_absent(backup, path)
    except OSError as exc:
        raise HostIntegrationStateError(
            f"Harness host integration recovery failed; backup preserved at {backup}"
        ) from exc
    if not restored:
        raise HostIntegrationStateError(
            f"Harness host integration recovery could not restore {path}; "
            f"backup preserved at {backup}"
        )


def _unused_sibling(path: Path, prefix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    os.close(fd)
    candidate = Path(name)
    candidate.unlink()
    return candidate


def _move_if_absent(source: Path, target: Path) -> bool:
    if os.name == "nt":
        raise HostIntegrationStateError("Harness host integration state currently requires POSIX")
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise HostIntegrationStateError("atomic no-clobber rename is unavailable") from exc
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, target_bytes, 1)
    else:
        renamex_np = getattr(library, "renamex_np", None)
        if renamex_np is None:
            raise HostIntegrationStateError("atomic no-clobber rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, target_bytes, 0x00000004)
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    raise OSError(error_number, os.strerror(error_number), target)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostIntegrationStateError(
            f"Harness host integration state cannot be inspected: {path}"
        ) from exc
    return True


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
