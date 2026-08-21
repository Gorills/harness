from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class RuntimePathError(RuntimeError):
    """Base class for canonical Harness runtime-path failures."""


class InsecureStateDirectoryError(RuntimePathError):
    """Raised when the canonical Harness state directory is not private to the current user."""


class InsecureRuntimeDirectoryError(RuntimePathError):
    """Raised when the canonical Harness runtime directory is not private to the current user."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Canonical per-user paths used by the installed Harness POSIX runtime."""

    database: Path
    socket: Path


def default_runtime_paths(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    temp_directory: Path | None = None,
    effective_uid: int | None = None,
) -> RuntimePaths:
    """Return deterministic POSIX state/database and local-IPC socket defaults."""
    _require_posix_runtime()
    values = os.environ if environment is None else environment

    state_base = _absolute_environment_path(values.get("XDG_STATE_HOME"))
    if state_base is None:
        home_directory = _home_directory() if home is None else home
        if not home_directory.is_absolute():
            raise RuntimePathError("Harness home directory must be absolute")
        state_base = home_directory / ".local" / "state"

    runtime_base = _absolute_environment_path(values.get("XDG_RUNTIME_DIR"))
    if runtime_base is None:
        fallback_root = _temporary_directory() if temp_directory is None else temp_directory
        if not fallback_root.is_absolute():
            raise RuntimePathError("Harness temporary directory must be absolute")
        runtime_directory = fallback_root / f"harness-{_effective_uid(effective_uid)}"
    else:
        runtime_directory = runtime_base / "harness"

    return RuntimePaths(
        database=state_base / "harness" / "harness.db",
        socket=runtime_directory / "harness.sock",
    )


def ensure_private_state_directory(
    directory: Path,
    *,
    effective_uid: int | None = None,
) -> None:
    """Create/validate the canonical state directory as a real current-user-only directory."""
    _require_posix_runtime()
    uid = _effective_uid(effective_uid)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_stat = directory.lstat()
    except OSError as exc:
        raise RuntimePathError("Harness state directory could not be prepared") from exc

    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != uid
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise InsecureStateDirectoryError(
            "Harness state directory must be a real directory owned by the current user "
            "with no group/other access"
        )


def require_private_runtime_directory(
    directory: Path,
    *,
    effective_uid: int | None = None,
) -> None:
    """Validate a canonical socket directory before a client trusts its Unix socket."""
    _require_posix_runtime()
    uid = _effective_uid(effective_uid)
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError as exc:
        raise RuntimePathError("Harness runtime directory does not exist") from exc
    except OSError as exc:
        raise RuntimePathError("Harness runtime directory could not be inspected") from exc

    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != uid
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise InsecureRuntimeDirectoryError(
            "Harness runtime directory must be a real directory owned by the current user "
            "with no group/other access"
        )


def _absolute_environment_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else None


def _home_directory() -> Path:
    try:
        return Path.home()
    except RuntimeError as exc:
        raise RuntimePathError(
            "Harness could not determine the current user's home directory"
        ) from exc


def _temporary_directory() -> Path:
    try:
        return Path(tempfile.gettempdir())
    except (OSError, RuntimeError) as exc:
        raise RuntimePathError("Harness could not determine a temporary runtime directory") from exc


def _effective_uid(value: int | None) -> int:
    uid = os.geteuid() if value is None else value
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise RuntimePathError("Harness effective user identity is invalid")
    return uid


def _require_posix_runtime() -> None:
    if os.name == "nt" or not hasattr(os, "geteuid"):
        raise RuntimePathError(
            "canonical Harness daemon paths are not implemented on this platform"
        )
