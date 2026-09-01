from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

from harness.daemon import _serve_skill_cleanup, serve_daemon
from harness.ipc import (
    request_shutdown,
    request_skill_cleanup,
    request_workspace_scan,
    request_workspace_skills_reconcile,
)
from harness.skill_runtime import SkillCleanupResult as RuntimeSkillCleanupResult
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
        scan = request_workspace_scan(socket_path, root)
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
        scan = request_workspace_scan(socket_path, root)
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
        request_workspace_scan(socket_path, root)
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
