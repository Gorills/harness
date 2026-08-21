from argparse import ArgumentParser
from importlib.metadata import version as distribution_version
from pathlib import Path
from signal import SIGINT, SIGTERM, getsignal, signal
from threading import Event
from types import FrameType

from harness.daemon import DaemonError, serve_daemon
from harness.doctor import DoctorReport, run_doctor_checks
from harness.ipc import IpcError
from harness.storage import DatabaseError

_DOCTOR_RUNTIME_SCOPE = (
    "Doctor scope: SQLite runtime only; pass --database PATH to inspect an initialized "
    "Harness database."
)
_DOCTOR_DATABASE_SCOPE = (
    "Doctor scope: SQLite runtime + selected initialized database; other checks are not "
    "implemented yet."
)


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

    args = parser.parse_args()
    if args.command == "doctor":
        return _run_doctor(args.database)

    parser.print_help()
    return 0


def harnessd_main() -> int:
    """Run the bounded Harness daemon entrypoint."""
    parser = _parser("harnessd", "Harness daemon. Broader product runtime is under implementation.")
    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser(
        "serve",
        help="serve the implemented local IPC status path",
        description=(
            "Serve the bounded local IPC status path using an explicit database and socket."
        ),
    )
    serve_parser.add_argument("--database", type=Path, required=True, metavar="PATH")
    serve_parser.add_argument("--socket", type=Path, required=True, metavar="PATH")

    args = parser.parse_args()
    if args.command == "serve":
        return _run_daemon(args.database, args.socket)

    parser.print_help()
    return 0
