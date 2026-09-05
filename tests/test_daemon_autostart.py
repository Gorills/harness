from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from typing import BinaryIO, cast

import pytest

import harness.daemon_autostart as autostart
from harness.daemon_autostart import DaemonAutostartError, ensure_canonical_daemon
from harness.ipc import IpcTransportError, StatusResult
from harness.runtime_paths import InsecureRuntimeDirectoryError, RuntimePaths
from harness.storage import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon autostart slice")


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )


def _ready_status() -> StatusResult:
    return StatusResult(SCHEMA_VERSION, 0, 0)


def _transport_failure(cause: OSError) -> IpcTransportError:
    error = IpcTransportError(f"local IPC transport failed: {cause}")
    error.__cause__ = cause
    return error


class _RunningProcess:
    @staticmethod
    def poll() -> None:
        return None


@pytest.mark.parametrize(
    ("readiness_failure", "ignore_terminate", "runtime_exists"),
    [
        ("timeout", False, False),
        ("timeout", False, True),
        ("transport", False, True),
        ("cancelled", False, True),
        ("timeout", True, True),
    ],
)
def test_failed_autostart_reaps_only_its_delayed_child_before_daemon_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readiness_failure: str,
    ignore_terminate: bool,
    runtime_exists: bool,
) -> None:
    paths = _paths(tmp_path)
    if runtime_exists:
        paths.socket.parent.mkdir(mode=0o700)
    child_ready = tmp_path / "child-ready"
    children: list[subprocess.Popen[bytes]] = []
    outputs: list[BinaryIO] = []
    original_popen = subprocess.Popen
    unrelated = original_popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def spawn(_command: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        output = cast(BinaryIO, kwargs["stdout"])
        script = (
            "import signal,time; from pathlib import Path; "
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_terminate else "")
            + f"Path({str(child_ready)!r}).touch(); time.sleep(60)"
        )
        child = original_popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children.append(child)
        outputs.append(output)
        deadline = monotonic() + 5
        while not child_ready.exists():
            if child.poll() is not None or monotonic() >= deadline:
                pytest.fail("delayed child did not enter its pre-lock startup stage")
            sleep(0.01)
        return child

    probes = 0

    def probe(*_args: object, **_kwargs: object) -> StatusResult:
        nonlocal probes
        probes += 1
        if probes > 1 and readiness_failure == "cancelled":
            raise KeyboardInterrupt("startup cancelled")
        if probes > 1 and readiness_failure == "transport":
            raise _transport_failure(PermissionError(errno.EACCES, "readiness denied"))
        raise _transport_failure(FileNotFoundError(errno.ENOENT, "socket missing"))

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(autostart, "request_status", probe)
    monkeypatch.setattr(autostart, "_DAEMON_START_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(autostart, "_DAEMON_TERMINATE_TIMEOUT_SECONDS", 0.05)
    try:
        expected = {
            "timeout": "did not become ready",
            "transport": "readiness denied",
            "cancelled": "startup cancelled",
        }[readiness_failure]
        error_type = KeyboardInterrupt if readiness_failure == "cancelled" else IpcTransportError
        with pytest.raises(error_type, match=expected):
            ensure_canonical_daemon(paths)
        assert len(children) == 1
        assert children[0].returncode == -(signal.SIGKILL if ignore_terminate else signal.SIGTERM)
        assert outputs[0].closed
        assert not paths.socket.with_name("harness.sock.lock").exists()
        assert unrelated.poll() is None
    finally:
        for child in (*children, unrelated):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_failed_autostart_preserves_readiness_and_cleanup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnstoppableProcess(_RunningProcess):
        @staticmethod
        def terminate() -> None:
            raise PermissionError("child termination denied")

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: UnstoppableProcess())
    monkeypatch.setattr(autostart, "_DAEMON_START_TIMEOUT_SECONDS", 0)
    with pytest.raises(
        DaemonAutostartError, match="did not become ready; spawned Harness daemon cleanup failed"
    ) as failure:
        ensure_canonical_daemon(_paths(tmp_path))
    assert "child termination denied" in str(failure.value)


def test_canonical_autostart_reuses_reachable_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)
    probes: list[Path] = []

    def request_status(socket_path: Path, *, timeout: float) -> StatusResult:
        probes.append(socket_path)
        assert timeout == autostart._DAEMON_PROBE_TIMEOUT_SECONDS
        return _ready_status()

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reachable canonical daemon must not be respawned")

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", unexpected_spawn)

    ensure_canonical_daemon(paths)

    assert probes == [paths.socket]


def test_canonical_autostart_treats_probe_timeout_as_busy_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)

    def request_status(*_args: object, **_kwargs: object) -> StatusResult:
        raise _transport_failure(TimeoutError("IPC receive deadline exceeded"))

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a busy daemon probe timeout must not trigger a duplicate start")

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", unexpected_spawn)

    ensure_canonical_daemon(paths)


def test_canonical_autostart_starts_package_module_when_runtime_directory_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    commands: list[list[str]] = []

    def spawn(command: list[str], **kwargs: object) -> object:
        commands.append(command)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert hasattr(kwargs["stdout"], "write")
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["close_fds"] is True
        assert kwargs["start_new_session"] is True
        paths.socket.parent.mkdir(mode=0o700)
        paths.socket.parent.chmod(0o700)
        return _RunningProcess()

    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", spawn)
    monkeypatch.setattr(autostart, "request_status", lambda *_args, **_kwargs: _ready_status())

    ensure_canonical_daemon(paths)

    assert commands == [[sys.executable, "-m", "harness.daemon_process"]]


def test_canonical_autostart_recovers_confirmed_absence_with_one_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)
    probe_count = 0
    spawn_count = 0

    def request_status(*_args: object, **_kwargs: object) -> StatusResult:
        nonlocal probe_count
        probe_count += 1
        if probe_count == 1:
            raise _transport_failure(
                ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")
            )
        return _ready_status()

    def spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawn_count
        spawn_count += 1
        return _RunningProcess()

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", spawn)

    ensure_canonical_daemon(paths)

    assert probe_count == 2
    assert spawn_count == 1


def test_canonical_autostart_accepts_busy_endpoint_during_readiness_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)
    probe_count = 0
    spawn_count = 0

    def request_status(*_args: object, **_kwargs: object) -> StatusResult:
        nonlocal probe_count
        probe_count += 1
        if probe_count == 1:
            raise _transport_failure(FileNotFoundError(errno.ENOENT, "socket missing"))
        raise _transport_failure(TimeoutError("IPC receive deadline exceeded"))

    def spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawn_count
        spawn_count += 1
        return _RunningProcess()

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", spawn)

    ensure_canonical_daemon(paths)

    assert probe_count == 2
    assert spawn_count == 1


def test_canonical_autostart_does_not_spawn_for_unclassified_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o700)
    paths.socket.parent.chmod(0o700)

    def request_status(*_args: object, **_kwargs: object) -> StatusResult:
        raise _transport_failure(PermissionError(errno.EACCES, "permission denied"))

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unclassified transport failures must fail closed")

    monkeypatch.setattr(autostart, "request_status", request_status)
    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", unexpected_spawn)

    with pytest.raises(IpcTransportError, match="permission denied"):
        ensure_canonical_daemon(paths)


def test_canonical_autostart_rejects_existing_insecure_runtime_directory_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.socket.parent.mkdir(mode=0o755)
    paths.socket.parent.chmod(0o755)

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("insecure runtime directory must fail closed")

    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", unexpected_spawn)

    with pytest.raises(InsecureRuntimeDirectoryError):
        ensure_canonical_daemon(paths)


def test_canonical_autostart_reports_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)

    def failed_spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("process creation denied")

    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", failed_spawn)

    with pytest.raises(DaemonAutostartError, match="could not be started"):
        ensure_canonical_daemon(paths)


def test_canonical_autostart_reports_bounded_child_startup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    outputs: list[BinaryIO] = []

    class FailedProcess:
        @staticmethod
        def poll() -> int:
            return 17

    def spawn(_command: list[str], **kwargs: object) -> FailedProcess:
        output = cast(BinaryIO, kwargs["stdout"])
        output.write(b"Harness daemon: FAIL (database schema 99 is unsupported)\n")
        output.flush()
        outputs.append(output)
        return FailedProcess()

    monkeypatch.setattr("harness.daemon_autostart.subprocess.Popen", spawn)

    with pytest.raises(
        DaemonAutostartError,
        match=r"exited before becoming ready \(exit 17\).*database schema 99",
    ):
        ensure_canonical_daemon(paths)

    assert len(outputs) == 1
    assert outputs[0].closed is True
