import json
from argparse import ArgumentParser
from importlib.metadata import version as distribution_version
from pathlib import Path
from signal import SIGINT, SIGTERM, getsignal, signal
from threading import Event
from types import FrameType

from harness.daemon import DaemonError, serve_daemon
from harness.daemon_autostart import ensure_canonical_daemon
from harness.doctor import DoctorReport, run_doctor_checks
from harness.ipc import (
    IpcError,
    WorkspaceScanResult,
    WorkspaceSearchResult,
    WorkspaceStatusResult,
    request_dashboard_url,
    request_workspace_scan,
    request_workspace_search,
    request_workspace_status,
)
from harness.runtime_paths import (
    RuntimePathError,
    default_runtime_paths,
    ensure_private_state_directory,
)
from harness.search import DEFAULT_SEARCH_LIMIT
from harness.storage import DatabaseError
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_DOCTOR_RUNTIME_SCOPE = (
    "Doctor scope: SQLite runtime only; pass --database PATH to inspect an initialized "
    "Harness database."
)
_DOCTOR_DATABASE_SCOPE = (
    "Doctor scope: SQLite runtime + selected initialized database; other checks are not "
    "implemented yet."
)
_FAILURE_DETAIL_MAX_LENGTH = 1024


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


def _run_doctor(database_path: Path | None = None) -> int:
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


def _search_failure(detail: str) -> int:
    return _bounded_failure("Harness search", detail)


def _dashboard_failure(detail: str) -> int:
    return _bounded_failure("Harness dashboard", detail)


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


def _run_scan(workspace_location: Path, socket_path: Path | None) -> int:
    if socket_path is None:
        try:
            socket_path = _canonical_socket()
        except (RuntimePathError, IpcError) as exc:
            return _scan_failure(str(exc))

    try:
        location = workspace_location.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return _scan_failure(f"workspace path cannot be resolved: {workspace_location}: {exc}")
    if not location.is_dir():
        return _scan_failure(f"workspace path is not a directory: {location}")

    try:
        result = request_workspace_scan(socket_path, location)
    except IpcError as exc:
        return _scan_failure(str(exc))

    _print_workspace_scan(result)
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
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check implemented Harness runtime prerequisites",
        description="Check implemented Harness prerequisites without changing durable state.",
    )
    doctor_parser.add_argument(
        "--database",
        type=Path,
        metavar="PATH",
        help="inspect an existing initialized Harness database without creating or migrating it",
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

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="show the daemon-owned local Projects dashboard",
        description=(
            "Lazily start the daemon-owned loopback Projects dashboard and print its private URL."
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
    if args.command == "doctor":
        return _run_doctor(args.database)
    if args.command == "status":
        return _run_status(args.path, args.socket)
    if args.command == "scan":
        return _run_scan(args.path, args.socket)
    if args.command == "search":
        return _run_search(args.query, args.path, args.socket, args.limit)
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
