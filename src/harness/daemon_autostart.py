from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

from harness.ipc import IpcError, IpcTransportError, request_status

_DAEMON_READY_TIMEOUT_SECONDS = 3.0
_DAEMON_READY_POLL_SECONDS = 0.05
_DAEMON_PROBE_TIMEOUT_SECONDS = 0.2


class DaemonAutostartError(RuntimeError):
    """Raised when the canonical Harness daemon cannot be started and proven ready."""


def transport_error_allows_autostart(error: IpcTransportError) -> bool:
    """Return whether the failed request proves that no local endpoint accepted the connection."""
    cause = error.__cause__
    return isinstance(cause, OSError) and cause.errno in {errno.ENOENT, errno.ECONNREFUSED}


def start_canonical_daemon(socket_path: Path) -> None:
    """Start the installed daemon detached and wait for its bounded status readiness probe."""
    try:
        subprocess.Popen(
            [sys.executable, "-P", "-m", "harness.daemon_process", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise DaemonAutostartError("Harness daemon process could not be started") from exc

    deadline = monotonic() + _DAEMON_READY_TIMEOUT_SECONDS
    last_unavailable: IpcTransportError | None = None
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        try:
            request_status(socket_path, timeout=min(_DAEMON_PROBE_TIMEOUT_SECONDS, remaining))
            return
        except IpcTransportError as exc:
            if not transport_error_allows_autostart(exc):
                raise DaemonAutostartError(
                    f"Harness daemon readiness transport failed: {exc}"
                ) from exc
            last_unavailable = exc
        except IpcError as exc:
            raise DaemonAutostartError(f"Harness daemon readiness failed: {exc}") from exc
        sleep(min(_DAEMON_READY_POLL_SECONDS, max(0.0, deadline - monotonic())))

    detail = f": {last_unavailable}" if last_unavailable is not None else ""
    raise DaemonAutostartError(f"Harness daemon did not become ready{detail}")
