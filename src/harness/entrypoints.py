from argparse import ArgumentParser
from importlib.metadata import version as distribution_version

from harness.doctor import run_doctor_checks

_DOCTOR_SCOPE = "Doctor scope: SQLite runtime only; other checks are not implemented yet."


def _parser(program: str, description: str) -> ArgumentParser:
    parser = ArgumentParser(prog=program, description=description)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {distribution_version('harness')}",
    )
    return parser


def _run_doctor() -> int:
    report = run_doctor_checks()
    if report.sqlite_error is not None:
        print(f"SQLite runtime: FAIL ({report.sqlite_error})")
        print("FTS5: UNKNOWN")
        print(_DOCTOR_SCOPE)
        return 1

    print(f"SQLite runtime: OK (version {report.sqlite_version})")
    if report.fts5_available:
        print("FTS5: OK")
        result = 0
    else:
        print("FTS5: FAIL (not available in this SQLite runtime)")
        result = 1
    print(_DOCTOR_SCOPE)
    return result


def harness_main() -> int:
    """Run the Harness CLI."""
    parser = _parser("harness", "Harness CLI. Product runtime is under implementation.")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check implemented Harness runtime prerequisites",
        description="Check implemented Harness runtime prerequisites without changing durable state.",
    )

    args = parser.parse_args()
    if args.command == "doctor":
        return _run_doctor()

    parser.print_help()
    return 0


def harnessd_main() -> int:
    """Run the bootstrap Harness daemon entrypoint."""
    parser = _parser("harnessd", "Harness daemon runtime is not implemented yet.")
    parser.parse_args()
    parser.print_help()
    return 0
