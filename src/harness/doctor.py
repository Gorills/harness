from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic

from harness.cursor_adapter import CursorAdapter, discover_cursor_adapter
from harness.git_workspace import GitWorkspaceError, inspect_git_workspace_runtime_identity
from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationState,
    discover_claude_code_adapter,
)
from harness.index import IndexingError, inspect_workspace_index_freshness
from harness.ipc import (
    IpcError,
    IpcRemoteError,
    RuntimeDiagnosticsResult,
    request_runtime_diagnostics,
)
from harness.registry import RegistryError, list_workspaces
from harness.runtime_identity import RuntimeIdentityError, current_runtime_identity
from harness.runtime_paths import (
    RuntimePathError,
    RuntimePaths,
    default_runtime_paths,
    require_private_runtime_directory,
    require_private_state_directory,
)
from harness.runtime_state import (
    RuntimeStateError,
    canonical_database_purge_candidates,
    preflight_canonical_database_state,
)
from harness.skill_runtime import SkillRuntimeError, inspect_workspace_skills
from harness.skills import SkillError, default_skill_registry, load_skill_registry
from harness.storage import (
    SCHEMA_VERSION,
    DatabaseError,
    DatabaseStatus,
    connect_database_read_only,
    fts5_available,
    inspect_database,
)
from harness.tasks import TaskError

_DOCTOR_WORKSPACE_LIMIT = 16
_DOCTOR_WORKSPACE_DEADLINE_SECONDS = 2.0
_DOCTOR_WORKSPACE_TOTAL_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Runtime and optional persisted-database checks performed by ``harness doctor``."""

    sqlite_version: str
    fts5_available: bool | None
    sqlite_error: str | None = None
    database_status: DatabaseStatus | None = None
    database_error: str | None = None


class DoctorSeverity(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One bounded read-only operational diagnostic."""

    name: str
    severity: DoctorSeverity
    detail: str


@dataclass(frozen=True, slots=True)
class SystemDoctorReport:
    """Full supported Linux operational diagnostics without durable mutation."""

    checks: tuple[DoctorCheck, ...]

    @property
    def failure_count(self) -> int:
        return sum(check.severity is DoctorSeverity.FAIL for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.severity is DoctorSeverity.WARN for check in self.checks)

    @property
    def ok_count(self) -> int:
        return sum(check.severity is DoctorSeverity.OK for check in self.checks)


def run_doctor_checks(database_path: Path | None = None) -> DoctorReport:
    """Check SQLite prerequisites and optionally one database without durable mutation."""
    sqlite_error: str | None = None
    has_fts5: bool | None = None
    try:
        connection = sqlite3.connect(":memory:", autocommit=True)
        try:
            has_fts5 = fts5_available(connection)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        sqlite_error = str(exc)

    database_status: DatabaseStatus | None = None
    database_error: str | None = None
    if database_path is not None:
        try:
            database_status = inspect_database(database_path)
        except (DatabaseError, sqlite3.Error, OSError) as exc:
            database_error = str(exc)

    return DoctorReport(
        sqlite_version=sqlite3.sqlite_version,
        fts5_available=has_fts5,
        sqlite_error=sqlite_error,
        database_status=database_status,
        database_error=database_error,
    )


def run_system_doctor(
    *,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> SystemDoctorReport:
    """Run the complete supported Linux doctor contract read-only and fail-closed."""
    checks: list[DoctorCheck] = []
    stale_notes: list[str] = []

    if sys.platform.startswith("linux") and os.name != "nt" and hasattr(os, "geteuid"):
        checks.append(_check("Platform", DoctorSeverity.OK, "Linux/POSIX runtime supported"))
    else:
        checks.append(
            _check("Platform", DoctorSeverity.FAIL, "supported production target is Linux/POSIX")
        )

    values = os.environ if environment is None else environment
    git_executable = shutil.which("git", path=values.get("PATH"))
    if git_executable is None:
        checks.append(_check("Git", DoctorSeverity.FAIL, "Git executable not found on PATH"))
    else:
        checks.append(_check("Git", DoctorSeverity.OK, f"discovered at {git_executable}"))

    runtime = run_doctor_checks()
    if runtime.sqlite_error is not None:
        checks.append(_check("SQLite runtime", DoctorSeverity.FAIL, runtime.sqlite_error))
        checks.append(_check("FTS5", DoctorSeverity.FAIL, "SQLite runtime probe failed"))
    else:
        checks.append(
            _check("SQLite runtime", DoctorSeverity.OK, f"SQLite {runtime.sqlite_version}")
        )
        if runtime.fts5_available:
            checks.append(_check("FTS5", DoctorSeverity.OK, "available"))
        else:
            checks.append(_check("FTS5", DoctorSeverity.FAIL, "required FTS5 module unavailable"))

    try:
        paths = default_runtime_paths(environment=environment)
    except RuntimePathError as exc:
        checks.append(_check("Canonical paths", DoctorSeverity.FAIL, str(exc)))
        return SystemDoctorReport(tuple(checks))

    state_dir_ok = _inspect_private_state_directory(paths.database.parent, checks)
    runtime_dir_ok = _inspect_private_runtime_directory(paths.socket.parent, checks)
    database_exists = _exists_without_following(paths.database)
    database_files_ok = _inspect_database_files(
        paths, checks, stale_notes=stale_notes, database_exists=database_exists
    )

    adapter = discover_claude_code_adapter(
        environment=environment,
        python_executable=python_executable,
    )
    claude_registration_state: HostRegistrationState | None = None
    if adapter is None:
        checks.append(
            _check("Claude Code adapter", DoctorSeverity.WARN, "Claude CLI not found on PATH")
        )
        checks.append(
            _check(
                "Claude Code MCP registration",
                DoctorSeverity.WARN,
                "not inspectable without Claude CLI",
            )
        )
    else:
        checks.append(
            _check("Claude Code adapter", DoctorSeverity.OK, f"discovered at {adapter.executable}")
        )
        try:
            claude_registration_state = adapter.registration_state()
        except HostIntegrationError as exc:
            checks.append(
                _check("Claude Code MCP registration", DoctorSeverity.FAIL, _bounded_detail(exc))
            )
        else:
            if claude_registration_state is HostRegistrationState.CURRENT:
                checks.append(
                    _check(
                        "Claude Code MCP registration",
                        DoctorSeverity.OK,
                        "current Harness user registration",
                    )
                )
            elif claude_registration_state is HostRegistrationState.ABSENT:
                checks.append(
                    _check(
                        "Claude Code MCP registration",
                        DoctorSeverity.WARN,
                        "Harness registration absent",
                    )
                )
            elif claude_registration_state is HostRegistrationState.STALE_OWNED:
                stale_notes.append("stale Claude Harness MCP registration")
                checks.append(
                    _check(
                        "Claude Code MCP registration",
                        DoctorSeverity.FAIL,
                        "Harness-owned registration points at a different installed runtime",
                    )
                )
            else:
                stale_notes.append("Claude MCP name collision")
                checks.append(
                    _check(
                        "Claude Code MCP registration",
                        DoctorSeverity.FAIL,
                        "non-Harness user registration already owns the name 'harness'",
                    )
                )

    cursor_adapter = discover_cursor_adapter(
        environment=environment,
        python_executable=python_executable,
    )
    cursor_registration_state: HostRegistrationState | None = None
    try:
        cursor_diagnostic = cursor_adapter.registration_diagnostic()
    except HostIntegrationError as exc:
        checks.append(_check("Cursor MCP registration", DoctorSeverity.FAIL, _bounded_detail(exc)))
    else:
        cursor_registration_state = cursor_diagnostic.state
        configured_python = cursor_diagnostic.configured_python or "<missing>"
        if cursor_registration_state is HostRegistrationState.CURRENT:
            checks.append(
                _check(
                    "Cursor MCP registration",
                    DoctorSeverity.OK,
                    f"current at {cursor_diagnostic.path}; configured Python: "
                    f"{configured_python}; expected Python: {cursor_diagnostic.expected_python}",
                )
            )
        elif cursor_registration_state is HostRegistrationState.ABSENT:
            checks.append(
                _check(
                    "Cursor MCP registration",
                    DoctorSeverity.OK,
                    "Harness Cursor integration is not configured; "
                    f"expected Python: {cursor_diagnostic.expected_python}; "
                    "remediation: harness install --host cursor",
                )
            )
        elif cursor_registration_state is HostRegistrationState.STALE_OWNED:
            stale_notes.append("stale Cursor Harness MCP registration")
            checks.append(
                _check(
                    "Cursor MCP registration",
                    DoctorSeverity.FAIL,
                    f"stale Harness runtime at {cursor_diagnostic.path}; expected Python: "
                    f"{cursor_diagnostic.expected_python}; configured Python: {configured_python}; "
                    "remediation: harness install --host cursor",
                )
            )
        else:
            stale_notes.append("Cursor MCP name collision")
            checks.append(
                _check(
                    "Cursor MCP registration",
                    DoctorSeverity.FAIL,
                    f"foreign server named 'harness' at {cursor_diagnostic.path}; remediation: "
                    "rename or remove the foreign entry before harness install --host cursor",
                )
            )

    diagnostics = _inspect_daemon(
        paths.socket,
        runtime_dir_ok=runtime_dir_ok,
        python_executable=python_executable,
        checks=checks,
        stale_notes=stale_notes,
    )

    database_connection: sqlite3.Connection | None = None
    if database_exists and state_dir_ok and database_files_ok:
        database_report = run_doctor_checks(paths.database)
        if database_report.database_error is not None:
            checks.append(_check("Database", DoctorSeverity.FAIL, database_report.database_error))
        elif database_report.database_status is None:
            checks.append(_check("Database", DoctorSeverity.FAIL, "inspection produced no status"))
        else:
            status = database_report.database_status
            database_problems: list[str] = []
            if status.schema_version != SCHEMA_VERSION:
                database_problems.append(
                    f"schema {status.schema_version} != supported {SCHEMA_VERSION}"
                )
            if status.journal_mode != "wal":
                database_problems.append(f"journal mode is {status.journal_mode}, expected wal")
            if not status.foreign_keys:
                database_problems.append("foreign key enforcement unavailable")
            if not status.fts5_available:
                database_problems.append("FTS5 unavailable")
            if database_problems:
                checks.append(_check("Database", DoctorSeverity.FAIL, "; ".join(database_problems)))
            else:
                checks.append(
                    _check(
                        "Database",
                        DoctorSeverity.OK,
                        f"schema {status.schema_version}, WAL, foreign keys, FTS5",
                    )
                )
                try:
                    database_connection = connect_database_read_only(paths.database)
                except (DatabaseError, sqlite3.Error, OSError) as exc:
                    checks.append(
                        _check("Database read-only access", DoctorSeverity.FAIL, str(exc))
                    )
    elif database_exists:
        checks.append(
            _check("Database", DoctorSeverity.FAIL, "canonical database artifacts are unsafe")
        )
    else:
        checks.append(_check("Database", DoctorSeverity.WARN, "canonical database not initialized"))

    registry_root: Path | None
    try:
        registry_root = _skill_registry_root(environment)
    except (SkillError, RuntimeError) as exc:
        registry_root = None
        registry_ok = False
        checks.append(
            _check("Skill registry permissions", DoctorSeverity.FAIL, _bounded_detail(exc))
        )
    else:
        registry_ok = _inspect_skill_registry_permissions(registry_root, checks)
    if registry_ok and registry_root is not None:
        try:
            definitions = load_skill_registry(registry_root)
        except SkillError as exc:
            registry_ok = False
            checks.append(_check("Skill registry", DoctorSeverity.FAIL, str(exc)))
        else:
            checks.append(
                _check(
                    "Skill registry",
                    DoctorSeverity.OK,
                    f"{len(definitions)} canonical skills at {registry_root}",
                )
            )

    if database_connection is None:
        checks.append(
            _check("Projects", DoctorSeverity.WARN, "no readable canonical Project registry")
        )
        checks.append(
            _check("Index state", DoctorSeverity.WARN, "no readable Workspaces to inspect")
        )
        checks.append(
            _check("Generated skills", DoctorSeverity.WARN, "no readable Workspaces to inspect")
        )
    else:
        try:
            try:
                database_connection.execute("BEGIN")
                _inspect_projects_and_workspaces(
                    database_connection,
                    active_profiles=tuple(
                        profile
                        for profile, state in (
                            ("claude-code", claude_registration_state),
                            ("cursor", cursor_registration_state),
                        )
                        if state is HostRegistrationState.CURRENT
                    ),
                    cursor_adapter=cursor_adapter,
                    cursor_registration_state=cursor_registration_state,
                    registry_root=registry_root,
                    registry_ok=registry_ok,
                    checks=checks,
                    stale_notes=stale_notes,
                )
            except (RegistryError, TaskError, sqlite3.DatabaseError) as exc:
                stale_notes.append("Project registry inspection failed")
                checks.append(
                    _check(
                        "Projects",
                        DoctorSeverity.FAIL,
                        "canonical Project registry could not be inspected: "
                        + _bounded_detail(exc),
                    )
                )
                checks.append(
                    _check("Index state", DoctorSeverity.WARN, "Project inspection failed")
                )
                checks.append(
                    _check("Generated skills", DoctorSeverity.WARN, "Project inspection failed")
                )
        finally:
            try:
                if database_connection.in_transaction:
                    database_connection.execute("ROLLBACK")
            finally:
                database_connection.close()

    if diagnostics is None:
        checks.append(
            _check(
                "Dashboard",
                DoctorSeverity.WARN,
                "daemon unavailable; lazy dashboard not inspectable",
            )
        )
    elif diagnostics.dashboard_running:
        checks.append(
            _check("Dashboard", DoctorSeverity.OK, "daemon-owned loopback dashboard is running")
        )
    else:
        checks.append(
            _check(
                "Dashboard",
                DoctorSeverity.OK,
                "daemon dashboard subsystem available; listener is lazily inactive",
            )
        )

    if stale_notes:
        checks.append(
            _check("Stale integrations", DoctorSeverity.WARN, "; ".join(sorted(set(stale_notes))))
        )
    else:
        checks.append(_check("Stale integrations", DoctorSeverity.OK, "none detected"))

    return SystemDoctorReport(tuple(checks))


def _inspect_private_state_directory(path: Path, checks: list[DoctorCheck]) -> bool:
    if not _exists_without_following(path):
        checks.append(
            _check("State permissions", DoctorSeverity.WARN, f"state directory absent: {path}")
        )
        return True
    try:
        require_private_state_directory(path)
    except RuntimePathError as exc:
        checks.append(_check("State permissions", DoctorSeverity.FAIL, str(exc)))
        return False
    checks.append(
        _check("State permissions", DoctorSeverity.OK, f"private current-user directory: {path}")
    )
    return True


def _inspect_private_runtime_directory(path: Path, checks: list[DoctorCheck]) -> bool:
    if not _exists_without_following(path):
        checks.append(
            _check("Runtime permissions", DoctorSeverity.WARN, f"runtime directory absent: {path}")
        )
        return True
    try:
        require_private_runtime_directory(path)
    except RuntimePathError as exc:
        checks.append(_check("Runtime permissions", DoctorSeverity.FAIL, str(exc)))
        return False
    checks.append(
        _check("Runtime permissions", DoctorSeverity.OK, f"private current-user directory: {path}")
    )
    return True


def _inspect_database_files(
    paths: RuntimePaths,
    checks: list[DoctorCheck],
    *,
    stale_notes: list[str],
    database_exists: bool,
) -> bool:
    try:
        preflight_canonical_database_state(paths)
    except RuntimeStateError as exc:
        checks.append(_check("Database files", DoctorSeverity.FAIL, str(exc)))
        return False
    sidecars = canonical_database_purge_candidates(paths)[1:]
    stale_sidecars = [path.name for path in sidecars if _exists_without_following(path)]
    if not database_exists and stale_sidecars:
        stale_notes.append("stale SQLite sidecars without canonical database")
        checks.append(
            _check(
                "Database files",
                DoctorSeverity.WARN,
                "stale sidecars without harness.db: " + ", ".join(stale_sidecars),
            )
        )
        return True
    checks.append(
        _check("Database files", DoctorSeverity.OK, "canonical SQLite artifacts are safe or absent")
    )
    return True


def _inspect_daemon(
    socket_path: Path,
    *,
    runtime_dir_ok: bool,
    python_executable: Path | None,
    checks: list[DoctorCheck],
    stale_notes: list[str],
) -> RuntimeDiagnosticsResult | None:
    if not _exists_without_following(socket_path):
        checks.append(
            _check("Daemon", DoctorSeverity.WARN, "not running; canonical daemon is lazy-started")
        )
        return None
    if not runtime_dir_ok:
        checks.append(
            _check("Daemon", DoctorSeverity.FAIL, "socket exists under an unsafe runtime directory")
        )
        return None
    try:
        metadata = socket_path.lstat()
    except OSError as exc:
        checks.append(_check("Daemon", DoctorSeverity.FAIL, f"socket cannot be inspected: {exc}"))
        return None
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        stale_notes.append("unsafe or stale daemon socket")
        checks.append(
            _check(
                "Daemon", DoctorSeverity.FAIL, "canonical socket identity or permissions are unsafe"
            )
        )
        return None
    try:
        diagnostics = request_runtime_diagnostics(socket_path)
    except IpcRemoteError as exc:
        if exc.code == "invalid_request":
            stale_notes.append("legacy daemon runtime")
            checks.append(
                _check(
                    "Daemon",
                    DoctorSeverity.FAIL,
                    "running daemon predates runtime diagnostics; run 'harness install' "
                    "to restart it under the current installation",
                )
            )
        else:
            stale_notes.append("daemon diagnostics failure")
            checks.append(_check("Daemon", DoctorSeverity.FAIL, _bounded_detail(exc)))
        return None
    except IpcError as exc:
        stale_notes.append("unreachable daemon socket")
        checks.append(
            _check("Daemon", DoctorSeverity.FAIL, f"socket exists but daemon is unreachable: {exc}")
        )
        return None

    try:
        expected = current_runtime_identity()
    except RuntimeIdentityError as exc:
        stale_notes.append("current Harness runtime identity unavailable")
        checks.append(_check("Daemon", DoctorSeverity.FAIL, _bounded_detail(exc)))
        return diagnostics
    expected_python = (
        expected.python_executable
        if python_executable is None
        else os.path.abspath(os.fspath(python_executable))
    )
    problems: list[str] = []
    if diagnostics.schema_version != SCHEMA_VERSION:
        problems.append(f"schema {diagnostics.schema_version} != {SCHEMA_VERSION}")
    if diagnostics.package_version != expected.package_version:
        problems.append(
            f"runtime version {diagnostics.package_version} != installed {expected.package_version}"
        )
    if diagnostics.python_executable != expected_python:
        problems.append("daemon interpreter differs from current installation")
    if diagnostics.code_sha256 != expected.code_sha256:
        problems.append("daemon code fingerprint differs from current installation")
    if problems:
        stale_notes.append("stale daemon runtime")
        checks.append(_check("Daemon", DoctorSeverity.FAIL, "; ".join(problems)))
    else:
        checks.append(
            _check(
                "Daemon",
                DoctorSeverity.OK,
                f"current runtime {diagnostics.package_version} via {diagnostics.python_executable}",
            )
        )
    return diagnostics


def _inspect_projects_and_workspaces(
    connection: sqlite3.Connection,
    *,
    active_profiles: tuple[str, ...],
    cursor_adapter: CursorAdapter,
    cursor_registration_state: HostRegistrationState | None,
    registry_root: Path | None,
    registry_ok: bool,
    checks: list[DoctorCheck],
    stale_notes: list[str],
) -> None:
    workspaces = list_workspaces(connection, limit=_DOCTOR_WORKSPACE_LIMIT)
    project_row = connection.execute("SELECT COUNT(*) FROM projects").fetchone()
    project_count = 0 if project_row is None else int(project_row[0])
    workspace_row = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()
    workspace_count = 0 if workspace_row is None else int(workspace_row[0])
    if project_count == 0 and workspace_count == 0:
        checks.append(
            _check("Projects", DoctorSeverity.WARN, "no registered Projects or Workspaces")
        )
        checks.append(_check("Index state", DoctorSeverity.OK, "no registered Workspaces"))
        checks.append(_check("Generated skills", DoctorSeverity.OK, "no registered Workspaces"))
        return

    inspectable = workspaces
    truncated = workspace_count - len(inspectable)
    overall_deadline = monotonic() + _DOCTOR_WORKSPACE_TOTAL_SECONDS
    live = 0
    unavailable = 0
    mismatched = 0
    fresh_indexes = 0
    stale_indexes = 0
    index_unknown = 0
    current_skills = 0
    stale_skills = 0
    skill_unknown = 0
    cursor_projects_current = 0
    cursor_projects_isolated = 0
    cursor_projects_bad = 0
    cursor_project_checks: list[DoctorCheck] = []

    for position, workspace in enumerate(inspectable):
        if monotonic() >= overall_deadline:
            truncated += len(inspectable) - position
            break
        workspace_deadline = min(overall_deadline, monotonic() + _DOCTOR_WORKSPACE_DEADLINE_SECONDS)
        try:
            identity = inspect_git_workspace_runtime_identity(
                workspace.workspace_root,
                deadline=workspace_deadline,
            )
        except GitWorkspaceError:
            unavailable += 1
            stale_notes.append(f"unavailable Workspace {workspace.workspace_id}")
            continue
        if (
            identity.layout.workspace_root != workspace.workspace_root
            or identity.layout.git_common_dir != workspace.git_common_dir
        ):
            mismatched += 1
            stale_notes.append(f"changed Workspace identity {workspace.workspace_id}")
            continue
        live += 1
        try:
            index = inspect_workspace_index_freshness(
                connection,
                workspace.workspace_id,
                deadline=workspace_deadline,
            )
        except (IndexingError, GitWorkspaceError, RegistryError, sqlite3.DatabaseError):
            index_unknown += 1
        else:
            if index.is_fresh:
                fresh_indexes += 1
            else:
                stale_indexes += 1
                stale_notes.append(f"stale index {workspace.workspace_id}")

        try:
            cursor_project = cursor_adapter.project_registration_diagnostic(
                workspace.workspace_root
            )
        except HostIntegrationError as exc:
            cursor_projects_bad += 1
            issue = _bounded_detail(exc)
            cursor_project_checks.append(
                _check(
                    f"Cursor project MCP override {workspace.workspace_id}",
                    DoctorSeverity.FAIL,
                    issue,
                )
            )
            stale_notes.append(f"Cursor project override unreadable {workspace.workspace_id}")
        else:
            configured_python = cursor_project.configured_python or "<missing>"
            configured_root = cursor_project.configured_workspace_root or "<missing>"
            if cursor_project.isolated_development:
                cursor_projects_isolated += 1
                cursor_project_checks.append(
                    _check(
                        f"Cursor project MCP override {workspace.workspace_id}",
                        DoctorSeverity.OK,
                        f"isolated-development overlay at {cursor_project.path}; "
                        "production Cursor adapter will not mutate it",
                    )
                )
            elif cursor_project.preflight_error is not None:
                cursor_projects_bad += 1
                cursor_project_checks.append(
                    _check(
                        f"Cursor project MCP override {workspace.workspace_id}",
                        DoctorSeverity.FAIL,
                        f"{cursor_project.path}: {cursor_project.preflight_error}; "
                        "remediation: resolve the ownership/adoption issue, then run "
                        "harness install --host cursor",
                    )
                )
                stale_notes.append(f"unsafe Cursor project override {workspace.workspace_id}")
            elif cursor_registration_state is HostRegistrationState.CURRENT:
                if cursor_project.state is HostRegistrationState.CURRENT:
                    cursor_projects_current += 1
                    cursor_project_checks.append(
                        _check(
                            f"Cursor project MCP override {workspace.workspace_id}",
                            DoctorSeverity.OK,
                            f"current at {cursor_project.path}; configured Python: "
                            f"{configured_python}; expected Python: "
                            f"{cursor_project.expected_python}; "
                            "HARNESS_WORKSPACE_ROOT=${workspaceFolder}",
                        )
                    )
                else:
                    cursor_projects_bad += 1
                    cursor_project_checks.append(
                        _check(
                            f"Cursor project MCP override {workspace.workspace_id}",
                            DoctorSeverity.FAIL,
                            f"{cursor_project.path}: {cursor_project.state.value}; expected Python: "
                            f"{cursor_project.expected_python}; configured Python: "
                            f"{configured_python}; expected "
                            "HARNESS_WORKSPACE_ROOT=${workspaceFolder}; configured "
                            f"HARNESS_WORKSPACE_ROOT={configured_root}; remediation: "
                            "harness install --host cursor",
                        )
                    )
                    stale_notes.append(f"stale Cursor project override {workspace.workspace_id}")
            elif cursor_project.state is not HostRegistrationState.ABSENT:
                cursor_projects_bad += 1
                cursor_project_checks.append(
                    _check(
                        f"Cursor project MCP override {workspace.workspace_id}",
                        DoctorSeverity.FAIL,
                        f"{cursor_project.path}: orphaned {cursor_project.state.value} "
                        "project override while global Cursor integration is inactive; "
                        "remediation: harness uninstall --host cursor or "
                        "harness install --host cursor",
                    )
                )
                stale_notes.append(f"orphaned Cursor project override {workspace.workspace_id}")

        if not active_profiles or not registry_ok:
            skill_unknown += 1
            continue
        if registry_root is None:
            skill_unknown += 1
            continue
        try:
            skill = inspect_workspace_skills(
                connection,
                workspace.workspace_id,
                active_profiles,
                registry_root=registry_root,
                deadline=workspace_deadline,
            )
        except (
            SkillRuntimeError,
            GitWorkspaceError,
            RegistryError,
            TaskError,
            sqlite3.DatabaseError,
        ):
            skill_unknown += 1
        else:
            if skill.projection.is_current:
                current_skills += 1
            else:
                stale_skills += 1
                stale_notes.append(f"stale generated skills {workspace.workspace_id}")

    project_severity = (
        DoctorSeverity.FAIL
        if mismatched
        else DoctorSeverity.WARN
        if unavailable or truncated
        else DoctorSeverity.OK
    )
    project_detail = (
        f"{project_count} projects, {workspace_count} workspaces; "
        f"{live} live, {unavailable} unavailable, {mismatched} identity mismatches"
    )
    if truncated:
        project_detail += f"; {truncated} workspaces not inspected (bounded doctor limit)"
    checks.append(_check("Projects", project_severity, project_detail))

    index_severity = (
        DoctorSeverity.WARN if stale_indexes or index_unknown or truncated else DoctorSeverity.OK
    )
    checks.append(
        _check(
            "Index state",
            index_severity,
            f"{fresh_indexes} fresh, {stale_indexes} stale, {index_unknown} unavailable",
        )
    )

    if cursor_projects_bad:
        checks.append(
            _check(
                "Cursor project MCP overrides",
                DoctorSeverity.FAIL,
                f"{cursor_projects_current} current, {cursor_projects_isolated} "
                "isolated-development, "
                f"{cursor_projects_bad} missing/stale/foreign/orphaned/unsafe; "
                "see per-Workspace checks below",
            )
        )
    elif cursor_registration_state is HostRegistrationState.CURRENT:
        cursor_severity = DoctorSeverity.WARN if unavailable or truncated else DoctorSeverity.OK
        checks.append(
            _check(
                "Cursor project MCP overrides",
                cursor_severity,
                f"{cursor_projects_current} current, {cursor_projects_isolated} "
                "isolated-development, 0 missing/stale/foreign; required root contract "
                "is HARNESS_WORKSPACE_ROOT=${workspaceFolder}",
            )
        )
    elif cursor_projects_isolated:
        checks.append(
            _check(
                "Cursor project MCP overrides",
                DoctorSeverity.OK,
                f"{cursor_projects_isolated} isolated-development overlay(s) preserved; "
                "Cursor global Harness integration is inactive",
            )
        )
    else:
        checks.append(
            _check(
                "Cursor project MCP overrides",
                DoctorSeverity.OK,
                "Cursor global Harness integration is inactive and no project overrides were found",
            )
        )

    checks.extend(cursor_project_checks)

    if not active_profiles:
        checks.append(
            _check(
                "Generated skills",
                DoctorSeverity.WARN,
                "expected projection cannot be established without a current supported host",
            )
        )
    elif not registry_ok:
        checks.append(
            _check("Generated skills", DoctorSeverity.FAIL, "canonical skill registry is invalid")
        )
    else:
        skill_severity = (
            DoctorSeverity.WARN if stale_skills or skill_unknown or truncated else DoctorSeverity.OK
        )
        checks.append(
            _check(
                "Generated skills",
                skill_severity,
                f"{current_skills} current, {stale_skills} stale, {skill_unknown} unavailable",
            )
        )


def _inspect_skill_registry_permissions(path: Path, checks: list[DoctorCheck]) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        checks.append(
            _check("Skill registry permissions", DoctorSeverity.FAIL, _bounded_detail(exc))
        )
        return False
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        checks.append(
            _check(
                "Skill registry permissions",
                DoctorSeverity.FAIL,
                "registry must be a real current-user directory without group/other write access",
            )
        )
        return False
    checks.append(
        _check(
            "Skill registry permissions",
            DoctorSeverity.OK,
            f"current-user controlled directory: {path}",
        )
    )
    return True


def _skill_registry_root(environment: Mapping[str, str] | None) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get("HOME")
    home = Path.home() if not configured else Path(configured).expanduser()
    return default_skill_registry(home=home)


def _exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _check(name: str, severity: DoctorSeverity, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, severity=severity, detail=_single_line_detail(detail)[:1024])


def _single_line_detail(detail: str) -> str:
    safe: list[str] = []
    for character in detail:
        if unicodedata.category(character).startswith("C"):
            safe.append(ascii(character)[1:-1])
        else:
            safe.append(character)
    return "".join(safe)


def _bounded_detail(exc: BaseException) -> str:
    detail = str(exc).replace("\r", "\\r").replace("\n", "\\n")
    return detail[:1024] if detail else exc.__class__.__name__
