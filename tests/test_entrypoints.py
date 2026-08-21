import sys
from pathlib import Path

import pytest

import harness.entrypoints as entrypoints
from harness.doctor import DoctorReport
from harness.entrypoints import harness_main, harnessd_main
from harness.ipc import IpcTransportError, WorkspaceStatusResult
from harness.storage import DatabaseStatus
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


def test_harness_doctor_reports_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def successful_checks() -> DoctorReport:
        return DoctorReport(sqlite_version="3.50.4", fts5_available=True)

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", successful_checks)

    assert harness_main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: OK (version 3.50.4)",
        "FTS5: OK",
        "Doctor scope: SQLite runtime only; pass --database PATH to inspect an initialized "
        "Harness database.",
    ]


def test_harness_doctor_fails_when_fts5_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing_fts5() -> DoctorReport:
        return DoctorReport(sqlite_version="3.50.4", fts5_available=False)

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
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

    monkeypatch.setattr(sys, "argv", ["harness", "doctor"])
    monkeypatch.setattr(entrypoints, "run_doctor_checks", failed_checks)

    assert harness_main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "SQLite runtime: FAIL (probe failed)",
        "FTS5: UNKNOWN",
        "Doctor scope: SQLite runtime only; pass --database PATH to inspect an initialized "
        "Harness database.",
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
        "Doctor scope: SQLite runtime + selected initialized database; other checks are not "
        "implemented yet.",
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
        "Schema: 3",
    ]


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
    assert capsys.readouterr().out.strip() == (
        "Harness status: FAIL (local IPC request timed out)"
    )


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
