from __future__ import annotations

from pathlib import Path

import pytest

import harness.installation as installation
from harness.installation import InstallationError
from harness.ipc import IpcRemoteError, RuntimeDiagnosticsResult, ShutdownResult, StatusResult
from harness.runtime_identity import RuntimeIdentity
from harness.runtime_paths import RuntimePaths
from harness.storage import SCHEMA_VERSION


def _diagnostics(
    *,
    python: str,
    version: str = "1.0.0",
    schema: int = SCHEMA_VERSION,
    code_sha256: str = "a" * 64,
) -> RuntimeDiagnosticsResult:
    return RuntimeDiagnosticsResult(
        schema_version=schema,
        package_version=version,
        python_executable=python,
        code_sha256=code_sha256,
        project_count=2,
        workspace_count=3,
        dashboard_running=False,
    )


def test_install_reuses_current_daemon_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    ensured: list[RuntimePaths] = []
    shutdowns: list[Path] = []
    monkeypatch.setattr(
        installation,
        "current_runtime_identity",
        lambda: RuntimeIdentity("1.0.0", "/current/python", "a" * 64),
    )
    monkeypatch.setattr(
        installation,
        "ensure_canonical_daemon",
        lambda value, environment=None: ensured.append(value),
    )
    monkeypatch.setattr(
        installation,
        "request_runtime_diagnostics",
        lambda _socket: _diagnostics(python="/current/python"),
    )

    def shutdown(socket: Path) -> ShutdownResult:
        shutdowns.append(socket)
        return ShutdownResult(accepted=True)

    monkeypatch.setattr(installation, "request_shutdown", shutdown)

    result = installation._ensure_current_daemon(paths, None)

    assert result.python_executable == "/current/python"
    assert ensured == [paths]
    assert shutdowns == []


def test_install_restarts_daemon_from_stale_interpreter_and_revalidates_exact_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    ensured: list[RuntimePaths] = []
    shutdowns: list[Path] = []
    waits: list[Path] = []
    observed = iter(
        (
            _diagnostics(python="/old/python"),
            _diagnostics(python="/current/python"),
        )
    )
    monkeypatch.setattr(
        installation,
        "current_runtime_identity",
        lambda: RuntimeIdentity("1.0.0", "/current/python", "a" * 64),
    )
    monkeypatch.setattr(
        installation,
        "ensure_canonical_daemon",
        lambda value, environment=None: ensured.append(value),
    )
    monkeypatch.setattr(installation, "request_runtime_diagnostics", lambda _socket: next(observed))

    def shutdown(socket: Path) -> ShutdownResult:
        shutdowns.append(socket)
        return ShutdownResult(accepted=True)

    monkeypatch.setattr(installation, "request_shutdown", shutdown)
    monkeypatch.setattr(installation, "_wait_for_daemon_shutdown", waits.append)

    result = installation._ensure_current_daemon(paths, None)

    assert result.python_executable == "/current/python"
    assert ensured == [paths, paths]
    assert shutdowns == [paths.socket]
    assert waits == [paths.socket]


def test_install_restarts_daemon_when_code_fingerprint_is_stale_in_same_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    ensured: list[RuntimePaths] = []
    shutdowns: list[Path] = []
    observed = iter(
        (
            _diagnostics(python="/current/python", code_sha256="b" * 64),
            _diagnostics(python="/current/python", code_sha256="a" * 64),
        )
    )
    monkeypatch.setattr(
        installation,
        "current_runtime_identity",
        lambda: RuntimeIdentity("1.0.0", "/current/python", "a" * 64),
    )
    monkeypatch.setattr(
        installation,
        "ensure_canonical_daemon",
        lambda value, environment=None: ensured.append(value),
    )
    monkeypatch.setattr(installation, "request_runtime_diagnostics", lambda _socket: next(observed))

    def shutdown(socket: Path) -> ShutdownResult:
        shutdowns.append(socket)
        return ShutdownResult(accepted=True)

    monkeypatch.setattr(installation, "request_shutdown", shutdown)
    monkeypatch.setattr(installation, "_wait_for_daemon_shutdown", lambda _socket: None)

    result = installation._ensure_current_daemon(paths, None)

    assert result.code_sha256 == "a" * 64
    assert ensured == [paths, paths]
    assert shutdowns == [paths.socket]


def test_install_restarts_pre_diagnostics_protocol_v1_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    ensured: list[RuntimePaths] = []
    shutdowns: list[Path] = []
    diagnostics_calls = 0
    monkeypatch.setattr(
        installation,
        "current_runtime_identity",
        lambda: RuntimeIdentity("1.0.0", "/current/python", "a" * 64),
    )
    monkeypatch.setattr(
        installation,
        "ensure_canonical_daemon",
        lambda value, environment=None: ensured.append(value),
    )

    def diagnostics(_socket: Path) -> RuntimeDiagnosticsResult:
        nonlocal diagnostics_calls
        diagnostics_calls += 1
        if diagnostics_calls == 1:
            raise IpcRemoteError("invalid_request", "IPC request is invalid")
        return _diagnostics(python="/current/python")

    monkeypatch.setattr(installation, "request_runtime_diagnostics", diagnostics)
    monkeypatch.setattr(
        installation,
        "request_status",
        lambda _socket: StatusResult(
            schema_version=SCHEMA_VERSION, project_count=2, workspace_count=3
        ),
    )

    def shutdown(socket: Path) -> ShutdownResult:
        shutdowns.append(socket)
        return ShutdownResult(accepted=True)

    monkeypatch.setattr(installation, "request_shutdown", shutdown)
    monkeypatch.setattr(installation, "_wait_for_daemon_shutdown", lambda _socket: None)

    result = installation._ensure_current_daemon(paths, None)

    assert result.python_executable == "/current/python"
    assert diagnostics_calls == 2
    assert ensured == [paths, paths]
    assert shutdowns == [paths.socket]


def test_install_does_not_restart_daemon_for_nonlegacy_diagnostics_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    monkeypatch.setattr(
        installation,
        "current_runtime_identity",
        lambda: RuntimeIdentity("1.0.0", "/current/python", "a" * 64),
    )
    monkeypatch.setattr(installation, "ensure_canonical_daemon", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        installation,
        "request_runtime_diagnostics",
        lambda _socket: (_ for _ in ()).throw(IpcRemoteError("database_error", "broken")),
    )

    with pytest.raises(IpcRemoteError, match="database_error"):
        installation._ensure_current_daemon(paths, None)


def test_install_refuses_daemon_schema_newer_than_current_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    monkeypatch.setattr(
        installation,
        "current_runtime_identity",
        lambda: RuntimeIdentity("1.0.0", "/current/python", "a" * 64),
    )
    monkeypatch.setattr(installation, "ensure_canonical_daemon", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        installation,
        "request_runtime_diagnostics",
        lambda _socket: _diagnostics(
            python="/newer/python",
            version="2.0.0",
            schema=SCHEMA_VERSION + 1,
        ),
    )

    with pytest.raises(InstallationError, match="schema newer"):
        installation._ensure_current_daemon(paths, None)
