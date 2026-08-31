import errno
import json
import os
import sys
from argparse import ArgumentParser
from importlib.metadata import version as distribution_version
from pathlib import Path
from signal import SIGINT, SIGTERM, getsignal, signal
from threading import Event
from time import monotonic, sleep
from types import FrameType

from harness.builtin_skills import BuiltinSkillError, sync_builtin_skills
from harness.codex_adapter import discover_codex_adapter
from harness.cursor_adapter import (
    CursorProjectRuntimeStatus,
    cursor_project_enable_command,
    discover_cursor_adapter,
    find_isolated_development_root,
)
from harness.daemon import DaemonError, serve_daemon
from harness.daemon_autostart import ensure_canonical_daemon
from harness.doctor import DoctorReport, run_doctor_checks, run_system_doctor
from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationState,
    IntegrationChange,
)
from harness.host_integration_state import load_host_integration_state
from harness.installation import InstallationError, install_harness, uninstall_harness
from harness.ipc import (
    IpcError,
    IpcTransportError,
    VisibilityResult,
    WorkspaceScanResult,
    WorkspaceSearchResult,
    WorkspaceStatusResult,
    request_dashboard_url,
    request_set_visibility,
    request_shutdown,
    request_workspace_scan,
    request_workspace_search,
    request_workspace_skills_reconcile,
    request_workspace_status,
)
from harness.recovery import RecoveryError, create_database_backup, restore_database_backup
from harness.registry import WorkspaceRecord
from harness.runtime_paths import (
    RuntimePathError,
    default_runtime_paths,
    ensure_private_state_directory,
)
from harness.search import DEFAULT_SEARCH_LIMIT
from harness.skill_runtime import (
    SkillRuntimeError,
    active_skill_profiles_for_runtime,
    supported_skill_profiles,
    validate_skill_definitions_for_profiles,
)
from harness.skills import SkillError, default_skill_registry, load_skill_registry
from harness.storage import DatabaseError
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_DOCTOR_RUNTIME_SCOPE = "Doctor scope: SQLite runtime only."
_DOCTOR_DATABASE_SCOPE = "Doctor scope: SQLite runtime + selected initialized database."
_FAILURE_DETAIL_MAX_LENGTH = 1024
_RECOVERY_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_RECOVERY_SHUTDOWN_POLL_SECONDS = 0.05


def _parser(program: str, description: str) -> ArgumentParser:
    parser = ArgumentParser(prog=program, description=description)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {distribution_version('harness')}",
    )
    return parser


def _print_database_report(report: DoctorReport, database_path: Path) -> int:
    if report.database_error is not None:
        print(f"Database: FAIL ({database_path}: {report.database_error})")
        return 1

    status = report.database_status
    if status is None:
        print(f"Database: FAIL ({database_path}: inspection produced no result)")
        return 1

    print(f"Database: OK ({database_path})")
    print(f"Database schema: {status.schema_version}")
    print(f"Database journal mode: {status.journal_mode}")
    print(f"Database foreign keys: {'OK' if status.foreign_keys else 'FAIL'}")
    print(f"Database FTS5: {'OK' if status.fts5_available else 'FAIL'}")
    return 0 if status.foreign_keys and status.fts5_available else 1


def _run_doctor(
    database_path: Path | None = None,
    *,
    runtime_only: bool = False,
) -> int:
    if database_path is None and not runtime_only:
        system_report = run_system_doctor()
        for check in system_report.checks:
            print(f"{check.name}: {check.severity.value} ({check.detail})")
        print(
            "Doctor summary: "
            f"{system_report.ok_count} OK, "
            f"{system_report.warning_count} WARN, "
            f"{system_report.failure_count} FAIL"
        )
        return 1 if system_report.failure_count else 0

    report = run_doctor_checks() if database_path is None else run_doctor_checks(database_path)
    result = 0
    if report.sqlite_error is not None:
        print(f"SQLite runtime: FAIL ({report.sqlite_error})")
        print("FTS5: UNKNOWN")
        result = 1
    else:
        print(f"SQLite runtime: OK (version {report.sqlite_version})")
        if report.fts5_available:
            print("FTS5: OK")
        else:
            print("FTS5: FAIL (not available in this SQLite runtime)")
            result = 1

    if database_path is None:
        print(_DOCTOR_RUNTIME_SCOPE)
        return result

    result = max(result, _print_database_report(report, database_path))
    print(_DOCTOR_DATABASE_SCOPE)
    return result


def _run_backup(archive_path: Path, database_path: Path | None) -> int:
    try:
        paths = default_runtime_paths()
        database = paths.database if database_path is None else database_path.absolute()
        if database_path is None:
            ensure_private_state_directory(database.parent)
        result = create_database_backup(database, archive_path)
    except (DatabaseError, RecoveryError, RuntimePathError, OSError) as exc:
        print(f"Harness backup: FAIL ({exc})")
        return 1
    print(f"Harness backup: OK ({result.path})")
    print(f"Database schema: {result.manifest.schema_version}")
    print(f"Runtime version: {result.manifest.package_version}")
    print(f"Database SHA-256: {result.manifest.database_sha256}")
    return 0


def _run_restore(
    archive_path: Path,
    database_path: Path | None,
    socket_path: Path | None,
    *,
    allow_runtime_mismatch: bool,
) -> int:
    try:
        if database_path is not None and socket_path is None:
            raise RecoveryError("--database restore requires the matching --socket path")
        defaults = default_runtime_paths()
        database = defaults.database if database_path is None else database_path.absolute()
        socket = defaults.socket if socket_path is None else socket_path.absolute()
        if database_path is None:
            ensure_private_state_directory(database.parent)
        _stop_daemon_for_restore(socket)
        result = restore_database_backup(
            database,
            archive_path,
            allow_runtime_mismatch=allow_runtime_mismatch,
        )
    except (DatabaseError, RecoveryError, RuntimePathError, IpcError, OSError) as exc:
        print(f"Harness restore: FAIL ({exc})")
        return 1
    print(f"Harness restore: OK ({result.database_path})")
    print(f"Database schema: {result.manifest.schema_version}")
    if result.previous_state_backup is not None:
        print(f"Previous state backup: {result.previous_state_backup}")
    print("Harness daemon: stopped; the next normal Harness command starts it with restored state.")
    return 0


def _stop_daemon_for_restore(socket_path: Path) -> None:
    try:
        socket_path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RecoveryError("Harness daemon socket could not be inspected for restore") from exc
    try:
        request_shutdown(socket_path)
    except IpcTransportError as exc:
        cause = exc.__cause__
        if not isinstance(cause, OSError) or cause.errno not in {
            errno.ENOENT,
            errno.ECONNREFUSED,
        }:
            raise
        return
    deadline = monotonic() + _RECOVERY_SHUTDOWN_TIMEOUT_SECONDS
    while True:
        try:
            socket_path.lstat()
        except FileNotFoundError:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise RecoveryError("Harness daemon did not stop cleanly for restore")
        sleep(min(_RECOVERY_SHUTDOWN_POLL_SECONDS, remaining))


def _print_workspace_status(status: WorkspaceStatusResult) -> None:
    print(f"Project: {status.project_id}")
    print(f"Workspace: {status.workspace_id}")
    print(f"Workspace root: {status.workspace_root}")
    print(f"Visibility: {status.visibility_mode}")
    print(f"Git HEAD: {status.head if status.head is not None else '(unborn)'}")
    print(f"Git branch: {status.branch if status.branch is not None else '(detached)'}")
    print(f"Dirty paths: {status.dirty_path_count}")
    print(f"Indexed files: {status.indexed_file_count}")
    print(f"Schema: {status.schema_version}")


def _print_cursor_reload_guidance(*, expect_harness: bool) -> None:
    print("Cursor restart required: fully quit and reopen Cursor after MCP config changes.")
    if expect_harness:
        print("Cursor verification: agent mcp list-tools harness")
        print(
            "Cursor IDE: production MCP is the project harness server in this window's "
            ".cursor/mcp.json. Leftover user-harness is not Workspace identity. "
            "Isolated Harness source checkout uses harness-dev."
        )
    else:
        print("Cursor verification: agent mcp list (confirm Harness is absent)")


def _print_cursor_manual_enable(commands: tuple[str, ...]) -> None:
    if not commands:
        return
    print("Cursor CLI was not found; enable each project MCP, then fully quit and reopen Cursor:")
    for command in commands:
        print(f"  {command}")


def _print_codex_reload_guidance(*, expect_harness: bool) -> None:
    print(
        "Codex restart required: fully quit and reopen the Codex client after project MCP "
        "config changes, then create a new task; existing tasks keep their original instruction "
        "snapshot."
    )
    if expect_harness:
        print("Codex trust: open and trust the Workspace so project .codex/config.toml is loaded.")
        print("Codex verification: run `codex mcp get harness --json` from the Workspace root.")
        print(
            "Codex task verification: in the new task, `project_status` must be the first "
            "project action, before shell search, browser inspection, or changes."
        )
    else:
        print(
            "Codex verification: confirm the Harness-owned project .codex/config.toml was removed."
        )


def _print_workspace_scan(result: WorkspaceScanResult) -> None:
    print(f"Project: {result.project_id} ({'created' if result.project_created else 'existing'})")
    print(
        f"Workspace: {result.workspace_id} "
        f"({'created' if result.workspace_created else 'existing'})"
    )
    print(f"Workspace root: {result.workspace_root}")
    print(f"Visibility: {result.visibility_mode}")
    print(f"Indexed files: {result.file_count}")
    print(f"Added: {result.added}")
    print(f"Updated: {result.updated}")
    print(f"Removed: {result.removed}")
    print(f"Schema: {result.schema_version}")


def _print_visibility(result: VisibilityResult) -> None:
    print(f"Project: {result.project_id}")
    print(f"Workspace: {result.workspace_id}")
    print(f"Workspace root: {result.workspace_root}")
    print(f"Visibility: {result.visibility_mode}")
    print(f"Hidden instruction paths: {result.projected_path_count}")
    print(f"Materialized: {result.materialized}")
    print(f"Removed: {result.removed}")
    print(f"Git exclude changed: {'yes' if result.exclude_changed else 'no'}")
    print(f"SCM write enforcement: {result.scm_write_enforcement}")
    if result.visibility_mode == "hidden":
        print("Cursor does not host-block git commit, push, or pull requests.")
    print(f"Schema: {result.schema_version}")


def _print_workspace_search(result: WorkspaceSearchResult) -> None:
    print(f"Project: {result.project_id}")
    print(f"Workspace: {result.workspace_id}")
    print(f"Workspace root: {result.workspace_root}")
    print(f"Matches: {len(result.results)}")
    for hit in result.results:
        relative_path = json.dumps(hit.relative_path, ensure_ascii=False)
        print(f"{relative_path}\t{hit.kind.value}\t{hit.size_bytes}\t{hit.match_kind.value}")
    print(f"Schema: {result.schema_version}")


def _bounded_failure(prefix: str, detail: str) -> int:
    detail = detail.replace("\r", "\\r").replace("\n", "\\n")
    if len(detail) > _FAILURE_DETAIL_MAX_LENGTH:
        detail = f"{detail[: _FAILURE_DETAIL_MAX_LENGTH - 3]}..."
    print(f"{prefix}: FAIL ({detail})")
    return 1


def _status_failure(detail: str) -> int:
    return _bounded_failure("Harness status", detail)


def _scan_failure(detail: str) -> int:
    return _bounded_failure("Harness scan", detail)


def _visibility_failure(detail: str) -> int:
    return _bounded_failure("Harness visibility", detail)


def _install_failure(detail: str) -> int:
    return _bounded_failure("Harness install", detail)


def _uninstall_failure(detail: str) -> int:
    return _bounded_failure("Harness uninstall", detail)


def _search_failure(detail: str) -> int:
    return _bounded_failure("Harness search", detail)


def _dashboard_failure(detail: str) -> int:
    return _bounded_failure("Harness dashboard", detail)


def _skills_failure(detail: str) -> int:
    return _bounded_failure("Harness skills", detail)


def _run_skills_list() -> int:
    try:
        registry = default_skill_registry()
        definitions = load_skill_registry(registry)
    except SkillError as exc:
        return _skills_failure(str(exc))
    print(f"Skill registry: {registry}")
    print(f"Skills: {len(definitions)}")
    for definition in definitions:
        description = dict(definition.frontmatter_text_fields).get("description", "")
        if description:
            print(f"{definition.skill_id}	{description}")
        else:
            print(definition.skill_id)
    return 0


def _run_skills_sync() -> int:
    registry = default_skill_registry()
    try:
        result = sync_builtin_skills(registry)
    except BuiltinSkillError as exc:
        return _skills_failure(str(exc))
    print(f"Skill registry: {registry}")
    print(f"Built-in skills: {len(result.skill_ids)}")
    print(f"Installed: {result.installed}")
    print(f"Updated: {result.updated}")
    print(f"Unchanged: {result.unchanged}")
    if result.adopted:
        print(f"Adopted exact built-ins: {result.adopted}")
    if result.retired:
        print(f"Retired: {result.retired}")
    if result.released:
        print(f"Released: {result.released}")
    return 0


def _run_skills_validate() -> int:
    registry = default_skill_registry()
    try:
        definitions = load_skill_registry(registry)
        profiles = tuple(sorted(supported_skill_profiles()))
        validate_skill_definitions_for_profiles(definitions, profiles)
    except SkillError as exc:
        return _skills_failure(str(exc))
    print(f"Skill registry: {registry}")
    print(f"Skills: {len(definitions)}")
    print(f"Host profiles: {', '.join(profiles)}")
    print("Skill validation: OK")
    return 0


def _canonical_socket() -> Path:
    defaults = default_runtime_paths()
    ensure_canonical_daemon(defaults)
    return defaults.socket


def _run_status(workspace_location: Path, socket_path: Path | None) -> int:
    if socket_path is None:
        try:
            socket_path = _canonical_socket()
        except (RuntimePathError, IpcError) as exc:
            return _status_failure(str(exc))

    try:
        location = workspace_location.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _status_failure(f"workspace path cannot be resolved: {workspace_location}: {exc}")
    if not location.is_dir():
        return _status_failure(f"workspace path is not a directory: {location}")

    try:
        status = request_workspace_status(
            socket_path,
            [
                WorkspaceHint(
                    path=location,
                    source="cli-location",
                    match_mode=WorkspaceHintMatchMode.LOCATION,
                )
            ],
        )
    except IpcError as exc:
        return _status_failure(str(exc))

    _print_workspace_status(status)
    return 0


def _isolated_development_host_lifecycle_error() -> str | None:
    root = os.environ.get("HARNESS_DEV_ROOT")
    if not root:
        return None
    return (
        "install/uninstall is refused while HARNESS_DEV_ROOT is set because it would mutate "
        f"the user-global host MCP from isolated development ({root})"
    )


def _isolated_development_canonical_scan_error(
    location: Path, *, global_dogfood: bool = False
) -> str | None:
    overlay_root = find_isolated_development_root(location)
    if overlay_root is None:
        if global_dogfood:
            return "--global-dogfood is valid only for a Harness source checkout overlay"
        return None
    if global_dogfood:
        if os.environ.get("HARNESS_DEV_ROOT"):
            return (
                "--global-dogfood requires the tool-installed Harness outside "
                "scripts/dev/HARNESS_DEV_ROOT"
            )
        try:
            interpreter = Path(sys.executable).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return f"tool-installed Harness interpreter cannot be resolved: {exc}"
        if interpreter.is_relative_to(overlay_root):
            return (
                "--global-dogfood requires the tool-installed Harness; "
                f"checkout interpreter refused: {interpreter}"
            )
        return None
    configured = os.environ.get("HARNESS_DEV_ROOT")
    if configured:
        try:
            if Path(configured).expanduser().resolve(strict=True) == overlay_root:
                return None
        except (OSError, RuntimeError):
            pass
    return (
        "this Harness source checkout is reserved for isolated development; "
        "use scripts/dev harness scan instead of a system Harness install"
    )


def _run_scan(
    workspace_location: Path,
    socket_path: Path | None,
    *,
    global_dogfood: bool = False,
) -> int:
    try:
        location = workspace_location.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _scan_failure(f"workspace path cannot be resolved: {workspace_location}: {exc}")
    if not location.is_dir():
        return _scan_failure(f"workspace path is not a directory: {location}")
    blocked = None
    try:
        blocked = _isolated_development_canonical_scan_error(
            location, global_dogfood=global_dogfood
        )
    except HostIntegrationError as exc:
        return _scan_failure(str(exc))
    if blocked is not None:
        return _scan_failure(blocked)

    if socket_path is None:
        try:
            socket_path = _canonical_socket()
        except (RuntimePathError, IpcError) as exc:
            return _scan_failure(str(exc))

    try:
        result = request_workspace_scan(socket_path, location)
    except IpcError as exc:
        return _scan_failure(str(exc))

    try:
        overlay_root = find_isolated_development_root(result.workspace_root)
    except HostIntegrationError as exc:
        return _scan_failure(
            f"index reconciliation succeeded but isolated-development overlay could not be inspected: {exc}"
        )
    if global_dogfood:
        if overlay_root != result.workspace_root:
            return _scan_failure(
                "source checkout overlay changed during global dogfood registration; "
                "host and skill reconciliation was not attempted"
            )
        if result.visibility_mode != "normal":
            return _scan_failure(
                "global dogfood requires Normal visibility because host policy reconciliation "
                "is intentionally skipped"
            )
        _print_workspace_scan(result)
        print("Global dogfood: index registered; host and skill reconciliation skipped")
        return 0
    if overlay_root == result.workspace_root:
        try:
            sync_builtin_skills(default_skill_registry())
            development_profiles = active_skill_profiles_for_runtime(
                default_runtime_paths().database
            )
            development_skills = request_workspace_skills_reconcile(
                socket_path,
                [
                    WorkspaceHint(
                        path=result.workspace_root,
                        source="scan-result-root",
                        match_mode=WorkspaceHintMatchMode.ROOT,
                    )
                ],
                development_profiles,
            )
        except (BuiltinSkillError, IpcError, RuntimePathError, SkillRuntimeError) as exc:
            return _scan_failure(
                "index reconciliation succeeded but isolated-development skills could not "
                f"be reconciled: {exc}"
            )
        _print_workspace_scan(result)
        print("Development skill profiles: " + ", ".join(development_profiles))
        print(f"Relevant skills: {len(development_skills.selected_skill_ids)}")
        print(f"Skills materialized: {development_skills.materialized}")
        print(f"Skills removed: {development_skills.removed}")
        print(f"Skills unchanged: {development_skills.unchanged}")
        print("Host configuration reconciliation skipped: isolated-development checkout overlay")
        return 0

    active_profiles: list[str] = []

    try:
        integration_state = load_host_integration_state(default_runtime_paths())
    except (HostIntegrationError, RuntimePathError) as exc:
        return _scan_failure(
            f"index reconciliation succeeded but host integration intent could not be inspected: {exc}"
        )

    codex_project_change = IntegrationChange.UNCHANGED
    if integration_state.includes("codex"):
        codex = discover_codex_adapter()
        if codex is None:
            return _scan_failure(
                "index reconciliation succeeded but the configured Codex integration could not "
                "be reconciled because the Codex CLI was not found on PATH"
            )
        try:
            codex_project_change = codex.reconcile_project(
                result.workspace_root, hidden=result.visibility_mode == "hidden"
            )
        except HostIntegrationError as exc:
            return _scan_failure(
                f"index reconciliation succeeded but Codex project MCP failed: {exc}"
            )
        active_profiles.append(codex.profile)

    cursor = discover_cursor_adapter()
    cursor_project_change = IntegrationChange.UNCHANGED
    cursor_runtime_manual: tuple[str, ...] = ()
    try:
        cursor_intent = integration_state.includes("cursor")
        cursor_state = cursor.registration_state()
    except (HostIntegrationError, RuntimePathError) as exc:
        return _scan_failure(
            f"index reconciliation succeeded but Cursor integration could not be inspected: {exc}"
        )
    if cursor_state is HostRegistrationState.FOREIGN:
        return _scan_failure(
            "index reconciliation succeeded but Cursor global MCP name 'harness' is not "
            "owned by Harness"
        )
    if cursor_intent:
        try:
            cursor_project_change = cursor.reconcile_project(result.workspace_root)
            runtime = cursor.enable_and_verify_project_mcp(result.workspace_root)
        except HostIntegrationError as exc:
            return _scan_failure(
                f"index reconciliation succeeded but Cursor project MCP failed: {exc}"
            )
        if runtime.status is CursorProjectRuntimeStatus.MANUAL:
            command = runtime.enable_command or cursor_project_enable_command(result.workspace_root)
            cursor_runtime_manual = (command,)
        active_profiles.append(cursor.profile)

    skills = None
    if active_profiles:
        try:
            skills = request_workspace_skills_reconcile(
                socket_path,
                [
                    WorkspaceHint(
                        path=result.workspace_root,
                        source="scan-result-root",
                        match_mode=WorkspaceHintMatchMode.ROOT,
                    )
                ],
                tuple(active_profiles),
            )
        except IpcError as exc:
            return _scan_failure(
                f"index reconciliation succeeded but skill projection failed: {exc}"
            )

    _print_workspace_scan(result)
    if skills is not None:
        print(f"Relevant skills: {len(skills.selected_skill_ids)}")
        print(f"Skills materialized: {skills.materialized}")
        print(f"Skills removed: {skills.removed}")
        print(f"Skills unchanged: {skills.unchanged}")
    if cursor_runtime_manual:
        _print_cursor_manual_enable(cursor_runtime_manual)
        _print_cursor_reload_guidance(expect_harness=True)
    elif cursor_project_change is IntegrationChange.CHANGED:
        _print_cursor_reload_guidance(expect_harness=True)
    if codex_project_change is IntegrationChange.CHANGED:
        _print_codex_reload_guidance(expect_harness=True)
    return 0


def _run_visibility(
    visibility_mode: str, workspace_location: Path, socket_path: Path | None
) -> int:
    try:
        location = workspace_location.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _visibility_failure(
            f"workspace path cannot be resolved: {workspace_location}: {exc}"
        )
    if not location.is_dir():
        return _visibility_failure(f"workspace path is not a directory: {location}")
    if socket_path is None:
        try:
            socket_path = _canonical_socket()
        except (RuntimePathError, IpcError) as exc:
            return _visibility_failure(str(exc))
    try:
        result = request_set_visibility(socket_path, location, visibility_mode)
    except IpcError as exc:
        return _visibility_failure(str(exc))
    _print_visibility(result)
    return 0


def _print_unavailable_workspaces(workspaces: tuple[WorkspaceRecord, ...]) -> None:
    if not workspaces:
        return
    print(f"Unavailable workspaces skipped: {len(workspaces)}")
    for workspace in workspaces:
        print(f"  {workspace.workspace_id} ({workspace.workspace_root})")


def _run_install(*, host: str) -> int:
    blocked = _isolated_development_host_lifecycle_error()
    if blocked is not None:
        return _install_failure(blocked)
    try:
        result = install_harness(host=host)
    except (InstallationError, HostIntegrationError) as exc:
        return _install_failure(str(exc))
    print(f"Harness host: {result.host_profile}")
    print(f"MCP registration: {result.registration_change.value}")
    print(
        "Built-in skills: "
        f"{len(result.builtin_skills.skill_ids)} "
        f"(installed {result.builtin_skills.installed}, updated {result.builtin_skills.updated})"
    )
    if len(result.hosts) > 1:
        for item in result.hosts:
            print(
                f"Host {item.host_profile}: {item.registration_change.value}; "
                f"project overrides changed: {item.project_change_count}"
            )
    cursor_result = next((item for item in result.hosts if item.host_profile == "cursor"), None)
    codex_result = next((item for item in result.hosts if item.host_profile == "codex"), None)
    if cursor_result is not None and len(result.hosts) == 1:
        print(f"Cursor project overrides changed: {cursor_result.project_change_count}")
        if cursor_result.project_runtime_verified:
            print(f"Cursor project MCP tools verified: {cursor_result.project_runtime_verified}")
    if codex_result is not None and len(result.hosts) == 1:
        print(f"Codex project overrides changed: {codex_result.project_change_count}")
    print(f"Daemon schema: {result.daemon_status.schema_version}")
    print(f"Daemon runtime: {result.daemon_status.package_version}")
    print(f"Daemon Python: {result.daemon_status.python_executable}")
    print(f"Registered projects: {result.daemon_status.project_count}")
    print(f"Registered workspaces: {result.daemon_status.workspace_count}")
    print("Diagnostics: harness doctor")
    if cursor_result is not None:
        _print_cursor_manual_enable(cursor_result.manual_enable_commands)
        if (
            cursor_result.registration_change is IntegrationChange.CHANGED
            or cursor_result.project_change_count
            or cursor_result.manual_enable_commands
        ):
            _print_cursor_reload_guidance(expect_harness=True)
    if codex_result is not None and (
        codex_result.registration_change is IntegrationChange.CHANGED
        or codex_result.project_change_count
    ):
        _print_codex_reload_guidance(expect_harness=True)
    _print_unavailable_workspaces(result.unavailable_workspaces)
    print("Harness install: OK")
    return 0


def _run_uninstall(*, host: str, purge: bool) -> int:
    blocked = _isolated_development_host_lifecycle_error()
    if blocked is not None:
        return _uninstall_failure(blocked)
    try:
        result = uninstall_harness(host=host, purge=purge)
    except (InstallationError, HostIntegrationError) as exc:
        return _uninstall_failure(str(exc))
    print(f"Harness host: {result.host_profile}")
    print(f"MCP registration removal: {result.registration_change.value}")
    if len(result.hosts) > 1:
        for item in result.hosts:
            print(
                f"Host {item.host_profile}: {item.registration_change.value}; "
                f"project overrides changed: {item.project_change_count}"
            )
    cursor_result = next((item for item in result.hosts if item.host_profile == "cursor"), None)
    codex_result = next((item for item in result.hosts if item.host_profile == "codex"), None)
    if cursor_result is not None and len(result.hosts) == 1:
        print(f"Cursor project overrides changed: {cursor_result.project_change_count}")
    if codex_result is not None and len(result.hosts) == 1:
        print(f"Codex project overrides changed: {codex_result.project_change_count}")
    print(f"Generated skills removed: {result.skill_cleanup.removed}")
    print(f"Workspaces cleaned: {result.skill_cleanup.cleaned_workspace_count}")
    print(f"Workspaces skipped safely: {result.skill_cleanup.skipped_workspace_count}")
    print(f"Project Intelligence: {'purged' if result.purged else 'preserved'}")
    if cursor_result is not None and (
        cursor_result.registration_change is IntegrationChange.CHANGED
        or cursor_result.project_change_count
    ):
        _print_cursor_reload_guidance(expect_harness=False)
    if codex_result is not None and (
        codex_result.registration_change is IntegrationChange.CHANGED
        or codex_result.project_change_count
    ):
        _print_codex_reload_guidance(expect_harness=False)
    _print_unavailable_workspaces(result.unavailable_workspaces)
    print("Harness uninstall: OK")
    return 0


def _run_dashboard(socket_path: Path | None) -> int:
    if socket_path is None:
        try:
            socket_path = _canonical_socket()
        except (RuntimePathError, IpcError) as exc:
            return _dashboard_failure(str(exc))

    try:
        dashboard = request_dashboard_url(socket_path)
    except IpcError as exc:
        return _dashboard_failure(str(exc))
    print(f"Harness dashboard: {dashboard.url}")
    return 0


def _run_search(
    query: str,
    workspace_location: Path,
    socket_path: Path | None,
    limit: int,
) -> int:
    if socket_path is None:
        try:
            socket_path = _canonical_socket()
        except (RuntimePathError, IpcError) as exc:
            return _search_failure(str(exc))

    try:
        location = workspace_location.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _search_failure(f"workspace path cannot be resolved: {workspace_location}: {exc}")
    if not location.is_dir():
        return _search_failure(f"workspace path is not a directory: {location}")

    try:
        result = request_workspace_search(
            socket_path,
            [
                WorkspaceHint(
                    path=location,
                    source="cli-location",
                    match_mode=WorkspaceHintMatchMode.LOCATION,
                )
            ],
            query,
            limit=limit,
        )
    except IpcError as exc:
        return _search_failure(str(exc))

    _print_workspace_search(result)
    return 0


def _run_daemon(database_path: Path, socket_path: Path) -> int:
    stop_event = Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop_event.set()

    previous_handlers = {
        SIGINT: getsignal(SIGINT),
        SIGTERM: getsignal(SIGTERM),
    }
    signal(SIGINT, request_stop)
    signal(SIGTERM, request_stop)
    try:
        serve_daemon(database_path, socket_path, stop_event=stop_event)
    except (DaemonError, DatabaseError, IpcError, OSError) as exc:
        print(f"Harness daemon: FAIL ({exc})")
        return 1
    finally:
        signal(SIGINT, previous_handlers[SIGINT])
        signal(SIGTERM, previous_handlers[SIGTERM])
    return 0


def _run_daemon_with_defaults(
    database_path: Path | None,
    socket_path: Path | None,
) -> int:
    uses_default_database = database_path is None
    try:
        if database_path is None or socket_path is None:
            defaults = default_runtime_paths()
            if database_path is None:
                database_path = defaults.database
            if socket_path is None:
                socket_path = defaults.socket
        if uses_default_database:
            ensure_private_state_directory(database_path.parent)
    except RuntimePathError as exc:
        print(f"Harness daemon: FAIL ({exc})")
        return 1

    return _run_daemon(database_path, socket_path)


def harness_main() -> int:
    """Run the Harness CLI."""
    parser = _parser("harness", "Harness CLI. Product runtime is under implementation.")
    subparsers = parser.add_subparsers(dest="command")
    install_parser = subparsers.add_parser(
        "install",
        help="install a supported local Harness integration",
        description=(
            "Prepare the canonical local daemon and idempotently register Harness with one local "
            "host profile. The explicit 'all' selection installs Codex and Cursor."
        ),
    )
    install_parser.add_argument(
        "--host",
        choices=("codex", "cursor", "all"),
        default="cursor",
        help="host integration to install (default: cursor)",
    )
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="remove Harness-owned integration artifacts",
        description=(
            "Remove selected Harness-owned host registration, project MCP configuration, and "
            "generated skills while preserving Project Intelligence by default."
        ),
    )
    uninstall_parser.add_argument(
        "--host",
        choices=("codex", "cursor", "all"),
        default="cursor",
        help="host integration to remove (default: cursor)",
    )
    uninstall_parser.add_argument(
        "--purge",
        action="store_true",
        help="also remove the canonical Harness database after clean daemon shutdown",
    )
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="run read-only Harness operational diagnostics",
        description="Inspect Linux Harness runtime, integration, Project, index, skill, and dashboard state without durable mutation.",
    )
    doctor_scope = doctor_parser.add_mutually_exclusive_group()
    doctor_scope.add_argument(
        "--database",
        type=Path,
        metavar="PATH",
        help="inspect only an existing initialized database plus SQLite runtime prerequisites",
    )
    doctor_scope.add_argument(
        "--runtime-only",
        action="store_true",
        help="check only the ephemeral SQLite/FTS5 runtime without inspecting Harness state",
    )
    backup_parser = subparsers.add_parser(
        "backup",
        help="create a consistent durable-state backup archive",
        description=(
            "Create an atomic archive from a consistent SQLite snapshot, including committed WAL "
            "content, plus schema, runtime-identity, size, and checksum metadata."
        ),
    )
    backup_parser.add_argument(
        "archive",
        type=Path,
        metavar="ARCHIVE",
        help="new backup archive path (existing files are never overwritten)",
    )
    backup_parser.add_argument(
        "--database",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Harness database path",
    )
    restore_parser = subparsers.add_parser(
        "restore",
        help="validate and restore a durable-state backup archive",
        description=(
            "Cleanly stop the selected daemon, validate archive checksum, SQLite integrity, "
            "schema, and runtime identity, then atomically restore the database."
        ),
    )
    restore_parser.add_argument(
        "archive",
        type=Path,
        metavar="ARCHIVE",
        help="Harness backup archive to restore",
    )
    restore_parser.add_argument(
        "--database",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Harness database path",
    )
    restore_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the Unix-domain socket for clean daemon shutdown",
    )
    restore_parser.add_argument(
        "--allow-runtime-mismatch",
        action="store_true",
        help="restore an exact-schema archive created by a different Harness runtime after review",
    )
    status_parser = subparsers.add_parser(
        "status",
        help="show read-only status for one registered Workspace",
        description=(
            "Resolve a filesystem location to a registered Workspace and read compact status "
            "from the per-user Harness daemon."
        ),
    )
    status_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("."),
        metavar="PATH",
        help="location inside the registered Workspace (default: current directory)",
    )
    status_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Unix-domain socket path",
    )
    scan_parser = subparsers.add_parser(
        "scan",
        help="register and deterministically scan one Git Workspace",
        description=(
            "Register or reuse the Git Workspace containing PATH and reconcile its deterministic "
            "local Structural Index through the per-user Harness daemon."
        ),
    )
    scan_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("."),
        metavar="PATH",
        help="location inside the Git Workspace (default: current directory)",
    )
    scan_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Unix-domain socket path",
    )
    scan_parser.add_argument(
        "--global-dogfood",
        action="store_true",
        help=(
            "register the Harness source checkout with a tool-installed runtime while skipping "
            "checkout host and skill reconciliation"
        ),
    )
    visibility_parser = subparsers.add_parser(
        "visibility",
        help="set Project Hidden or Normal visibility for one Git Workspace",
        description=(
            "Operator-only Hidden/Normal switch. Hidden projects local host rules and Git "
            "info/exclude without changing .gitignore. Cursor does not host-block git/PR."
        ),
    )
    visibility_parser.add_argument(
        "mode",
        choices=("hidden", "normal"),
        help="visibility mode to persist for the Project",
    )
    visibility_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("."),
        metavar="PATH",
        help="location inside the Git Workspace (default: current directory)",
    )
    visibility_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Unix-domain socket path",
    )
    search_parser = subparsers.add_parser(
        "search",
        help="search one registered Workspace's current Structural Index",
        description=(
            "Resolve PATH to a registered Workspace and search its current deterministic indexed "
            "paths through the per-user Harness daemon without reading source content."
        ),
    )
    search_parser.add_argument(
        "query",
        metavar="QUERY",
        help="bounded path or identifier query",
    )
    search_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("."),
        metavar="PATH",
        help="location inside the registered Workspace (default: current directory)",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SEARCH_LIMIT,
        metavar="N",
        help=f"maximum results (default: {DEFAULT_SEARCH_LIMIT})",
    )
    search_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Unix-domain socket path",
    )

    skills_parser = subparsers.add_parser(
        "skills",
        help="inspect the canonical Harness skill registry",
        description="Read canonical Harness skills without mutating projects or host state.",
    )
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command")
    skills_subparsers.add_parser(
        "list",
        help="list canonical Harness skills",
        description="List canonical skill ids and portable descriptions in stable order.",
    )
    skills_subparsers.add_parser(
        "sync",
        help="install or update Harness built-in quality skills",
        description="Reconcile the Harness-owned built-in quality pack without overwriting unknown or user-modified skill ids.",
    )
    skills_subparsers.add_parser(
        "validate",
        help="validate canonical skills against supported host surfaces",
        description="Load the canonical registry strictly and validate portable frontmatter against every currently supported Harness host skill surface.",
    )

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="print the daemon-owned local Projects dashboard URL",
        description=(
            "Print the private URL of the daemon-owned loopback Projects dashboard. "
            "The listener starts with the daemon on a fixed loopback port."
        ),
    )
    dashboard_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Unix-domain socket path",
    )

    subparsers.add_parser(
        "mcp",
        help="serve the five bounded Harness MCP tools over stdio",
        description="Serve the Harness model-facing MCP surface over stdio using daemon IPC.",
    )

    args = parser.parse_args()
    if args.command == "install":
        return _run_install(host=args.host)
    if args.command == "uninstall":
        return _run_uninstall(host=args.host, purge=args.purge)
    if args.command == "doctor":
        return _run_doctor(args.database, runtime_only=args.runtime_only)
    if args.command == "backup":
        return _run_backup(args.archive, args.database)
    if args.command == "restore":
        return _run_restore(
            args.archive,
            args.database,
            args.socket,
            allow_runtime_mismatch=args.allow_runtime_mismatch,
        )
    if args.command == "status":
        return _run_status(args.path, args.socket)
    if args.command == "scan":
        return _run_scan(args.path, args.socket, global_dogfood=args.global_dogfood)
    if args.command == "visibility":
        return _run_visibility(args.mode, args.path, args.socket)
    if args.command == "search":
        return _run_search(args.query, args.path, args.socket, args.limit)
    if args.command == "skills":
        if args.skills_command == "list":
            return _run_skills_list()
        if args.skills_command == "sync":
            return _run_skills_sync()
        if args.skills_command == "validate":
            return _run_skills_validate()
        skills_parser.print_help()
        return 0
    if args.command == "dashboard":
        return _run_dashboard(args.socket)

    if args.command == "mcp":
        from harness.mcp_bridge import run_mcp_server

        run_mcp_server()
        return 0

    parser.print_help()
    return 0


def harnessd_main() -> int:
    """Run the bounded Harness daemon entrypoint."""
    parser = _parser("harnessd", "Harness daemon. Broader product runtime is under implementation.")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser(
        "serve",
        help="serve the implemented local IPC status, search, and scan paths",
        description=(
            "Serve bounded local IPC status, indexed-path search, and deterministic scan paths "
            "using canonical per-user database and socket defaults unless explicitly overridden."
        ),
    )
    serve_parser.add_argument(
        "--database",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Harness database path",
    )
    serve_parser.add_argument(
        "--socket",
        type=Path,
        metavar="PATH",
        help="override the canonical per-user Unix-domain socket path",
    )

    args = parser.parse_args()
    if args.command == "serve":
        return _run_daemon_with_defaults(args.database, args.socket)

    parser.print_help()
    return 0
