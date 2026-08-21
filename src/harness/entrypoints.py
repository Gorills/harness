from argparse import ArgumentParser
from importlib.metadata import version as distribution_version


def _parser(program: str, description: str) -> ArgumentParser:
    parser = ArgumentParser(prog=program, description=description)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {distribution_version('harness')}",
    )
    return parser


def harness_main() -> int:
    """Run the bootstrap Harness CLI."""
    parser = _parser("harness", "Harness CLI. Product commands are not implemented yet.")
    parser.parse_args()
    parser.print_help()
    return 0


def harnessd_main() -> int:
    """Run the bootstrap Harness daemon entrypoint."""
    parser = _parser("harnessd", "Harness daemon runtime is not implemented yet.")
    parser.parse_args()
    parser.print_help()
    return 0
