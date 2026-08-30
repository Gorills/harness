from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from time import monotonic, sleep

import pytest

from harness.ipc import request_shutdown

_DAEMON_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_DAEMON_SHUTDOWN_POLL_SECONDS = 0.05


@pytest.fixture(autouse=True)
def isolate_harness_xdg_from_user_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Keep tests off the caller's canonical Harness state and socket paths."""
    root = tmp_path_factory.mktemp("harness-xdg")
    state = root / "state"
    runtime = root / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
    monkeypatch.delenv("HARNESS_SKILL_REGISTRY", raising=False)

    yield

    session_root = root.parent.resolve()
    socket_paths = {runtime / "harness" / "harness.sock"}
    active_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if active_runtime:
        active_socket = Path(active_runtime) / "harness" / "harness.sock"
        if active_socket.resolve(strict=False).is_relative_to(session_root):
            socket_paths.add(active_socket)
    for socket_path in socket_paths:
        if not socket_path.exists():
            continue
        shutdown = request_shutdown(socket_path)
        assert shutdown.accepted is True
        deadline = monotonic() + _DAEMON_SHUTDOWN_TIMEOUT_SECONDS
        while socket_path.exists() and monotonic() < deadline:
            sleep(_DAEMON_SHUTDOWN_POLL_SECONDS)
        assert not socket_path.exists(), "test Harness daemon did not stop cleanly"
