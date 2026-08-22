from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

import harness.daemon_autostart as daemon_autostart
from harness.daemon_autostart import (
    DaemonAutostartError,
    start_canonical_daemon,
    transport_error_allows_autostart,
)
from harness.ipc import IpcTransportError, StatusResult
from harness.storage import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX daemon autostart slice")


def _transport_error(error_number: int) -> IpcTransportError:
    try:
        raise IpcTransportError("transport failed") from OSError(error_number, "transport")
    except IpcTransportError as exc:
        return exc


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ECONNREFUSED])
def test_transport_error_allows_autostart_only_for_unavailable_endpoint(error_number: int) -> None:
    assert transport_error_allows_autostart(_transport_error(error_number)) is True


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ETIMEDOUT, errno.ECONNRESET])
def test_transport_error_rejects_ambiguous_or_accepted_endpoint_failures(error_number: int) -> None:
    assert transport_error_allows_autostart(_transport_error(error_number)) is False


def test_start_canonical_daemon_uses_same_python_detached_and_waits_for_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "run" / "harness.sock"
    spawned: list[tuple[list[str], dict[str, object]]] = []
    probes: list[tuple[Path, float]] = []

    def fake_popen(command: list[str], **kwargs: object) -> object:
        spawned.append((command, kwargs))
        return object()

    def probe(path: Path, *, timeout: float) -> StatusResult:
        probes.append((path, timeout))
        return StatusResult(SCHEMA_VERSION, 0, 0)

    monkeypatch.setattr(daemon_autostart.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_autostart, "request_status", probe)

    start_canonical_daemon(socket_path)

    assert spawned == [
        (
            [sys.executable, "-m", "harness.daemon_process", "serve"],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
                "start_new_session": True,
            },
        )
    ]
    assert len(probes) == 1
    assert probes[0][0] == socket_path
    assert 0 < probes[0][1] <= 0.2


def test_start_canonical_daemon_retries_only_endpoint_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "run" / "harness.sock"
    calls = 0

    monkeypatch.setattr(daemon_autostart.subprocess, "Popen", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(daemon_autostart, "sleep", lambda _seconds: None)

    def probe(_path: Path, *, timeout: float) -> StatusResult:
        nonlocal calls
        assert timeout > 0
        calls += 1
        if calls == 1:
            raise _transport_error(errno.ENOENT)
        return StatusResult(SCHEMA_VERSION, 0, 0)

    monkeypatch.setattr(daemon_autostart, "request_status", probe)

    start_canonical_daemon(socket_path)

    assert calls == 2


def test_start_canonical_daemon_fails_closed_on_ambiguous_readiness_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "run" / "harness.sock"
    monkeypatch.setattr(daemon_autostart.subprocess, "Popen", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        daemon_autostart,
        "request_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_transport_error(errno.EACCES)),
    )

    with pytest.raises(DaemonAutostartError, match="readiness transport failed"):
        start_canonical_daemon(socket_path)
