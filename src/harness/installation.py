from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

from harness.builtin_skills import BuiltinSkillError, BuiltinSkillSyncResult, sync_builtin_skills
from harness.codex_adapter import CodexAdapter, discover_codex_adapter
from harness.cursor_adapter import (
    CursorAdapter,
    CursorProjectRuntimeStatus,
    cursor_project_enable_command,
    discover_cursor_adapter,
)
from harness.daemon import DaemonError, hold_database_maintenance_lock
from harness.daemon_autostart import ensure_canonical_daemon
from harness.doctor import run_doctor_checks
from harness.host_adapters import (
    ClaudeCodeAdapter,
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    discover_claude_code_adapter,
)
from harness.host_integration_state import (
    add_host_profiles,
    load_host_integration_state,
    remove_host_profiles,
)
from harness.ipc import (
    IpcError,
    IpcRemoteError,
    RuntimeDiagnosticsResult,
    SkillCleanupResult,
    request_runtime_diagnostics,
    request_set_visibility,
    request_shutdown,
    request_skill_cleanup,
    request_status,
    request_workspace_skills_reconcile,
)
from harness.registry import WorkspaceRecord, list_workspaces
from harness.runtime_identity import RuntimeIdentity, RuntimeIdentityError, current_runtime_identity
from harness.runtime_paths import (
    RuntimePathError,
    RuntimePaths,
    default_runtime_paths,
    ensure_private_state_directory,
)
from harness.runtime_state import (
    RuntimeStateError,
    canonical_database_lock_path,
    canonical_database_purge_candidates,
    preflight_canonical_database_state,
)
from harness.skill_runtime import SkillRuntimeError, validate_skill_profile_combination
from harness.skills import SkillRegistryError, default_skill_registry
from harness.storage import SCHEMA_VERSION, DatabaseError, connect_database_read_only
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_SHUTDOWN_POLL_SECONDS = 0.05
_SUPPORTED_HOSTS = ("claude-code", "codex", "cursor")
_PROJECT_SCOPED_ADAPTERS = (CodexAdapter, CursorAdapter)


class InstallationError(RuntimeError):
    """Raised when the supported Linux installation lifecycle cannot complete safely."""


@dataclass(frozen=True, slots=True)
class HostInstallResult:
    host_profile: str
    registration_change: IntegrationChange
    project_change_count: int = 0
    project_runtime_verified: int = 0
    project_runtime_manual: int = 0
    manual_enable_commands: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InstallResult:
    host_profile: str
    registration_change: IntegrationChange
    daemon_status: RuntimeDiagnosticsResult
    builtin_skills: BuiltinSkillSyncResult
    hosts: tuple[HostInstallResult, ...] = ()


@dataclass(frozen=True, slots=True)
class HostUninstallResult:
    host_profile: str
    registration_change: IntegrationChange
    project_change_count: int = 0


@dataclass(frozen=True, slots=True)
class UninstallResult:
    host_profile: str
    registration_change: IntegrationChange
    skill_cleanup: SkillCleanupResult
    purged: bool
    hosts: tuple[HostUninstallResult, ...] = ()


def install_harness(
    *,
    host: str = "claude-code",
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> InstallResult:
    """Install one supported local Harness host integration idempotently.

    ``all`` remains an explicit compatibility check but is rejected while the complete
    three-profile skill visibility graph cannot be projected without duplicates.
    """
    _require_runtime_prerequisites()
    if host == "all":
        raise InstallationError(
            "selected host integrations cannot share a duplicate-free project skill layout; "
            "install a compatible pair explicitly instead of Claude Code, Codex, and Cursor "
            "together"
        )
    selected = _selected_adapters(
        host,
        environment=environment,
        python_executable=python_executable,
        codex_cli_required=True,
    )
    for adapter in selected:
        state = adapter.registration_state()
        if state is HostRegistrationState.FOREIGN:
            raise HostRegistrationCollisionError(
                f"{_host_label(adapter)} already has a non-Harness MCP server named 'harness'"
            )

    paths = _runtime_paths(environment)
    _require_safe_database_state(paths)
    try:
        active_profiles = _active_profiles(
            paths=paths,
            environment=environment,
            python_executable=python_executable,
            selected=selected,
            selected_states={adapter.profile: adapter.registration_state() for adapter in selected},
        )
        validate_skill_profile_combination(
            tuple(sorted(active_profiles | {a.profile for a in selected}))
        )
    except SkillRuntimeError as exc:
        raise InstallationError(
            "selected host integrations cannot share a duplicate-free project skill layout; "
            "remove one of Claude Code, Codex, or Cursor before installing the other two"
        ) from exc
    preflight_workspaces = _registered_workspaces(paths) if paths.database.exists() else ()
    hidden_workspace_roots = _registered_hidden_workspace_roots(paths)
    for adapter in selected:
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            for workspace in preflight_workspaces:
                if isinstance(adapter, CodexAdapter):
                    adapter.preflight_project_reconcile(
                        workspace.workspace_root,
                        hidden=workspace.workspace_root in hidden_workspace_roots,
                    )
                else:
                    adapter.preflight_project_reconcile(workspace.workspace_root)

    try:
        builtin_skills = sync_builtin_skills(_skill_registry_path(environment))
    except BuiltinSkillError as exc:
        raise InstallationError("Harness built-in skills could not be reconciled") from exc

    try:
        daemon_status = _ensure_current_daemon(paths, environment)
    except (RuntimePathError, IpcError, RuntimeIdentityError) as exc:
        raise InstallationError("Harness daemon could not be prepared") from exc

    workspaces = _registered_workspaces(paths)
    hidden_workspace_roots = _registered_hidden_workspace_roots(paths)
    for adapter in selected:
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            for workspace in workspaces:
                if isinstance(adapter, CodexAdapter):
                    adapter.preflight_project_reconcile(
                        workspace.workspace_root,
                        hidden=workspace.workspace_root in hidden_workspace_roots,
                    )
                else:
                    adapter.preflight_project_reconcile(workspace.workspace_root)

    results: list[HostInstallResult] = []
    for adapter in selected:
        intent_change = IntegrationChange.UNCHANGED
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            intent_change = add_host_profiles(paths, (adapter.profile,))
        change = adapter.register_mcp()
        if intent_change is IntegrationChange.CHANGED:
            change = IntegrationChange.CHANGED
        project_changes = 0
        runtime_verified = 0
        runtime_manual = 0
        manual_commands: list[str] = []
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            for workspace in workspaces:
                if isinstance(adapter, CodexAdapter):
                    project_change = adapter.reconcile_project(
                        workspace.workspace_root,
                        hidden=workspace.workspace_root in hidden_workspace_roots,
                    )
                else:
                    project_change = adapter.reconcile_project(workspace.workspace_root)
                project_changes += int(project_change is IntegrationChange.CHANGED)
        if isinstance(adapter, CursorAdapter):
            for workspace in workspaces:
                runtime = adapter.enable_and_verify_project_mcp(
                    workspace.workspace_root, environment=environment
                )
                if runtime.status is CursorProjectRuntimeStatus.VERIFIED:
                    runtime_verified += 1
                elif runtime.status is CursorProjectRuntimeStatus.MANUAL:
                    runtime_manual += 1
                    command = runtime.enable_command or cursor_project_enable_command(
                        workspace.workspace_root
                    )
                    if command not in manual_commands:
                        manual_commands.append(command)
        results.append(
            HostInstallResult(
                host_profile=adapter.profile,
                registration_change=change,
                project_change_count=project_changes,
                project_runtime_verified=runtime_verified,
                project_runtime_manual=runtime_manual,
                manual_enable_commands=tuple(manual_commands),
            )
        )
    try:
        active_after_install = _active_profiles(
            paths=paths,
            environment=environment,
            python_executable=python_executable,
            selected=selected,
            selected_states={adapter.profile: adapter.registration_state() for adapter in selected},
        )
        profiles_after_install = tuple(
            profile for profile in _SUPPORTED_HOSTS if profile in active_after_install
        )
        if workspaces and profiles_after_install:
            _reconcile_remaining_profiles(paths, workspaces, profiles_after_install)
            for hidden_root in _hidden_project_representative_roots(paths):
                request_set_visibility(paths.socket, hidden_root, "hidden")
    except (HostIntegrationError, IpcError) as exc:
        raise InstallationError(
            "Harness project integration could not be reconciled after host installation"
        ) from exc
    overall = (
        IntegrationChange.CHANGED
        if any(
            item.registration_change is IntegrationChange.CHANGED or item.project_change_count
            for item in results
        )
        else IntegrationChange.UNCHANGED
    )
    return InstallResult(
        host_profile=host,
        registration_change=overall,
        daemon_status=daemon_status,
        builtin_skills=builtin_skills,
        hosts=tuple(results),
    )


def uninstall_harness(
    *,
    host: str = "claude-code",
    purge: bool = False,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> UninstallResult:
    """Remove selected Harness-owned host artifacts while preserving other active hosts."""
    selected = _selected_adapters(
        host,
        environment=environment,
        python_executable=python_executable,
        codex_cli_required=False,
    )
    selected_profiles = {adapter.profile for adapter in selected}
    observed_selected: dict[str, HostRegistrationState] = {}
    for adapter in selected:
        state = adapter.registration_state()
        observed_selected[adapter.profile] = state
        if state is HostRegistrationState.FOREIGN:
            raise HostRegistrationCollisionError(
                f"{_host_label(adapter)} MCP server named 'harness' is not owned by Harness"
            )

    paths = _runtime_paths(environment)
    _require_safe_database_state(paths)
    if purge:
        _preflight_skill_registry_purge(environment)

    workspaces = _registered_workspaces(paths) if paths.database.exists() else ()
    for adapter in selected:
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            for workspace in workspaces:
                adapter.preflight_project_remove(workspace.workspace_root)

    active = _active_profiles(
        paths=paths,
        environment=environment,
        python_executable=python_executable,
        selected=selected,
        selected_states=observed_selected,
    )
    remaining = tuple(
        profile for profile in _SUPPORTED_HOSTS if profile in active - selected_profiles
    )
    if purge and remaining:
        raise InstallationError(
            "Harness --purge refused while another supported host integration remains active"
        )

    if "cursor" in remaining:
        remaining_cursor = discover_cursor_adapter(
            environment=environment,
            python_executable=python_executable,
        )
        for workspace in workspaces:
            state = remaining_cursor.project_registration_state(workspace.workspace_root)
            if state is not HostRegistrationState.CURRENT:
                raise InstallationError(
                    "remaining Cursor host is not healthy for all registered Workspaces; "
                    "run harness install --host cursor before removing another host"
                )

    if "codex" in remaining:
        remaining_codex = discover_codex_adapter(
            environment=environment,
            python_executable=python_executable,
        )
        if remaining_codex is None:
            raise InstallationError(
                "remaining Codex host cannot be verified because the Codex CLI was not found on PATH"
            )
        hidden_workspace_roots = _registered_hidden_workspace_roots(paths)
        for workspace in workspaces:
            state = remaining_codex.project_registration_state(
                workspace.workspace_root,
                hidden=workspace.workspace_root in hidden_workspace_roots,
            )
            if state is not HostRegistrationState.CURRENT:
                raise InstallationError(
                    "remaining Codex host is not healthy for all registered Workspaces; "
                    "run harness install --host codex before removing another host"
                )

    any_selected_owned = False
    for adapter in selected:
        state = observed_selected[adapter.profile]
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            if load_host_integration_state(paths).includes(adapter.profile) or state in {
                HostRegistrationState.CURRENT,
                HostRegistrationState.STALE_OWNED,
            }:
                any_selected_owned = True
            continue
        if state in {HostRegistrationState.CURRENT, HostRegistrationState.STALE_OWNED}:
            any_selected_owned = True
    has_state = paths.database.exists() or paths.socket.exists()
    if not any_selected_owned and not has_state:
        if purge:
            try:
                _purge_without_running_daemon(paths, environment)
            except (DaemonError, OSError) as exc:
                raise InstallationError("Harness state cleanup could not be completed") from exc
        return UninstallResult(
            host_profile=host,
            registration_change=IntegrationChange.UNCHANGED,
            skill_cleanup=_empty_cleanup(),
            purged=purge,
            hosts=tuple(
                HostUninstallResult(adapter.profile, IntegrationChange.UNCHANGED)
                for adapter in selected
            ),
        )

    try:
        _ensure_current_daemon(paths, environment)
        cleanup = request_skill_cleanup(paths.socket, tuple(sorted(active or selected_profiles)))
        if remaining:
            reconciled = _reconcile_remaining_profiles(paths, workspaces, remaining)
            cleanup = _combined_cleanup(cleanup, reconciled)
    except (RuntimePathError, IpcError, RuntimeIdentityError) as exc:
        raise InstallationError(
            "Harness project integration cleanup could not be completed"
        ) from exc

    project_changes_by_profile: dict[str, int] = {}
    for adapter in selected:
        if not isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            continue
        project_changes = 0
        for workspace in workspaces:
            project_changes += int(
                adapter.remove_project(workspace.workspace_root) is IntegrationChange.CHANGED
            )
        project_changes_by_profile[adapter.profile] = project_changes

    results: list[HostUninstallResult] = []
    for adapter in selected:
        change = adapter.unregister_mcp()
        if isinstance(adapter, _PROJECT_SCOPED_ADAPTERS):
            intent_change = remove_host_profiles(paths, (adapter.profile,))
            if intent_change is IntegrationChange.CHANGED:
                change = IntegrationChange.CHANGED
        results.append(
            HostUninstallResult(
                host_profile=adapter.profile,
                registration_change=change,
                project_change_count=project_changes_by_profile.get(adapter.profile, 0),
            )
        )

    if not remaining:
        try:
            shutdown = request_shutdown(paths.socket)
            if not shutdown.accepted:
                raise InstallationError("Harness daemon rejected shutdown")
            _wait_for_daemon_shutdown(paths.socket)
            if purge:
                _purge_after_daemon_shutdown(paths, environment)
        except (RuntimePathError, IpcError, DaemonError, OSError) as exc:
            raise InstallationError("Harness daemon/state cleanup could not be completed") from exc

    overall = (
        IntegrationChange.CHANGED
        if any(
            item.registration_change is IntegrationChange.CHANGED or item.project_change_count
            for item in results
        )
        else IntegrationChange.UNCHANGED
    )
    return UninstallResult(
        host_profile=host,
        registration_change=overall,
        skill_cleanup=cleanup,
        purged=purge,
        hosts=tuple(results),
    )


def _selected_adapters(
    host: str,
    *,
    environment: Mapping[str, str] | None,
    python_executable: Path | None,
    codex_cli_required: bool,
) -> tuple[ClaudeCodeAdapter | CodexAdapter | CursorAdapter, ...]:
    profiles = _SUPPORTED_HOSTS if host == "all" else (host,)
    if any(profile not in _SUPPORTED_HOSTS for profile in profiles):
        raise InstallationError(f"unsupported Harness host selection: {host}")
    adapters: list[ClaudeCodeAdapter | CodexAdapter | CursorAdapter] = []
    for profile in profiles:
        if profile == "claude-code":
            claude_adapter = discover_claude_code_adapter(
                environment=environment,
                python_executable=python_executable,
            )
            if claude_adapter is None:
                raise InstallationError("Claude Code CLI was not found on PATH")
            adapters.append(claude_adapter)
        elif profile == "codex":
            codex_adapter = discover_codex_adapter(
                environment=environment,
                python_executable=python_executable,
            )
            if codex_adapter is None:
                if codex_cli_required:
                    raise InstallationError("Codex CLI was not found on PATH")
                codex_adapter = CodexAdapter(
                    executable=Path("codex"),
                    python_executable=(
                        Path(sys.executable) if python_executable is None else python_executable
                    ).resolve(),
                )
            adapters.append(codex_adapter)
        else:
            adapters.append(
                discover_cursor_adapter(
                    environment=environment,
                    python_executable=python_executable,
                )
            )
    return tuple(adapters)


def _active_profiles(
    *,
    paths: RuntimePaths,
    environment: Mapping[str, str] | None,
    python_executable: Path | None,
    selected: tuple[ClaudeCodeAdapter | CodexAdapter | CursorAdapter, ...],
    selected_states: Mapping[str, HostRegistrationState],
) -> set[str]:
    states = dict(selected_states)
    selected_by_profile = {adapter.profile: adapter for adapter in selected}
    if "claude-code" not in states:
        claude = discover_claude_code_adapter(
            environment=environment,
            python_executable=python_executable,
        )
        if claude is not None:
            states["claude-code"] = claude.registration_state()
    integration_state = load_host_integration_state(paths)
    if "cursor" not in states:
        cursor = selected_by_profile.get("cursor")
        if cursor is None:
            cursor = discover_cursor_adapter(
                environment=environment,
                python_executable=python_executable,
            )
        states["cursor"] = cursor.registration_state()
    for profile, state in states.items():
        if profile not in selected_states and state is HostRegistrationState.FOREIGN:
            continue
        if profile in {"codex", "cursor"}:
            continue
        if profile not in selected_states and state is HostRegistrationState.STALE_OWNED:
            raise InstallationError(
                f"other Harness host profile is stale: {profile}; run harness install --host {profile}"
            )
    active = {
        profile
        for profile, state in states.items()
        if profile not in {"codex", "cursor"}
        and state in {HostRegistrationState.CURRENT, HostRegistrationState.STALE_OWNED}
    }
    active.update(integration_state.profiles & {"codex", "cursor"})
    return active


def _registered_workspaces(paths: RuntimePaths) -> tuple[WorkspaceRecord, ...]:
    if not paths.database.exists():
        return ()
    try:
        # Cursor project preflight runs before daemon restart/migration, so this
        # listing must accept a migratable older schema. Workspace identity
        # columns have been stable since schema v2.
        connection = connect_database_read_only(paths.database, allow_older_schema=True)
        try:
            present = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'workspaces'"
            ).fetchone()
            if present is None:
                return ()
            return list_workspaces(connection)
        finally:
            connection.close()
    except (DatabaseError, sqlite3.DatabaseError, OSError) as exc:
        raise InstallationError("registered Workspaces could not be enumerated") from exc


def _registered_hidden_workspace_roots(paths: RuntimePaths) -> frozenset[Path]:
    """Return live registration roots whose Project is already Hidden."""
    return frozenset(root for _, root in _registered_hidden_project_roots(paths))


def _hidden_project_representative_roots(paths: RuntimePaths) -> tuple[Path, ...]:
    """Return one deterministic Workspace root for each registered Hidden Project."""
    representatives: dict[str, Path] = {}
    for project_id, root in _registered_hidden_project_roots(paths):
        representatives.setdefault(project_id, root)
    return tuple(representatives.values())


def _registered_hidden_project_roots(paths: RuntimePaths) -> tuple[tuple[str, Path], ...]:
    if not paths.database.exists():
        return ()
    try:
        connection = connect_database_read_only(paths.database, allow_older_schema=True)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                ).fetchall()
            }
            if not {"projects", "workspaces"}.issubset(tables):
                return ()
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(projects)").fetchall()
            }
            if "visibility_mode" not in columns:
                return ()
            rows = connection.execute(
                """
                SELECT w.project_id, w.workspace_root
                FROM workspaces AS w
                JOIN projects AS p ON p.id = w.project_id
                WHERE p.visibility_mode = 'hidden'
                ORDER BY w.project_id, w.id
                """
            ).fetchall()
            return tuple((str(project_id), Path(str(root))) for project_id, root in rows)
        finally:
            connection.close()
    except (DatabaseError, sqlite3.DatabaseError, OSError) as exc:
        raise InstallationError("registered Project visibility could not be inspected") from exc


def _reconcile_remaining_profiles(
    paths: RuntimePaths,
    workspaces: tuple[WorkspaceRecord, ...],
    profiles: tuple[str, ...],
) -> SkillCleanupResult:
    removed = 0
    exclude_changed = 0
    for workspace in workspaces:
        result = request_workspace_skills_reconcile(
            paths.socket,
            (
                WorkspaceHint(
                    path=workspace.workspace_root,
                    source="uninstall-workspace-root",
                    match_mode=WorkspaceHintMatchMode.ROOT,
                ),
            ),
            profiles,
        )
        removed += result.removed
        exclude_changed += int(result.exclude_changed)
    return SkillCleanupResult(
        schema_version=SCHEMA_VERSION,
        workspace_count=len(workspaces),
        cleaned_workspace_count=len(workspaces),
        skipped_workspace_count=0,
        removed=removed,
        exclude_changed_count=exclude_changed,
    )


def _empty_cleanup() -> SkillCleanupResult:
    return SkillCleanupResult(
        schema_version=SCHEMA_VERSION,
        workspace_count=0,
        cleaned_workspace_count=0,
        skipped_workspace_count=0,
        removed=0,
        exclude_changed_count=0,
    )


def _combined_cleanup(
    removed: SkillCleanupResult, reconciled: SkillCleanupResult
) -> SkillCleanupResult:
    return SkillCleanupResult(
        schema_version=max(removed.schema_version, reconciled.schema_version),
        workspace_count=max(removed.workspace_count, reconciled.workspace_count),
        cleaned_workspace_count=max(
            removed.cleaned_workspace_count, reconciled.cleaned_workspace_count
        ),
        skipped_workspace_count=max(
            removed.skipped_workspace_count, reconciled.skipped_workspace_count
        ),
        removed=removed.removed + reconciled.removed,
        exclude_changed_count=(removed.exclude_changed_count + reconciled.exclude_changed_count),
    )


def _host_label(adapter: ClaudeCodeAdapter | CodexAdapter | CursorAdapter) -> str:
    if adapter.profile == "claude-code":
        return "Claude Code"
    if adapter.profile == "codex":
        return "Codex"
    return "Cursor"


def _ensure_current_daemon(
    paths: RuntimePaths,
    environment: Mapping[str, str] | None,
) -> RuntimeDiagnosticsResult:
    """Reuse a current daemon or replace one owned by an older installed runtime."""
    expected = current_runtime_identity()
    ensure_canonical_daemon(paths, environment=environment)
    try:
        diagnostics = request_runtime_diagnostics(paths.socket)
    except IpcRemoteError as exc:
        if exc.code != "invalid_request":
            raise
        # The immediately preceding supported Linux release used protocol v1 and
        # clean shutdown, but did not yet expose runtime_diagnostics. Verify that
        # bounded legacy status is readable before treating this as an upgrade.
        legacy_status = request_status(paths.socket)
        if legacy_status.schema_version > SCHEMA_VERSION:
            raise InstallationError(
                "running Harness daemon uses a database schema newer than this installation supports"
            ) from exc
        return _restart_daemon_under_current_runtime(paths, environment, expected)

    if diagnostics.schema_version > SCHEMA_VERSION:
        raise InstallationError(
            "running Harness daemon uses a database schema newer than this installation supports"
        )
    if _daemon_matches_runtime(diagnostics, expected):
        return diagnostics
    return _restart_daemon_under_current_runtime(paths, environment, expected)


def _daemon_matches_runtime(
    diagnostics: RuntimeDiagnosticsResult, expected: RuntimeIdentity
) -> bool:
    return (
        diagnostics.schema_version == SCHEMA_VERSION
        and diagnostics.package_version == expected.package_version
        and diagnostics.python_executable == expected.python_executable
        and diagnostics.code_sha256 == expected.code_sha256
    )


def _restart_daemon_under_current_runtime(
    paths: RuntimePaths,
    environment: Mapping[str, str] | None,
    expected: RuntimeIdentity,
) -> RuntimeDiagnosticsResult:
    shutdown = request_shutdown(paths.socket)
    if not shutdown.accepted:
        raise InstallationError("stale Harness daemon rejected upgrade shutdown")
    _wait_for_daemon_shutdown(paths.socket)
    ensure_canonical_daemon(paths, environment=environment)
    refreshed = request_runtime_diagnostics(paths.socket)
    if not _daemon_matches_runtime(refreshed, expected):
        raise InstallationError("Harness daemon did not restart under the current installation")
    return refreshed


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
    _require_safe_database_state(paths)
    _preflight_skill_registry_purge(environment)


def _require_safe_database_state(paths: RuntimePaths) -> None:
    try:
        preflight_canonical_database_state(paths)
    except RuntimeStateError as exc:
        raise InstallationError(str(exc)) from exc


def _purge_canonical_database(paths: RuntimePaths) -> None:
    state_directory = paths.database.parent
    if not state_directory.exists():
        return
    ensure_private_state_directory(state_directory)
    _require_safe_database_state(paths)
    for candidate in canonical_database_purge_candidates(paths):
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
            *canonical_database_purge_candidates(paths),
            canonical_database_lock_path(paths),
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
        return default_skill_registry(home=home, environment=values)
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
