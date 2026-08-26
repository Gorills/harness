from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from harness.daemon import DaemonError, hold_database_maintenance_lock
from harness.daemon_autostart import ensure_canonical_daemon
from harness.doctor import run_doctor_checks
from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    discover_claude_code_adapter,
)
from harness.ipc import (
    IpcError,
    SkillCleanupResult,
    StatusResult,
    request_shutdown,
    request_skill_cleanup,
    request_status,
)
from harness.runtime_paths import (
    RuntimePathError,
    RuntimePaths,
    default_runtime_paths,
    ensure_private_state_directory,
)
from harness.skills import SkillRegistryError, default_skill_registry
from harness.storage import SCHEMA_VERSION

_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_SHUTDOWN_POLL_SECONDS = 0.05


class InstallationError(RuntimeError):
    """Raised when the supported Linux installation lifecycle cannot complete safely."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    host_profile: str
    registration_change: IntegrationChange
    daemon_status: StatusResult


@dataclass(frozen=True, slots=True)
class UninstallResult:
    host_profile: str
    registration_change: IntegrationChange
    skill_cleanup: SkillCleanupResult
    purged: bool


def install_harness(
    *,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> InstallResult:
    """Install the currently supported Linux/Claude Code Harness integration idempotently."""
    _require_runtime_prerequisites()
    adapter = discover_claude_code_adapter(
        environment=environment,
        python_executable=python_executable,
    )
    if adapter is None:
        raise InstallationError("Claude Code CLI was not found on PATH")
    state = adapter.registration_state()
    if state is HostRegistrationState.FOREIGN:
        raise HostRegistrationCollisionError(
            "Claude Code already has a non-Harness MCP server named 'harness'"
        )

    paths = _runtime_paths(environment)
    _preflight_canonical_database_state(paths)
    try:
        ensure_canonical_daemon(paths, environment=environment)
        daemon_status = request_status(paths.socket)
    except (RuntimePathError, IpcError) as exc:
        raise InstallationError("Harness daemon could not be prepared") from exc

    try:
        change = adapter.register_mcp()
    except HostIntegrationError:
        raise
    return InstallResult(
        host_profile=adapter.profile,
        registration_change=change,
        daemon_status=daemon_status,
    )


def uninstall_harness(
    *,
    purge: bool = False,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> UninstallResult:
    """Remove Harness-owned Claude integration artifacts while preserving Project Intelligence."""
    adapter = discover_claude_code_adapter(
        environment=environment,
        python_executable=python_executable,
    )
    if adapter is None:
        raise InstallationError(
            "Claude Code CLI was not found; Harness cannot safely verify/remove its registration"
        )
    state = adapter.registration_state()
    if state is HostRegistrationState.FOREIGN:
        raise HostRegistrationCollisionError(
            "Claude Code MCP server named 'harness' is not owned by Harness"
        )

    paths = _runtime_paths(environment)
    _preflight_canonical_database_state(paths)
    if purge:
        _preflight_skill_registry_purge(environment)
    if state is HostRegistrationState.ABSENT and not (
        paths.database.exists() or paths.socket.exists()
    ):
        if purge:
            try:
                _purge_without_running_daemon(paths, environment)
            except (DaemonError, OSError) as exc:
                raise InstallationError("Harness state cleanup could not be completed") from exc
        return UninstallResult(
            host_profile=adapter.profile,
            registration_change=IntegrationChange.UNCHANGED,
            skill_cleanup=SkillCleanupResult(
                schema_version=SCHEMA_VERSION,
                workspace_count=0,
                cleaned_workspace_count=0,
                skipped_workspace_count=0,
                removed=0,
                exclude_changed_count=0,
            ),
            purged=purge,
        )
    try:
        ensure_canonical_daemon(paths, environment=environment)
        cleanup = request_skill_cleanup(paths.socket, (adapter.profile,))
    except (RuntimePathError, IpcError) as exc:
        raise InstallationError(
            "Harness project integration cleanup could not be completed"
        ) from exc

    change = adapter.unregister_mcp()
    try:
        shutdown = request_shutdown(paths.socket)
        if not shutdown.accepted:
            raise InstallationError("Harness daemon rejected shutdown")
        _wait_for_daemon_shutdown(paths.socket)
        if purge:
            _purge_after_daemon_shutdown(paths, environment)
    except (RuntimePathError, IpcError, DaemonError, OSError) as exc:
        raise InstallationError("Harness daemon/state cleanup could not be completed") from exc

    return UninstallResult(
        host_profile=adapter.profile,
        registration_change=change,
        skill_cleanup=cleanup,
        purged=purge,
    )


def _require_runtime_prerequisites() -> None:
    report = run_doctor_checks()
    if report.sqlite_error is not None:
        raise InstallationError(f"SQLite runtime check failed: {report.sqlite_error}")
    if not report.fts5_available:
        raise InstallationError("SQLite runtime does not provide required FTS5 support")


def _runtime_paths(environment: Mapping[str, str] | None) -> RuntimePaths:
    try:
        return default_runtime_paths(environment=environment)
    except RuntimePathError as exc:
        raise InstallationError("canonical Harness runtime paths are unavailable") from exc


def _wait_for_daemon_shutdown(socket_path: Path) -> None:
    deadline = monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
    while True:
        try:
            socket_path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise InstallationError(
                "Harness daemon socket could not be inspected after shutdown"
            ) from exc
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise InstallationError("Harness daemon did not stop cleanly")
        sleep(min(_SHUTDOWN_POLL_SECONDS, remaining))


def _preflight_purge_targets(paths: RuntimePaths, environment: Mapping[str, str] | None) -> None:
    _preflight_canonical_database_state(paths)
    _preflight_skill_registry_purge(environment)


def _canonical_database_purge_candidates(paths: RuntimePaths) -> tuple[Path, ...]:
    return (
        paths.database,
        paths.database.with_name(f"{paths.database.name}-wal"),
        paths.database.with_name(f"{paths.database.name}-shm"),
        paths.database.with_name(f"{paths.database.name}-journal"),
    )


def _canonical_database_lock_path(paths: RuntimePaths) -> Path:
    return paths.database.with_name(f"{paths.database.name}.lock")


def _preflight_canonical_database_state(paths: RuntimePaths) -> None:
    state_directory = paths.database.parent
    try:
        directory_metadata = state_directory.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InstallationError("Harness state directory could not be inspected") from exc
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_ISLNK(directory_metadata.st_mode)
        or directory_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(directory_metadata.st_mode) & 0o077
    ):
        raise InstallationError("Harness refused an unsafe state directory")

    for candidate in (
        *_canonical_database_purge_candidates(paths),
        _canonical_database_lock_path(paths),
    ):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InstallationError("Harness database state could not be inspected") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise InstallationError("Harness refused unsafe database state")


def _purge_canonical_database(paths: RuntimePaths) -> None:
    state_directory = paths.database.parent
    if not state_directory.exists():
        return
    ensure_private_state_directory(state_directory)
    _preflight_canonical_database_state(paths)
    for candidate in _canonical_database_purge_candidates(paths):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
    try:
        state_directory.rmdir()
    except OSError:
        # Preserve the singleton lock sentinel and any unknown/user-created files.
        pass


def _purge_after_daemon_shutdown(
    paths: RuntimePaths, environment: Mapping[str, str] | None
) -> None:
    with hold_database_maintenance_lock(paths.database):
        _preflight_purge_targets(paths, environment)
        _purge_canonical_database(paths)
        _purge_skill_registry(environment)


def _purge_without_running_daemon(
    paths: RuntimePaths, environment: Mapping[str, str] | None
) -> None:
    candidates_exist = any(
        _path_exists_without_following(path)
        for path in (
            *_canonical_database_purge_candidates(paths),
            _canonical_database_lock_path(paths),
        )
    )
    if candidates_exist:
        _purge_after_daemon_shutdown(paths, environment)
        return
    _purge_skill_registry(environment)


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise InstallationError("Harness state could not be inspected for purge") from exc
    return True


def _skill_registry_path(environment: Mapping[str, str] | None) -> Path:
    values = os.environ if environment is None else environment
    home_value = values.get("HOME")
    home = Path.home() if not home_value else Path(home_value).expanduser()
    try:
        return default_skill_registry(home=home)
    except SkillRegistryError as exc:
        raise InstallationError("Harness skill registry path is unsafe") from exc


def _preflight_skill_registry_purge(environment: Mapping[str, str] | None) -> None:
    registry = _skill_registry_path(environment)
    try:
        metadata = registry.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InstallationError("Harness skill registry could not be inspected") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise InstallationError("Harness purge refused an unsafe skill registry")


def _purge_skill_registry(environment: Mapping[str, str] | None) -> None:
    registry = _skill_registry_path(environment)
    _preflight_skill_registry_purge(environment)
    if not registry.exists():
        return
    try:
        shutil.rmtree(registry)
        registry.parent.rmdir()
    except OSError as exc:
        if registry.exists():
            raise InstallationError("Harness skill registry could not be purged") from exc
        # The registry is gone; preserve a non-empty ~/.harness parent containing unknown data.
