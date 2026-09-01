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


DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 17373
DASHBOARD_ISOLATED_PORT = 17374
MCP_HTTP_PORT = 17375
MCP_HTTP_ISOLATED_PORT = 17376
_DASHBOARD_PORT_OVERRIDE = "HARNESS_ACCEPTANCE_DASHBOARD_PORT"
_MCP_HTTP_PORT_OVERRIDE = "HARNESS_ACCEPTANCE_MCP_HTTP_PORT"


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


def dashboard_listen_port(
    socket_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    temp_directory: Path | None = None,
    effective_uid: int | None = None,
) -> int:
    """Return the loopback TCP port for this daemon instance's dashboard.

    The canonical per-user daemon binds ``DASHBOARD_PORT``. An isolated checkout
    (``HARNESS_DEV_ROOT``) binds the neighboring ``DASHBOARD_ISOLATED_PORT`` so
    both listeners can exist at once. Explicit socket overrides stay ephemeral.
    """
    values = os.environ if environment is None else environment
    override = _acceptance_port_override(values, _DASHBOARD_PORT_OVERRIDE)
    if override is not None:
        return override
    canonical = default_runtime_paths(
        environment=environment,
        home=home,
        temp_directory=temp_directory,
        effective_uid=effective_uid,
    ).socket
    if socket_path != canonical:
        return 0
    if values.get("HARNESS_DEV_ROOT"):
        return DASHBOARD_ISOLATED_PORT
    return DASHBOARD_PORT


def mcp_http_listen_port(
    socket_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    temp_directory: Path | None = None,
    effective_uid: int | None = None,
) -> int:
    """Return the daemon-owned loopback MCP port for this runtime.

    Canonical and isolated daemons use distinct stable ports. Explicit socket
    overrides use an ephemeral port so tests and recovery daemons cannot collide
    with either live runtime.
    """
    values = os.environ if environment is None else environment
    override = _acceptance_port_override(values, _MCP_HTTP_PORT_OVERRIDE)
    if override is not None:
        return override
    canonical = default_runtime_paths(
        environment=environment,
        home=home,
        temp_directory=temp_directory,
        effective_uid=effective_uid,
    ).socket
    if socket_path != canonical:
        return 0
    if values.get("HARNESS_DEV_ROOT"):
        return MCP_HTTP_ISOLATED_PORT
    return MCP_HTTP_PORT


def _acceptance_port_override(values: Mapping[str, str], name: str) -> int | None:
    """Read a bounded machine-acceptance-only loopback port override."""
    raw = values.get(name)
    if raw is None:
        return None
    try:
        port = int(raw, 10)
    except ValueError as exc:
        raise RuntimePathError(f"{name} must be a TCP port") from exc
    if not 1 <= port <= 65535:
        raise RuntimePathError(f"{name} must be between 1 and 65535")
    return port


def ensure_private_state_directory(
    directory: Path,
    *,
    effective_uid: int | None = None,
) -> None:
    """Create/validate the canonical state directory as a real current-user-only directory."""
    _require_posix_runtime()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimePathError("Harness state directory could not be prepared") from exc
    require_private_state_directory(directory, effective_uid=effective_uid)


def require_private_state_directory(
    directory: Path,
    *,
    effective_uid: int | None = None,
) -> None:
    """Validate an existing canonical state directory without creating or changing it."""
    _require_posix_runtime()
    uid = _effective_uid(effective_uid)
    try:
        directory_stat = directory.lstat()
    except FileNotFoundError as exc:
        raise RuntimePathError("Harness state directory does not exist") from exc
    except OSError as exc:
        raise RuntimePathError("Harness state directory could not be inspected") from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != uid
        or stat.S_IMODE(directory_stat.st_mode) & 0o077
    ):
        raise InsecureStateDirectoryError(
            "Harness state directory must be owned by the current user, be a real directory, "
            "and have no group/other access"
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
            "Harness runtime directory must be owned by the current user, be a real directory, "
            "and have no group/other access"
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
