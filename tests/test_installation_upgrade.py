from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import harness.installation as installation
import harness.storage as storage
from harness.builtin_skills import BuiltinSkillSyncResult
from harness.codex_adapter import CodexAdapter
from harness.cursor_adapter import (
    CursorAdapter,
    CursorProjectRuntimeResult,
    CursorProjectRuntimeStatus,
)
from harness.host_adapters import HostRegistrationState, IntegrationChange
from harness.installation import InstallationError
from harness.ipc import (
    IpcRemoteError,
    IpcTransportError,
    RuntimeDiagnosticsResult,
    ShutdownResult,
    StatusResult,
    WorkspaceSkillsResult,
)
from harness.registry import WorkspaceRecord
from harness.runtime_identity import RuntimeIdentity
from harness.runtime_paths import RuntimePaths
from harness.storage import SCHEMA_VERSION, initialize_database


def test_install_and_restore_shutdown_waits_cover_owned_listener_stop_budgets() -> None:
    from harness.daemon import _CLIENT_TIMEOUT_SECONDS
    from harness.dashboard import _DASHBOARD_STOP_TIMEOUT_SECONDS
    from harness.entrypoints import _RECOVERY_SHUTDOWN_TIMEOUT_SECONDS
    from harness.mcp_http_server import _STOP_TIMEOUT_SECONDS as mcp_http_stop_timeout
    from harness.watcher import DEFAULT_WATCH_SCAN_DEADLINE_SECONDS

    owned_stop_budget = (
        _CLIENT_TIMEOUT_SECONDS
        + DEFAULT_WATCH_SCAN_DEADLINE_SECONDS
        + _DASHBOARD_STOP_TIMEOUT_SECONDS
        + mcp_http_stop_timeout
    )
    assert installation._SHUTDOWN_TIMEOUT_SECONDS >= owned_stop_budget
    assert _RECOVERY_SHUTDOWN_TIMEOUT_SECONDS >= owned_stop_budget


def test_install_all_keeps_established_codex_then_cursor_adapter_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovered: list[str] = []
    codex = CodexAdapter(executable=Path("/codex"), python_executable=Path("/python"))
    cursor = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    def discover_codex(**_kwargs: object) -> CodexAdapter:
        discovered.append("codex")
        return codex

    def discover_cursor(**_kwargs: object) -> CursorAdapter:
        discovered.append("cursor")
        return cursor

    monkeypatch.setattr(installation, "discover_codex_adapter", discover_codex)
    monkeypatch.setattr(installation, "discover_cursor_adapter", discover_cursor)

    selected = installation._selected_adapters(
        "all",
        environment={},
        python_executable=Path("/python"),
        codex_cli_required=True,
    )

    assert discovered == ["codex", "cursor"]
    assert [adapter.profile for adapter in selected] == ["codex", "cursor"]


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


def test_post_install_skill_timeout_retries_once_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = WorkspaceRecord("workspace-1", "project-1", root, root / ".git")
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    calls = 0

    monkeypatch.setattr(installation, "find_isolated_development_root", lambda _root: None)

    def reconcile(*_args: object, **_kwargs: object) -> WorkspaceSkillsResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IpcRemoteError(
                "skill_integration_timeout",
                "Workspace skill reconciliation exceeded the daemon execution deadline",
            )
        return WorkspaceSkillsResult(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace.workspace_id,
            selected_skill_ids=(),
            materialized=0,
            removed=0,
            unchanged=0,
            exclude_changed=False,
        )

    monkeypatch.setattr(installation, "request_workspace_skills_reconcile", reconcile)

    result = installation._reconcile_remaining_profiles(
        paths,
        (workspace,),
        ("codex", "cursor"),
        install_host="all",
    )

    assert calls == 2
    assert result.cleaned_workspace_count == 1


def test_install_retries_post_mutation_skill_lock_timeout_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = WorkspaceRecord("workspace-install", "project-install", root, root / ".git")
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))
    events: list[str] = []
    reconcile_calls = 0

    monkeypatch.setattr(installation, "_require_runtime_prerequisites", lambda: None)
    monkeypatch.setattr(installation, "_selected_adapters", lambda *_args, **_kwargs: (adapter,))
    monkeypatch.setattr(installation, "_runtime_paths", lambda _environment: paths)
    monkeypatch.setattr(installation, "_require_safe_database_state", lambda _paths: None)
    monkeypatch.setattr(installation, "_active_profiles", lambda **_kwargs: {"cursor"})
    monkeypatch.setattr(installation, "_registered_workspaces", lambda _paths: (workspace,))
    monkeypatch.setattr(installation, "_live_hidden_workspace_roots", lambda _paths: frozenset())
    monkeypatch.setattr(installation, "_hidden_project_representative_roots", lambda _paths: ())
    monkeypatch.setattr(installation, "find_isolated_development_root", lambda _root: None)
    monkeypatch.setattr(
        installation, "_skill_registry_path", lambda _environment: tmp_path / "skills"
    )
    monkeypatch.setattr(
        installation,
        "sync_builtin_skills",
        lambda _path: BuiltinSkillSyncResult(0, 0, 0, 0, 0, 0, ()),
    )
    monkeypatch.setattr(
        installation,
        "_ensure_current_daemon",
        lambda *_args: _diagnostics(python="/python"),
    )

    def add_profiles(*_args: object) -> IntegrationChange:
        events.append("intent")
        return IntegrationChange.CHANGED

    monkeypatch.setattr(
        installation,
        "add_host_profiles",
        add_profiles,
    )
    monkeypatch.setattr(
        CursorAdapter,
        "registration_state",
        lambda _self: HostRegistrationState.CURRENT,
    )
    monkeypatch.setattr(CursorAdapter, "preflight_project_reconcile", lambda *_args: None)

    def register_mcp(_self: CursorAdapter) -> IntegrationChange:
        events.append("register")
        return IntegrationChange.CHANGED

    def reconcile_project(_self: CursorAdapter, _root: Path) -> IntegrationChange:
        events.append("project")
        return IntegrationChange.CHANGED

    def enable_and_verify(
        _self: CursorAdapter,
        workspace_root: Path,
        *,
        environment: object = None,
    ) -> CursorProjectRuntimeResult:
        del environment
        events.append("verify")
        return CursorProjectRuntimeResult(
            workspace_root=workspace_root,
            status=CursorProjectRuntimeStatus.VERIFIED,
            tools=(),
            detail="verified",
        )

    monkeypatch.setattr(CursorAdapter, "register_mcp", register_mcp)
    monkeypatch.setattr(CursorAdapter, "reconcile_project", reconcile_project)
    monkeypatch.setattr(CursorAdapter, "enable_and_verify_project_mcp", enable_and_verify)

    def reconcile(*_args: object, **_kwargs: object) -> WorkspaceSkillsResult:
        nonlocal reconcile_calls
        reconcile_calls += 1
        events.append(f"skills-{reconcile_calls}")
        if reconcile_calls == 1:
            raise IpcRemoteError("skill_integration_timeout", "scan lock deadline exceeded")
        return WorkspaceSkillsResult(
            schema_version=SCHEMA_VERSION,
            workspace_id=workspace.workspace_id,
            selected_skill_ids=(),
            materialized=0,
            removed=0,
            unchanged=0,
            exclude_changed=False,
        )

    monkeypatch.setattr(installation, "request_workspace_skills_reconcile", reconcile)

    result = installation.install_harness(host="cursor")

    assert result.host_profile == "cursor"
    assert events == ["intent", "register", "project", "verify", "skills-1", "skills-2"]


def test_post_install_nontransient_skill_failure_is_contextual_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = WorkspaceRecord("workspace-2", "project-2", root, root / ".git")
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    calls = 0

    monkeypatch.setattr(installation, "find_isolated_development_root", lambda _root: None)

    def reconcile(*_args: object, **_kwargs: object) -> WorkspaceSkillsResult:
        nonlocal calls
        calls += 1
        raise IpcRemoteError(
            "skill_integration_error", "daemon could not reconcile Workspace skills"
        )

    monkeypatch.setattr(installation, "request_workspace_skills_reconcile", reconcile)

    with pytest.raises(InstallationError) as caught:
        installation._reconcile_remaining_profiles(
            paths,
            (workspace,),
            ("codex", "cursor"),
            install_host="all",
        )

    message = str(caught.value)
    assert calls == 1
    assert "post-install project skill reconciliation" in message
    assert f"Workspace {workspace.workspace_id} ({root})" in message
    assert "profiles [codex, cursor]" in message
    assert "skill_integration_error" in message
    assert "Retry: harness install --host all" in message
    assert isinstance(caught.value.__cause__, IpcRemoteError)


def test_post_install_skill_timeout_stops_after_one_retry_with_partial_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = WorkspaceRecord("workspace-timeout", "project-timeout", root, root / ".git")
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    calls = 0

    monkeypatch.setattr(installation, "find_isolated_development_root", lambda _root: None)

    def reconcile(*_args: object, **_kwargs: object) -> WorkspaceSkillsResult:
        nonlocal calls
        calls += 1
        raise IpcRemoteError(
            "skill_integration_timeout",
            "Workspace skill reconciliation exceeded the daemon execution deadline",
        )

    monkeypatch.setattr(installation, "request_workspace_skills_reconcile", reconcile)

    with pytest.raises(InstallationError) as caught:
        installation._reconcile_remaining_profiles(
            paths,
            (workspace,),
            ("codex", "cursor"),
            install_host="all",
        )

    assert calls == 2
    assert "Host integration may be partially updated" in str(caught.value)
    assert "Retry: harness install --host all" in str(caught.value)


def test_post_install_transport_timeout_is_contextual_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = WorkspaceRecord("workspace-3", "project-3", root, root / ".git")
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    calls = 0

    monkeypatch.setattr(installation, "find_isolated_development_root", lambda _root: None)

    def reconcile(*_args: object, **_kwargs: object) -> WorkspaceSkillsResult:
        nonlocal calls
        calls += 1
        raise IpcTransportError("local IPC request timed out")

    monkeypatch.setattr(installation, "request_workspace_skills_reconcile", reconcile)

    with pytest.raises(InstallationError, match="local IPC request timed out"):
        installation._reconcile_remaining_profiles(
            paths,
            (workspace,),
            ("cursor",),
            install_host="cursor",
        )

    assert calls == 1


def test_post_install_visibility_failure_names_separate_phase_and_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hidden"
    paths = RuntimePaths(tmp_path / "state" / "harness.db", tmp_path / "run" / "harness.sock")
    error = IpcRemoteError("skill_integration_error", "hidden projection could not be reconciled")
    monkeypatch.setattr(
        installation,
        "request_set_visibility",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(InstallationError) as caught:
        installation._restore_hidden_visibility_after_install(
            paths,
            (root,),
            ("codex", "cursor"),
            host="all",
        )

    message = str(caught.value)
    assert "post-install Hidden visibility restoration" in message
    assert f"Workspace root {root}" in message
    assert "profiles [codex, cursor]" in message
    assert caught.value.__cause__ is error


def test_post_install_cause_is_single_line_and_bounded() -> None:
    message = installation._post_install_failure(
        phase="project skill reconciliation",
        host="cursor",
        profiles=("cursor",),
        cause=IpcTransportError("first line\n" + "x" * 800),
    )

    cause_text = message.split(": ", 1)[1].split(". Host integration", 1)[0]
    assert "\n" not in message
    assert len(cause_text) == 512
    assert cause_text.endswith("...")


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
