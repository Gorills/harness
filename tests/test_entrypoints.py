import sys
from pathlib import Path

import pytest

import harness.builtin_skills as builtin_module
import harness.entrypoints as entrypoints
from harness.builtin_skills import BUILTIN_SKILLS, BuiltinSkill
from harness.doctor import DoctorCheck, DoctorReport, DoctorSeverity, SystemDoctorReport
from harness.entrypoints import harness_main, harnessd_main
from harness.ipc import (
    DashboardUrlResult,
    IpcTransportError,
    VisibilityResult,
    WorkspaceStatusResult,
)
from harness.runtime_paths import RuntimePaths
from harness.storage import SCHEMA_VERSION, DatabaseStatus
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode


def test_harness_main_lists_status_and_doctor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harness"])

    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Product runtime is under implementation." in output
    assert "doctor" in output
    assert "status" in output
    assert "dashboard" in output
    assert "visibility" in output


def test_harness_doctor_reports_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def successful_checks() -> DoctorReport:
        return DoctorReport(sqlite_version="3.50.4", fts5_available=True)

    monkeypatch.setattr(sys, "argv", ["harness", "doctor", "--runtime-only"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", successful_checks)

    assert harness_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: OK (version 3.50.4)",
        "FTS5: OK",
        "Doctor scope: SQLite runtime only.",
    ]


def test_harness_doctor_fails_when_fts5_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_fts5() -> DoctorReport:
        return DoctorReport(sqlite_version="3.50.4", fts5_available=False)

    monkeypatch.setattr(sys, "argv", ["harness", "doctor", "--runtime-only"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", missing_fts5)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert "SQLite runtime: OK (version 3.50.4)" in output
    assert "FTS5: FAIL" in output


def test_harness_doctor_reports_sqlite_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed_checks() -> DoctorReport:
        return DoctorReport(
            sqlite_version="3.50.4",
            fts5_available=None,
            sqlite_error="probe failed",
        )

    monkeypatch.setattr(sys, "argv", ["harness", "doctor", "--runtime-only"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", failed_checks)

    assert harness_main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: FAIL (probe failed)",
        "FTS5: UNKNOWN",
        "Doctor scope: SQLite runtime only.",
    ]


def test_harness_doctor_reports_full_system_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = SystemDoctorReport(
        checks=(
            DoctorCheck("Platform", DoctorSeverity.OK, "Linux/POSIX runtime supported"),
            DoctorCheck("Daemon", DoctorSeverity.WARN, "not running"),
            DoctorCheck("MCP registration", DoctorSeverity.FAIL, "foreign registration"),
        )
    )
    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    monkeypatch.setattr(entrypoints, "run_system_doctor", lambda: report)

    assert harness_main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "Platform: OK (Linux/POSIX runtime supported)",
        "Daemon: WARN (not running)",
        "MCP registration: FAIL (foreign registration)",
        "Doctor summary: 1 OK, 1 WARN, 1 FAIL",
    ]


def test_harness_doctor_inspects_selected_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "harness.db"

    def successful_checks(path: Path) -> DoctorReport:
        assert path == database_path
        return DoctorReport(
            sqlite_version="3.50.4",
            fts5_available=True,
            database_status=DatabaseStatus(
                schema_version=1,
                sqlite_version="3.50.4",
                journal_mode="wal",
                foreign_keys=True,
                fts5_available=True,
            ),
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "doctor", "--database", str(database_path)],
    )
    monkeypatch.setattr(entrypoints, "run_doctor_checks", successful_checks)

    assert harness_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: OK (version 3.50.4)",
        "FTS5: OK",
        f"Database: OK ({database_path})",
        "Database schema: 1",
        "Database journal mode: wal",
        "Database foreign keys: OK",
        "Database FTS5: OK",
        "Doctor scope: SQLite runtime + selected initialized database.",
    ]


def test_harness_doctor_reports_database_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "missing.db"

    def failed_checks(path: Path) -> DoctorReport:
        assert path == database_path
        return DoctorReport(
            sqlite_version="3.50.4",
            fts5_available=True,
            database_error="unable to open database file",
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "doctor", "--database", str(database_path)],
    )
    monkeypatch.setattr(entrypoints, "run_doctor_checks", failed_checks)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert f"Database: FAIL ({database_path}: unable to open database file)" in output


def test_harness_dashboard_uses_canonical_daemon_and_prints_private_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    autostarted: list[RuntimePaths] = []
    requested: list[Path] = []
    dashboard_url = "http://127.0.0.1:43123/"

    def request_dashboard(ipc_socket: Path) -> DashboardUrlResult:
        requested.append(ipc_socket)
        return DashboardUrlResult(url=dashboard_url)

    monkeypatch.setattr(sys, "argv", ["harness", "dashboard"])
    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "ensure_canonical_daemon", autostarted.append)
    monkeypatch.setattr(entrypoints, "request_dashboard_url", request_dashboard)

    assert harness_main() == 0
    assert autostarted == [defaults]
    assert requested == [defaults.socket]
    assert capsys.readouterr().out.strip() == f"Harness dashboard: {dashboard_url}"


def test_harness_dashboard_explicit_socket_does_not_autostart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "manual.sock"

    def unexpected_autostart(_paths: RuntimePaths) -> None:
        raise AssertionError("explicit socket must not autostart the canonical daemon")

    monkeypatch.setattr(sys, "argv", ["harness", "dashboard", "--socket", str(socket_path)])
    monkeypatch.setattr(entrypoints, "ensure_canonical_daemon", unexpected_autostart)
    monkeypatch.setattr(
        entrypoints,
        "request_dashboard_url",
        lambda path: (
            DashboardUrlResult(url="http://127.0.0.1:43123/")
            if path == socket_path
            else (_ for _ in ()).throw(AssertionError("unexpected socket"))
        ),
    )

    assert harness_main() == 0


def test_harness_dashboard_reports_ipc_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    socket_path = tmp_path / "missing.sock"

    def fail(_socket: Path) -> DashboardUrlResult:
        raise IpcTransportError("dashboard daemon unavailable")

    monkeypatch.setattr(sys, "argv", ["harness", "dashboard", "--socket", str(socket_path)])
    monkeypatch.setattr(entrypoints, "request_dashboard_url", fail)

    assert harness_main() == 1
    assert capsys.readouterr().out.strip() == (
        "Harness dashboard: FAIL (dashboard daemon unavailable)"
    )


def test_harness_status_resolves_location_and_prints_bounded_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    location = workspace_root / "src" / "package"
    location.mkdir(parents=True)
    socket_path = tmp_path / "ipc" / "harness.sock"
    seen: list[tuple[Path, tuple[WorkspaceHint, ...]]] = []

    def request_status(
        ipc_socket: Path,
        hints: list[WorkspaceHint],
    ) -> WorkspaceStatusResult:
        seen.append((ipc_socket, tuple(hints)))
        return WorkspaceStatusResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            head="a" * 40,
            branch="main",
            dirty_path_count=2,
            indexed_file_count=17,
            content_search_document_count=11,
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "status", str(location), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_status", request_status)

    assert harness_main() == 0
    assert len(seen) == 1
    ipc_socket, hints = seen[0]
    assert ipc_socket == socket_path
    assert hints == (
        WorkspaceHint(
            path=location.resolve(),
            source="cli-location",
            match_mode=WorkspaceHintMatchMode.LOCATION,
        ),
    )
    assert capsys.readouterr().out.splitlines() == [
        "Project: project-1",
        "Workspace: workspace-1",
        f"Workspace root: {workspace_root.resolve()}",
        "Visibility: normal",
        f"Git HEAD: {'a' * 40}",
        "Git branch: main",
        "Dirty paths: 2",
        "Indexed files: 17",
        "Content search documents: 11",
        "Schema: 3",
    ]


def test_harness_status_uses_canonical_socket_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    seen_sockets: list[Path] = []
    autostarted: list[RuntimePaths] = []

    def request_status(
        ipc_socket: Path,
        _hints: list[WorkspaceHint],
    ) -> WorkspaceStatusResult:
        seen_sockets.append(ipc_socket)
        return WorkspaceStatusResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            head=None,
            branch="main",
            dirty_path_count=0,
            indexed_file_count=0,
            content_search_document_count=0,
        )

    monkeypatch.setattr(sys, "argv", ["harness", "status", str(workspace_root)])
    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "ensure_canonical_daemon", autostarted.append)
    monkeypatch.setattr(entrypoints, "request_workspace_status", request_status)

    assert harness_main() == 0
    assert autostarted == [defaults]
    assert seen_sockets == [defaults.socket]


def test_harness_status_explicit_socket_does_not_autostart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "manual.sock"

    def unexpected_autostart(_paths: RuntimePaths) -> None:
        raise AssertionError("explicit socket must not autostart the canonical daemon")

    def request_status(_socket: Path, _hints: list[WorkspaceHint]) -> WorkspaceStatusResult:
        return WorkspaceStatusResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            head=None,
            branch="main",
            dirty_path_count=0,
            indexed_file_count=0,
            content_search_document_count=0,
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "status", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "ensure_canonical_daemon", unexpected_autostart)
    monkeypatch.setattr(entrypoints, "request_workspace_status", request_status)

    assert harness_main() == 0


def test_harness_status_defaults_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "harness.sock"
    seen_hints: list[WorkspaceHint] = []

    def request_status(
        _ipc_socket: Path,
        hints: list[WorkspaceHint],
    ) -> WorkspaceStatusResult:
        seen_hints.extend(hints)
        return WorkspaceStatusResult(
            schema_version=3,
            workspace_id="workspace-1",
            project_id="project-1",
            visibility_mode="normal",
            workspace_root=workspace_root.resolve(),
            head=None,
            branch="main",
            dirty_path_count=0,
            indexed_file_count=0,
            content_search_document_count=0,
        )

    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr(sys, "argv", ["harness", "status", "--socket", str(socket_path)])
    monkeypatch.setattr(entrypoints, "request_workspace_status", request_status)

    assert harness_main() == 0
    assert seen_hints == [
        WorkspaceHint(
            path=workspace_root.resolve(),
            source="cli-location",
            match_mode=WorkspaceHintMatchMode.LOCATION,
        )
    ]


def test_harness_status_rejects_non_directory_before_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_file = tmp_path / "file.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")
    socket_path = tmp_path / "harness.sock"

    def unexpected_request(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise AssertionError("IPC should not be called for a non-directory location")

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "status", str(workspace_file), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_status", unexpected_request)

    assert harness_main() == 1
    assert capsys.readouterr().out.strip() == (
        f"Harness status: FAIL (workspace path is not a directory: {workspace_file.resolve()})"
    )


def test_harness_status_reports_ipc_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "harness.sock"

    def failed_request(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise IpcTransportError("local IPC request timed out")

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "status", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_status", failed_request)

    assert harness_main() == 1
    assert capsys.readouterr().out.strip() == "Harness status: FAIL (local IPC request timed out)"


def test_harness_visibility_prints_hygiene_and_cursor_caveat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "harness.sock"

    def request_visibility(ipc_socket: Path, path: Path, mode: str) -> VisibilityResult:
        assert ipc_socket == socket_path
        assert path == workspace_root.resolve()
        assert mode == "hidden"
        return VisibilityResult(
            schema_version=SCHEMA_VERSION,
            project_id="project-1",
            workspace_id="workspace-1",
            workspace_root=workspace_root.resolve(),
            visibility_mode="hidden",
            projected_path_count=2,
            materialized=1,
            removed=0,
            exclude_changed=True,
            scm_write_enforcement="unsupported",
        )

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "visibility", "hidden", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_set_visibility", request_visibility)

    assert harness_main() == 0
    output = capsys.readouterr().out
    assert "Visibility: hidden" in output
    assert "SCM write enforcement: unsupported" in output
    assert "Cursor does not host-block git commit, push, or pull requests." in output


def test_harness_status_bounds_multiline_ipc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    socket_path = tmp_path / "harness.sock"

    def failed_request(*_args: object, **_kwargs: object) -> WorkspaceStatusResult:
        raise IpcTransportError("first\nsecond\r" + "x" * 2000)

    monkeypatch.setattr(
        sys,
        "argv",
        ["harness", "status", str(workspace_root), "--socket", str(socket_path)],
    )
    monkeypatch.setattr(entrypoints, "request_workspace_status", failed_request)

    assert harness_main() == 1
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    line = output.rstrip("\n")
    assert "\n" not in line
    assert "\r" not in line
    assert line.startswith("Harness status: FAIL (first\\nsecond\\r")
    assert line.endswith("...)")
    assert len(line) == len("Harness status: FAIL ()") + 1024


def test_harnessd_main_lists_bounded_serve_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harnessd"])

    assert harnessd_main() == 0
    output = capsys.readouterr().out
    assert "Broader product runtime is under implementation." in output
    assert "serve" in output


def test_harnessd_serve_dispatches_explicit_database_and_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    seen: list[tuple[Path, Path]] = []

    def run_daemon(database: Path, ipc_socket: Path) -> int:
        seen.append((database, ipc_socket))
        return 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "harnessd",
            "serve",
            "--database",
            str(database_path),
            "--socket",
            str(socket_path),
        ],
    )
    monkeypatch.setattr(entrypoints, "_run_daemon", run_daemon)

    assert harnessd_main() == 0
    assert seen == [(database_path, socket_path)]


def test_harnessd_serve_uses_canonical_paths_and_prepares_default_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = RuntimePaths(
        database=tmp_path / "state" / "harness" / "harness.db",
        socket=tmp_path / "run" / "harness.sock",
    )
    prepared: list[Path] = []
    seen: list[tuple[Path, Path]] = []

    def prepare_state(directory: Path) -> None:
        prepared.append(directory)

    def run_daemon(database: Path, ipc_socket: Path) -> int:
        seen.append((database, ipc_socket))
        return 0

    monkeypatch.setattr(sys, "argv", ["harnessd", "serve"])
    monkeypatch.setattr(entrypoints, "default_runtime_paths", lambda: defaults)
    monkeypatch.setattr(entrypoints, "ensure_private_state_directory", prepare_state)
    monkeypatch.setattr(entrypoints, "_run_daemon", run_daemon)

    assert harnessd_main() == 0
    assert prepared == [defaults.database.parent]
    assert seen == [(defaults.database, defaults.socket)]


def test_harness_skills_list_reads_canonical_registry_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = tmp_path / "skills"
    skill = registry / "python-helper"
    skill.mkdir(parents=True)
    registry.chmod(0o700)
    (skill / "SKILL.md").write_text(
        "---\ndescription: Use Python conventions.\n---\n\n# Python helper\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text("id: python-helper\n", encoding="utf-8")
    before = (skill / "SKILL.md").read_bytes()
    monkeypatch.setattr(entrypoints, "default_skill_registry", lambda: registry)
    monkeypatch.setattr(sys, "argv", ["harness", "skills", "list"])

    assert harness_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Skill registry: {registry}",
        "Skills: 1",
        "python-helper\tUse Python conventions.",
    ]
    assert (skill / "SKILL.md").read_bytes() == before


def test_harness_skills_sync_and_validate_builtin_quality_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "skills"
    monkeypatch.setattr(entrypoints, "default_skill_registry", lambda: registry)
    monkeypatch.setattr(sys, "argv", ["harness", "skills", "sync"])
    assert harness_main() == 0
    out = capsys.readouterr().out
    assert f"Built-in skills: {len(BUILTIN_SKILLS)}" in out
    assert f"Installed: {len(BUILTIN_SKILLS)}" in out
    monkeypatch.setattr(sys, "argv", ["harness", "skills", "validate"])
    assert harness_main() == 0
    out = capsys.readouterr().out
    assert "Host profiles: codex, cursor" in out
    assert "Skill validation: OK" in out


def test_harness_skills_sync_reports_retired_and_released_built_ins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "skills"
    retired = BuiltinSkill(
        "retired-example",
        "Use when testing built-in retirement reporting.",
        ("retired-example",),
        "# retired-example\nValid body so the skill would load if left in place.\n",
    )
    released = BuiltinSkill(
        "released-example",
        "Use when testing built-in release reporting.",
        ("released-example",),
        "# released-example\nValid body so the skill would load if left in place.\n",
    )
    monkeypatch.setattr(entrypoints, "default_skill_registry", lambda: registry)
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, retired, released))
    monkeypatch.setattr(sys, "argv", ["harness", "skills", "sync"])
    assert harness_main() == 0
    capsys.readouterr()
    skill_md = registry / released.skill_id / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "User customization.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    assert harness_main() == 0
    out = capsys.readouterr().out
    assert "Retired: 1" in out
    assert "Released: 1" in out


def test_install_and_uninstall_host_cli_choices_are_codex_cursor_all(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harness", "install", "--host", "claude-code"])
    with pytest.raises(SystemExit) as raised:
        harness_main()
    assert raised.value.code == 2
    install_err = capsys.readouterr().err
    assert "invalid choice: 'claude-code'" in install_err
    assert "codex" in install_err
    assert "cursor" in install_err
    assert "all" in install_err

    monkeypatch.setattr(sys, "argv", ["harness", "uninstall", "--host", "claude-code"])
    with pytest.raises(SystemExit) as raised:
        harness_main()
    assert raised.value.code == 2
    uninstall_err = capsys.readouterr().err
    assert "invalid choice: 'claude-code'" in uninstall_err
