import shlex
import subprocess
import sys
from collections.abc import Sequence

CHECKS: tuple[tuple[str, ...], ...] = (
    ("uv", "lock", "--check"),
    ("ruff", "format", "--check", "."),
    ("ruff", "check", "."),
    ("mypy",),
    ("pytest",),
    (
        sys.executable,
        "scripts/benchmark_hot_paths.py",
        "--files",
        "100",
        "--iterations",
        "5",
        "--warmup",
        "1",
        "--assert-counters",
    ),
    (sys.executable, "scripts/smoke_wheel.py"),
)


def _run(command: Sequence[str]) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    """Run the repository quality gate."""
    for command in CHECKS:
        _run(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
