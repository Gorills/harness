from __future__ import annotations

import errno
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import BinaryIO

from harness.ipc import IpcTransportError, request_status
from harness.runtime_paths import RuntimePathError, RuntimePaths, require_private_runtime_directory

_DAEMON_PROBE_TIMEOUT_SECONDS = 0.2
_DAEMON_START_TIMEOUT_SECONDS = 10.0
_DAEMON_START_POLL_SECONDS = 0.05
_DAEMON_START_DETAIL_MAX_BYTES = 2048
_DAEMON_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ECONNREFUSED})


class DaemonAutostartError(IpcTransportError):
    """Raised when the canonical per-user daemon cannot be started or become ready."""


@dataclass(frozen=True, slots=True)
class _DaemonLaunch:
    process: subprocess.Popen[bytes]
    output: BinaryIO


def ensure_canonical_daemon(
    paths: RuntimePaths,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Ensure the canonical POSIX daemon is reachable, starting it lazily if needed."""
    runtime_directory = paths.socket.parent
    try:
        require_private_runtime_directory(runtime_directory)
    except RuntimePathError:
        if not _runtime_directory_is_missing(runtime_directory):
            raise
        launch = _start_canonical_daemon(environment=environment)
        try:
            _wait_for_canonical_daemon(paths, launch)
        finally:
            launch.output.close()
        return

    try:
        request_status(paths.socket, timeout=_DAEMON_PROBE_TIMEOUT_SECONDS)
    except IpcTransportError as exc:
        if _transport_error_is_timeout(exc):
            return
        if not _transport_error_proves_daemon_absent(exc):
            raise
        launch = _start_canonical_daemon(environment=environment)
        try:
            _wait_for_canonical_daemon(paths, launch)
        finally:
            launch.output.close()


def _runtime_directory_is_missing(directory: Path) -> bool:
    try:
        directory.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise RuntimePathError("Harness runtime directory could not be inspected") from exc
    return False


def _transport_error_proves_daemon_absent(error: IpcTransportError) -> bool:
    cause = error.__cause__
    return isinstance(cause, OSError) and cause.errno in _DAEMON_ABSENT_ERRNOS


def _transport_error_is_timeout(error: IpcTransportError) -> bool:
    return isinstance(error.__cause__, TimeoutError)


def _start_canonical_daemon(*, environment: Mapping[str, str] | None = None) -> _DaemonLaunch:
    try:
        output = tempfile.TemporaryFile(mode="w+b")
    except OSError as exc:
        raise DaemonAutostartError("Harness daemon could not be started") from exc
    try:
        command = [sys.executable, "-m", "harness.daemon_process"]
        if environment is None:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        else:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=dict(environment),
            )
    except OSError as exc:
        output.close()
        raise DaemonAutostartError("Harness daemon could not be started") from exc
    return _DaemonLaunch(process=process, output=output)


def _wait_for_canonical_daemon(paths: RuntimePaths, launch: _DaemonLaunch) -> None:
    deadline = monotonic() + _DAEMON_START_TIMEOUT_SECONDS
    last_transport_error: IpcTransportError | None = None
    while True:
        failure = _daemon_start_failure(launch)
        if failure is not None:
            raise DaemonAutostartError(failure) from last_transport_error
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DaemonAutostartError(
                "Harness daemon did not become ready"
            ) from last_transport_error

        if _runtime_directory_is_missing(paths.socket.parent):
            sleep(min(_DAEMON_START_POLL_SECONDS, remaining))
            continue

        require_private_runtime_directory(paths.socket.parent)
        try:
            request_status(
                paths.socket,
                timeout=min(_DAEMON_PROBE_TIMEOUT_SECONDS, remaining),
            )
        except IpcTransportError as exc:
            if _transport_error_is_timeout(exc):
                return
            if not _transport_error_proves_daemon_absent(exc):
                raise
            last_transport_error = exc
            sleep(min(_DAEMON_START_POLL_SECONDS, remaining))
            continue
        return


def _daemon_start_failure(launch: _DaemonLaunch) -> str | None:
    return_code = launch.process.poll()
    if return_code is None:
        return None
    try:
        launch.output.flush()
        launch.output.seek(0)
        raw = launch.output.read(_DAEMON_START_DETAIL_MAX_BYTES + 1)
    except OSError:
        raw = b""
    truncated = len(raw) > _DAEMON_START_DETAIL_MAX_BYTES
    decoded = raw[:_DAEMON_START_DETAIL_MAX_BYTES].decode("utf-8", errors="replace")
    detail = " ".join(decoded.split())
    if truncated:
        detail = f"{detail}…" if detail else "…"
    message = f"Harness daemon exited before becoming ready (exit {return_code})"
    return f"{message}: {detail}" if detail else message
