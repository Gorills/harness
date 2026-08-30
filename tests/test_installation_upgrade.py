from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import harness.installation as installation
import harness.storage as storage
from harness.installation import InstallationError
from harness.ipc import IpcRemoteError, RuntimeDiagnosticsResult, ShutdownResult, StatusResult
from harness.registry import WorkspaceRecord
from harness.runtime_identity import RuntimeIdentity
from harness.runtime_paths import RuntimePaths
from harness.storage import SCHEMA_VERSION, initialize_database


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


def test_registered_workspaces_lists_rows_from_older_supported_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    current = storage.SCHEMA_VERSION
    assert current > 2
    monkeypatch.setattr(storage, "SCHEMA_VERSION", current - 1)
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO projects(id) VALUES ('project')")
        connection.execute(
            """
            INSERT INTO workspaces(id, project_id, workspace_root, git_common_dir)
            VALUES ('workspace', 'project', '/repo', '/repo/.git')
            """
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setattr(storage, "SCHEMA_VERSION", current)

    workspaces = installation._registered_workspaces(
        RuntimePaths(database, tmp_path / "harness.sock")
    )

    assert [workspace.workspace_id for workspace in workspaces] == ["workspace"]
    assert [workspace.workspace_root for workspace in workspaces] == [Path("/repo")]


def test_registered_workspaces_returns_empty_before_workspaces_table_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 1)
    initialize_database(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", SCHEMA_VERSION)

    workspaces = installation._registered_workspaces(
        RuntimePaths(database, tmp_path / "harness.sock")
    )

    assert workspaces == ()


def test_partition_registered_workspaces_skips_unresolvable_roots(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    missing_root = tmp_path / "missing"
    file_root = tmp_path / "not-a-dir"
    file_root.write_text("file\n", encoding="utf-8")
    live = WorkspaceRecord("live-id", "project", live_root, live_root)
    missing = WorkspaceRecord("missing-id", "project", missing_root, missing_root)
    as_file = WorkspaceRecord("file-id", "project", file_root, file_root)

    live_workspaces, unavailable = installation._partition_registered_workspaces(
        (live, missing, as_file)
    )

    assert live_workspaces == (live,)
    assert unavailable == (missing, as_file)


def test_hidden_project_representative_roots_prefers_live_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        installation,
        "_registered_hidden_project_roots",
        lambda _paths: (("project", missing), ("project", live), ("gone", missing)),
    )

    roots = installation._hidden_project_representative_roots(
        RuntimePaths(tmp_path / "db", tmp_path / "sock")
    )

    assert roots == (live,)
