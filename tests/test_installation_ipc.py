from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from harness.daemon import _serve_skill_cleanup, _serve_workspace_skills, serve_daemon
from harness.index import IndexingError, ScanDeadlineExceededError, scan_workspace
from harness.ipc import (
    IpcRemoteError,
    WorkspaceSkillsResult,
    request_shutdown,
    request_skill_cleanup,
    request_workspace_init,
    request_workspace_skills_reconcile,
)
from harness.registry import register_workspace_for_init
from harness.skill_runtime import SkillCleanupResult as RuntimeSkillCleanupResult
from harness.skill_runtime import SkillRuntimeError
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

pytestmark = pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX daemon integration")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repo(root: Path) -> None:
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "commit",
        "-m",
        "init",
    )


def _skill_registry(home: Path) -> None:
    skill = home / ".harness" / "skills" / "python-helper"
    skill.mkdir(parents=True)
    (home / ".harness").chmod(0o700)
    (home / ".harness" / "skills").chmod(0o700)
    (skill / "SKILL.md").write_text(
        "---\nname: python-helper\ndescription: Use Python conventions.\n---\n\n"
        "# Python helper\n\nUse Python conventions.\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text(
        "id: python-helper\napplies:\n  languages:\n    - python\n",
        encoding="utf-8",
    )


def _add_dependency_skill(home: Path) -> None:
    skill = home / ".harness" / "skills" / "react-helper"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: react-helper\ndescription: Use React conventions.\n---\n\n"
        "# React helper\n\nUse React conventions.\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text(
        "id: react-helper\napplies:\n  dependencies:\n    - react\n",
        encoding="utf-8",
    )


def _start_daemon(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop)
    deadline = time.monotonic() + 3
    while not socket_path.exists():
        if future.done():
            future.result()
        if time.monotonic() >= deadline:
            raise AssertionError("daemon did not start")
        time.sleep(0.01)
    return stop, executor, future


def test_daemon_reconciles_and_cleans_project_skills_then_shuts_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _skill_registry(home)
    root = tmp_path / "repo"
    _repo(root)
    database = tmp_path / "state" / "harness.db"
    socket_path = tmp_path / "run" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    try:
        scan = request_workspace_init(socket_path, root)
        hints = (
            WorkspaceHint(
                path=scan.workspace_root,
                source="test-root",
                match_mode=WorkspaceHintMatchMode.ROOT,
            ),
        )
        skills = request_workspace_skills_reconcile(socket_path, hints, ("cursor",))
        assert skills.workspace_id == scan.workspace_id
        assert skills.selected_skill_ids == ("python-helper",)
        assert skills.materialized == 1
        assert (root / ".agents" / "skills" / "python-helper" / "SKILL.md").exists()
        exclude = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        exclude_path = Path(exclude)
        if not exclude_path.is_absolute():
            exclude_path = root / exclude_path
        assert ".agents/skills/python-helper/" in exclude_path.read_text(encoding="utf-8")

        cleanup = request_skill_cleanup(socket_path, ("cursor",))
        assert cleanup.workspace_count == 1
        assert cleanup.cleaned_workspace_count == 1
        assert cleanup.skipped_workspace_count == 0
        assert cleanup.removed == 1
        assert not (root / ".agents" / "skills" / "python-helper").exists()
        assert ".agents/skills/python-helper/" not in exclude_path.read_text(encoding="utf-8")

        assert request_shutdown(socket_path).accepted is True
        future.result(timeout=3)
        assert not socket_path.exists()
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_daemon_refreshes_stale_manifest_index_before_skill_reconciliation_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _skill_registry(home)
    _add_dependency_skill(home)
    root = tmp_path / "repo"
    _repo(root)
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    database = tmp_path / "state" / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        registration = register_workspace_for_init(connection, path=root)
        scan_workspace(connection, registration.workspace.workspace_id)
    finally:
        connection.close()
    hints = (
        WorkspaceHint(
            path=root,
            source="test-root",
            match_mode=WorkspaceHintMatchMode.ROOT,
        ),
    )

    captured: list[WorkspaceSkillsResult] = []

    def capture_result(
        _client: socket.socket,
        _request_id: str,
        result: WorkspaceSkillsResult,
    ) -> None:
        captured.append(result)

    monkeypatch.setattr("harness.daemon.send_workspace_skills_response", capture_result)

    def reconcile() -> WorkspaceSkillsResult:
        connection = connect_database(database)
        server_peer, client_peer = socket.socketpair()
        try:
            _serve_workspace_skills(
                server_peer,
                connection,
                "skills-request",
                hints,
                ("cursor",),
                Lock(),
            )
            assert len(captured) == 1
            return captured.pop()
        finally:
            server_peer.close()
            client_peer.close()
            connection.close()

    (root / "package.json").write_text(
        json.dumps({"dependencies": {"react": "1"}}),
        encoding="utf-8",
    )
    added = reconcile()
    assert set(added.selected_skill_ids) == {"python-helper", "react-helper"}
    assert (root / ".agents" / "skills" / "react-helper" / "SKILL.md").exists()

    (root / "package.json").unlink()
    removed = reconcile()
    assert removed.selected_skill_ids == ("python-helper",)
    assert removed.removed == 1
    assert not (root / ".agents" / "skills" / "react-helper").exists()

    (root / "package.json").write_text(
        json.dumps({"dependencies": {"react": "1"}}),
        encoding="utf-8",
    )
    readded = reconcile()
    assert set(readded.selected_skill_ids) == {"python-helper", "react-helper"}
    assert (root / ".agents" / "skills" / "react-helper" / "SKILL.md").exists()


def test_workspace_skill_reconciliation_uses_lifecycle_transport_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[float] = []

    class RequestCaptured(RuntimeError):
        pass

    def capture_request(*_args: object, timeout: float, **_kwargs: object) -> None:
        captured.append(timeout)
        raise RequestCaptured

    monkeypatch.setattr("harness.ipc._request_response", capture_request)

    with pytest.raises(RequestCaptured):
        request_workspace_skills_reconcile(
            tmp_path / "harness.sock",
            (
                WorkspaceHint(
                    path=tmp_path,
                    source="test-root",
                    match_mode=WorkspaceHintMatchMode.ROOT,
                ),
            ),
            ("cursor",),
        )

    assert captured == [220.0]


def test_daemon_reports_actionable_skill_projection_collision_without_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _skill_registry(home)
    root = tmp_path / "repo-with-private-name"
    _repo(root)
    user_skill = root / ".agents" / "skills" / "python-helper"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("SENSITIVE-SKILL-CONTENT\n", encoding="utf-8")
    database = tmp_path / "state" / "harness.db"
    socket_path = tmp_path / "run" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    try:
        scan = request_workspace_init(socket_path, root)
        with pytest.raises(IpcRemoteError) as caught:
            request_workspace_skills_reconcile(
                socket_path,
                (
                    WorkspaceHint(
                        path=scan.workspace_root,
                        source="test-root",
                        match_mode=WorkspaceHintMatchMode.ROOT,
                    ),
                ),
                ("cursor",),
            )

        assert caught.value.code == "skill_integration_error"
        assert caught.value.message == (
            "Workspace skill projection conflicts with tracked or user-owned content; inspect "
            ".agents/skills and Git tracking"
        )
        assert str(root) not in caught.value.message
        assert "SENSITIVE-SKILL-CONTENT" not in caught.value.message
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_daemon_reports_invalid_skill_registry_without_manifest_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _skill_registry(home)
    manifest = home / ".harness" / "skills" / "python-helper" / "harness.yaml"
    manifest.write_text("private manifest text that must not escape\n", encoding="utf-8")
    root = tmp_path / "repo"
    _repo(root)
    database = tmp_path / "state" / "harness.db"
    socket_path = tmp_path / "run" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    try:
        scan = request_workspace_init(socket_path, root)
        with pytest.raises(IpcRemoteError) as caught:
            request_workspace_skills_reconcile(
                socket_path,
                (
                    WorkspaceHint(
                        path=scan.workspace_root,
                        source="test-root",
                        match_mode=WorkspaceHintMatchMode.ROOT,
                    ),
                ),
                ("cursor",),
            )

        assert caught.value.code == "skill_integration_error"
        assert caught.value.message == (
            "Harness skill registry is invalid or unsafe; rerun harness install and inspect the "
            "skill registry"
        )
        assert "private manifest text" not in caught.value.message
        assert str(manifest) not in caught.value.message
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_daemon_redacts_unclassified_skill_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = sqlite3.connect(":memory:", check_same_thread=False)
    server_peer, client_peer = socket.socketpair()
    try:
        monkeypatch.setattr(
            "harness.daemon._resolve_task_workspace",
            lambda *_args: SimpleNamespace(workspace_id="workspace-id"),
        )
        monkeypatch.setattr("harness.daemon.scan_workspace", lambda *_args, **_kwargs: None)

        def fail_reconcile(*_args: object) -> None:
            raise SkillRuntimeError("secret-token\n/private/workspace")

        monkeypatch.setattr("harness.daemon.reconcile_workspace_skills", fail_reconcile)
        _serve_workspace_skills(
            server_peer,
            database,
            "skills-request",
            (),
            ("cursor",),
            Lock(),
        )
        payload = client_peer.recv(4096)
        response = json.loads(payload)

        assert len(payload) < 512
        assert response["error"] == {
            "code": "skill_integration_error",
            "message": (
                "Workspace skill integration could not be reconciled safely; run harness doctor"
            ),
        }
        assert b"secret-token" not in payload
        assert b"private/workspace" not in payload
    finally:
        server_peer.close()
        client_peer.close()
        database.close()


@pytest.mark.parametrize(
    ("scan_error", "expected_message"),
    (
        (
            IndexingError("private index failure"),
            "Workspace index could not be refreshed before skill reconciliation; run harness scan",
        ),
        (
            ScanDeadlineExceededError("private scan deadline"),
            "Workspace index refresh exceeded the daemon deadline; exclude generated dependency "
            "or cache directories and retry the install",
        ),
    ),
)
def test_daemon_reports_index_refresh_failure_as_nonretryable_skill_error(
    monkeypatch: pytest.MonkeyPatch,
    scan_error: IndexingError,
    expected_message: str,
) -> None:
    database = sqlite3.connect(":memory:", check_same_thread=False)
    server_peer, client_peer = socket.socketpair()
    scan_lock = Lock()
    errors: list[tuple[str | None, str, str]] = []
    deadlines: list[float | None] = []
    try:
        monkeypatch.setattr("harness.daemon.monotonic", lambda: 100.0)
        monkeypatch.setattr("harness.daemon._SKILL_INDEX_REFRESH_DEADLINE_SECONDS", 180.0)
        monkeypatch.setattr(
            "harness.daemon._resolve_task_workspace",
            lambda *_args: SimpleNamespace(workspace_id="workspace-id"),
        )

        def fail_scan(*_args: object, deadline: float | None = None, **_kwargs: object) -> None:
            deadlines.append(deadline)
            raise scan_error

        def capture_error(
            _client: socket.socket,
            *,
            code: str,
            message: str,
            request_id: str | None = None,
        ) -> None:
            errors.append((request_id, code, message))

        monkeypatch.setattr("harness.daemon.scan_workspace", fail_scan)
        monkeypatch.setattr(
            "harness.daemon.reconcile_workspace_skills",
            lambda *_args: pytest.fail("skill reconciliation must not run after scan failure"),
        )
        monkeypatch.setattr("harness.daemon._try_send_error", capture_error)
        _serve_workspace_skills(
            server_peer,
            database,
            "skills-request",
            (),
            ("cursor",),
            scan_lock,
        )

        assert errors == [
            (
                "skills-request",
                "skill_integration_error",
                expected_message,
            )
        ]
        assert "private" not in expected_message
        assert deadlines == [280.0]
        assert scan_lock.locked() is False
    finally:
        server_peer.close()
        client_peer.close()
        database.close()


def test_daemon_keeps_scan_lock_timeout_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    database = sqlite3.connect(":memory:", check_same_thread=False)
    server_peer, client_peer = socket.socketpair()
    errors: list[tuple[str | None, str, str]] = []
    try:

        def capture_error(
            _client: socket.socket,
            *,
            code: str,
            message: str,
            request_id: str | None = None,
        ) -> None:
            errors.append((request_id, code, message))

        monkeypatch.setattr("harness.daemon._SCAN_DEADLINE_SECONDS", 0.0)
        monkeypatch.setattr("harness.daemon._try_send_error", capture_error)
        _serve_workspace_skills(
            server_peer,
            database,
            "skills-request",
            (),
            ("cursor",),
            Lock(),
        )

        assert errors == [
            (
                "skills-request",
                "skill_integration_timeout",
                "Workspace skill reconciliation exceeded the daemon execution deadline",
            )
        ]
    finally:
        server_peer.close()
        client_peer.close()
        database.close()


def test_global_skill_cleanup_skips_replaced_workspace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _skill_registry(home)
    root = tmp_path / "repo"
    _repo(root)
    database = tmp_path / "state" / "harness.db"
    socket_path = tmp_path / "run" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    try:
        scan = request_workspace_init(socket_path, root)
        request_workspace_skills_reconcile(
            socket_path,
            (
                WorkspaceHint(
                    path=scan.workspace_root,
                    source="test-root",
                    match_mode=WorkspaceHintMatchMode.ROOT,
                ),
            ),
            ("cursor",),
        )
        original = tmp_path / "original-repo"
        root.rename(original)
        _repo(root)
        sentinel = root / ".agents" / "skills" / "python-helper" / "SKILL.md"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("user-owned replacement\n", encoding="utf-8")

        cleanup = request_skill_cleanup(socket_path, ("cursor",))
        assert cleanup.workspace_count == 1
        assert cleanup.cleaned_workspace_count == 1
        assert cleanup.skipped_workspace_count == 0
        assert cleanup.removed == 0
        assert sentinel.read_text(encoding="utf-8") == "user-owned replacement\n"
        assert (original / ".agents" / "skills" / "python-helper" / "SKILL.md").exists()
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_global_skill_cleanup_skips_unsafe_projection_parent_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "repo"
    _repo(root)
    database = tmp_path / "state" / "harness.db"
    socket_path = tmp_path / "run" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    try:
        request_workspace_init(socket_path, root)
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        (root / ".agents").mkdir()
        (root / ".agents" / "skills").symlink_to(outside, target_is_directory=True)

        cleanup = request_skill_cleanup(socket_path, ("cursor",))
        assert cleanup.workspace_count == 1
        assert cleanup.cleaned_workspace_count == 0
        assert cleanup.skipped_workspace_count == 1
        assert cleanup.removed == 0
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        assert (root / ".agents" / "skills").is_symlink()
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()


def test_skill_cleanup_waits_for_authoritative_scan_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_lock = Lock()
    scan_lock.acquire()
    cleanup_called = Event()

    def fake_cleanup(
        _connection: sqlite3.Connection, _profiles: tuple[str, ...]
    ) -> RuntimeSkillCleanupResult:
        cleanup_called.set()
        return RuntimeSkillCleanupResult(
            workspace_count=0,
            cleaned_workspace_count=0,
            skipped_workspace_count=0,
            removed=0,
            exclude_changed_count=0,
        )

    monkeypatch.setattr("harness.daemon.cleanup_projected_skills", fake_cleanup)
    database = sqlite3.connect(":memory:", check_same_thread=False)
    server_peer, client_peer = socket.socketpair()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            _serve_skill_cleanup,
            server_peer,
            database,
            "cleanup-request",
            ("cursor",),
            scan_lock,
        )
        assert cleanup_called.wait(0.05) is False
        assert future.done() is False

        scan_lock.release()
        assert cleanup_called.wait(1.0) is True
        future.result(timeout=1.0)
        response = json.loads(client_peer.recv(4096))
        assert response["ok"] is True
        assert response["result"]["workspace_count"] == 0
    finally:
        if scan_lock.locked():
            scan_lock.release()
        server_peer.close()
        client_peer.close()
        database.close()
        executor.shutdown(wait=True)
