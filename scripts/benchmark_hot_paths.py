from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median
from threading import Event, Lock
from time import monotonic_ns, sleep
from typing import Any, cast
from unittest.mock import patch

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness.daemon import serve_daemon
from harness.index import list_indexed_files, scan_workspace, scan_workspace_paths
from harness.registry import (
    WorkspaceRecord,
    create_project,
    register_workspace,
)
from harness.storage import connect_database, initialize_database
from harness.watcher import (
    WATCH_METADATA_FILE_SAMPLE_LIMIT,
    list_workspace_metadata_directories,
    read_workspace_metadata_token,
)

_PROJECT_STATUS_GIT_BUDGET = 13
_PROJECT_STATUS_IPC_BUDGET = 2
_WATCH_TOKEN_GIT_BUDGET = 0
_INCREMENTAL_SCAN_GIT_BUDGET = 6
_NOOP_SCAN_GIT_BUDGET = 6


@dataclass(frozen=True, slots=True)
class _Fixture:
    root: Path
    database: Path
    workspace: WorkspaceRecord


class _SubprocessCounter:
    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._counts: Counter[str] = Counter()
        self._lock = Lock()
        self._original: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run

    def run(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        command = args[0] if args else kwargs.get("args")
        cwd = kwargs.get("cwd")
        if (
            isinstance(command, Sequence)
            and not isinstance(command, (str, bytes))
            and command
            and command[0] == "git"
            and cwd is not None
            and Path(cwd).resolve() == self._workspace_root
        ):
            key = " ".join(str(item) for item in command)
            with self._lock:
                self._counts[key] += 1
        return self._original(*args, **kwargs)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(sorted(self._counts.items()))


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )


def _git_version() -> str:
    result = subprocess.run(
        ["git", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    try:
        content = cpuinfo.read_text(encoding="utf-8")
    except OSError:
        return platform.processor() or "unknown"
    for line in content.splitlines():
        if line.startswith("model name") and ":" in line:
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _create_fixture(base: Path, file_count: int) -> _Fixture:
    root = base / "workspace"
    root.mkdir()
    _run_git(root, "init", "-q", "-b", "main")
    source = root / "src"
    source.mkdir()
    for index in range(file_count):
        (source / f"module_{index:06d}.py").write_text(
            f"VALUE_{index:06d} = {index}\n",
            encoding="utf-8",
        )
    _run_git(root, "add", "src")
    _run_git(
        root,
        "-c",
        "user.name=Harness Benchmark",
        "-c",
        "user.email=benchmark@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-q",
        "-m",
        "benchmark fixture",
    )

    database = base / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
    finally:
        connection.close()
    return _Fixture(root=root.resolve(), database=database, workspace=workspace)


def _idle_watcher(
    _database_path: Path,
    stop_event: Event,
    _scan_lock: Lock,
    **_kwargs: object,
) -> None:
    stop_event.wait()


def _start_daemon(
    fixture: _Fixture,
    socket_path: Path,
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        serve_daemon,
        fixture.database,
        socket_path,
        stop_event=stop_event,
    )
    deadline = monotonic_ns() + 3_000_000_000
    while not socket_path.exists():
        if future.done():
            future.result()
        if monotonic_ns() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise RuntimeError("benchmark daemon did not start")
        sleep(0.01)
    return stop_event, executor, future


def _stop_daemon(
    stop_event: Event,
    executor: ThreadPoolExecutor,
    future: Future[None],
) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def _duration_summary(samples_ns: Sequence[int]) -> dict[str, float]:
    if not samples_ns:
        raise ValueError("benchmark requires at least one measured sample")
    ordered = sorted(samples_ns)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "min_ms": ordered[0] / 1_000_000,
        "p50_ms": median(ordered) / 1_000_000,
        "p95_ms": ordered[p95_index] / 1_000_000,
        "max_ms": ordered[-1] / 1_000_000,
        "mean_ms": fmean(ordered) / 1_000_000,
    }


def _git_total(counts: dict[str, int]) -> int:
    return sum(counts.values())


def _measure(
    operation: Callable[[], None],
    counter: _SubprocessCounter,
    *,
    iterations: int,
    warmup: int,
) -> tuple[dict[str, float], dict[str, int]]:
    for _ in range(warmup):
        operation()
    counter.reset()
    samples: list[int] = []
    for _ in range(iterations):
        started = monotonic_ns()
        operation()
        samples.append(monotonic_ns() - started)
    return _duration_summary(samples), counter.snapshot()


def _measure_incremental_scan(
    fixture: _Fixture,
    connection: sqlite3.Connection,
    counter: _SubprocessCounter,
    *,
    iterations: int,
    warmup: int,
) -> tuple[dict[str, float], dict[str, int]]:
    target = fixture.root / "src" / "module_000000.py"
    samples: list[int] = []
    total = iterations + warmup
    for index in range(total):
        target.write_text(f"VALUE_000000 = {(index + 1) % 2}\n", encoding="utf-8")
        if index == warmup:
            counter.reset()
        started = monotonic_ns()
        result = scan_workspace_paths(
            connection,
            fixture.workspace.workspace_id,
            ("src/module_000000.py",),
        )
        elapsed = monotonic_ns() - started
        if result.updated != 1:
            raise RuntimeError("benchmark incremental scan did not update the selected path")
        if index >= warmup:
            samples.append(elapsed)
    return _duration_summary(samples), counter.snapshot()


async def _measure_project_status(
    fixture: _Fixture,
    runtime_root: Path,
    counter: _SubprocessCounter,
    iterations: int,
    warmup: int,
) -> tuple[dict[str, float], dict[str, int]]:
    environment = dict(os.environ)
    environment.update(
        {
            "XDG_RUNTIME_DIR": str(runtime_root),
            "XDG_STATE_HOME": str(fixture.database.parent / "state"),
            "HARNESS_HOST_PROFILE": "codex",
            "HARNESS_WORKSPACE_ROOT": str(fixture.root),
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env=environment,
        cwd=str(fixture.root),
    )
    async with Client(stdio_client(parameters)) as client:
        for _ in range(warmup):
            response = await client.call_tool("project_status")
            if response.is_error:
                raise RuntimeError("benchmark project_status warmup failed")
        counter.reset()
        samples: list[int] = []
        for _ in range(iterations):
            started = monotonic_ns()
            response = await client.call_tool("project_status")
            samples.append(monotonic_ns() - started)
            if response.is_error or response.structured_content is None:
                raise RuntimeError("benchmark project_status call failed")
            if response.structured_content.get("workspace_id") != fixture.workspace.workspace_id:
                raise RuntimeError("benchmark project_status resolved the wrong Workspace")
    return _duration_summary(samples), counter.snapshot()


def _per_iteration(total: int, iterations: int) -> int | float:
    quotient, remainder = divmod(total, iterations)
    return quotient if remainder == 0 else total / iterations


def _benchmark_fixture(
    fixture: _Fixture,
    *,
    iterations: int,
    warmup: int,
) -> dict[str, object]:
    counter = _SubprocessCounter(fixture.root)
    runtime_root = fixture.database.parent / "runtime"
    socket_dir = runtime_root / "harness"
    runtime_root.mkdir(mode=0o700)
    socket_dir.mkdir(mode=0o700)
    socket_path = socket_dir / "harness.sock"

    with (
        patch("subprocess.run", counter.run),
        patch("harness.daemon.run_workspace_watcher", _idle_watcher),
    ):
        stop_event, executor, future = _start_daemon(fixture, socket_path)
        try:
            status_latency, status_counts = anyio.run(
                _measure_project_status,
                fixture,
                runtime_root,
                counter,
                iterations,
                warmup,
            )
        finally:
            _stop_daemon(stop_event, executor, future)

        connection = connect_database(fixture.database)
        try:
            metadata_directories = list_workspace_metadata_directories(
                fixture.root,
                deadline=monotonic_ns() / 1_000_000_000 + 10,
            )
            metadata_file_sample = list_indexed_files(
                connection,
                fixture.workspace.workspace_id,
            )[:WATCH_METADATA_FILE_SAMPLE_LIMIT]

            def watch_token() -> None:
                read_workspace_metadata_token(
                    fixture.workspace,
                    metadata_file_sample,
                    deadline=monotonic_ns() / 1_000_000_000 + 10,
                    directory_paths=metadata_directories,
                )

            watch_latency, watch_counts = _measure(
                watch_token,
                counter,
                iterations=iterations,
                warmup=warmup,
            )

            incremental_latency, incremental_counts = _measure_incremental_scan(
                fixture,
                connection,
                counter,
                iterations=iterations,
                warmup=warmup,
            )

            def noop_scan() -> None:
                result = scan_workspace(connection, fixture.workspace.workspace_id)
                if result.added or result.updated or result.removed:
                    raise RuntimeError("benchmark no-op scan unexpectedly changed the index")

            scan_latency, scan_counts = _measure(
                noop_scan,
                counter,
                iterations=iterations,
                warmup=warmup,
            )
        finally:
            connection.close()

    return {
        "project_status": {
            "latency": status_latency,
            "ipc_round_trips_per_iteration": _PROJECT_STATUS_IPC_BUDGET,
            "git_subprocesses_per_iteration": _per_iteration(_git_total(status_counts), iterations),
            "git_commands": status_counts,
        },
        "watcher_idle_token": {
            "latency": watch_latency,
            "git_subprocesses_per_iteration": _per_iteration(_git_total(watch_counts), iterations),
            "git_commands": watch_counts,
        },
        "watcher_incremental_reconcile": {
            "latency": incremental_latency,
            "git_subprocesses_per_iteration": _per_iteration(
                _git_total(incremental_counts), iterations
            ),
            "git_commands": incremental_counts,
            "hashed_paths_per_iteration": 1,
        },
        "authoritative_noop_scan": {
            "latency": scan_latency,
            "git_subprocesses_per_iteration": _per_iteration(_git_total(scan_counts), iterations),
            "git_commands": scan_counts,
        },
    }


def _assert_counter_budgets(results: dict[str, object]) -> None:
    expected = {
        "project_status": (_PROJECT_STATUS_GIT_BUDGET, _PROJECT_STATUS_IPC_BUDGET),
        "watcher_idle_token": (_WATCH_TOKEN_GIT_BUDGET, None),
        "watcher_incremental_reconcile": (_INCREMENTAL_SCAN_GIT_BUDGET, None),
        "authoritative_noop_scan": (_NOOP_SCAN_GIT_BUDGET, None),
    }
    failures: list[str] = []
    for name, (git_budget, ipc_budget) in expected.items():
        measurement = cast(dict[str, object], results[name])
        actual_git = measurement["git_subprocesses_per_iteration"]
        if actual_git != git_budget:
            failures.append(
                f"{name}: expected {git_budget} Git subprocesses, observed {actual_git}"
            )
        if ipc_budget is not None and measurement["ipc_round_trips_per_iteration"] != ipc_budget:
            failures.append(
                f"{name}: expected {ipc_budget} IPC round trips, observed "
                f"{measurement['ipc_round_trips_per_iteration']}"
            )
    if failures:
        raise RuntimeError("performance counter gate failed: " + "; ".join(failures))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure reproducible Harness hot-path latency and subprocess counters."
    )
    parser.add_argument(
        "--files", type=int, default=1000, help="tracked fixture files (default: 1000)"
    )
    parser.add_argument(
        "--iterations", type=int, default=50, help="measured iterations per path (default: 50)"
    )
    parser.add_argument("--warmup", type=int, default=5, help="warmup iterations (default: 5)")
    parser.add_argument(
        "--assert-counters",
        action="store_true",
        help="fail when IPC/Git subprocess counts differ from the recorded baseline",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if os.name == "nt":
        raise RuntimeError("hot-path benchmark currently requires the supported POSIX IPC path")
    arguments = _parser().parse_args(argv)
    if arguments.files <= 0 or arguments.iterations <= 0 or arguments.warmup < 0:
        raise ValueError("files and iterations must be positive; warmup must be non-negative")

    with tempfile.TemporaryDirectory(prefix="harness-hot-paths-") as temporary:
        fixture = _create_fixture(Path(temporary), arguments.files)
        measurements = _benchmark_fixture(
            fixture,
            iterations=arguments.iterations,
            warmup=arguments.warmup,
        )
    if arguments.assert_counters:
        _assert_counter_budgets(measurements)
    output = {
        "schema_version": 1,
        "fixture": {
            "tracked_files": arguments.files,
            "iterations": arguments.iterations,
            "warmup": arguments.warmup,
        },
        "runtime": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "git": _git_version(),
            "cpu_count": os.cpu_count(),
            "cpu_model": _cpu_model(),
        },
        "measurements": measurements,
        "counter_gate": "passed" if arguments.assert_counters else "not_requested",
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
