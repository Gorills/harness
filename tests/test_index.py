import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

from harness.index import (
    IndexedFileKind,
    IndexingError,
    WorkspaceIndexMismatchError,
    list_indexed_files,
    scan_workspace,
)
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
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
    return root


def _registered(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    root = _repo(tmp_path)
    db = tmp_path / "harness.db"
    initialize_database(db)
    connection = connect_database(db)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    return root, connection, workspace.workspace_id


def test_scan_indexes_tracked_and_untracked_and_respects_exclusions(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        (root / "new.txt").write_text("new\n", encoding="utf-8")
        (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (root / "tracked.key").write_text("secret\n", encoding="utf-8")
        _git(root, "add", "-f", "tracked.key")
        (root / ".harnessignore").write_text("private.txt\ntracked.txt\n", encoding="utf-8")
        (root / "private.txt").write_text("private\n", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "package.js").write_text("generated\n", encoding="utf-8")

        result = scan_workspace(connection, workspace_id)
        paths = [record.relative_path for record in list_indexed_files(connection, workspace_id)]

        assert result.file_count == len(paths)
        assert "new.txt" in paths
        assert ".gitignore" in paths
        assert ".harnessignore" in paths
        assert "tracked.txt" not in paths
        assert "private.txt" not in paths
        assert "ignored.txt" not in paths
        assert ".env" not in paths
        assert "tracked.key" not in paths
        assert "node_modules/package.js" not in paths
    finally:
        connection.close()


def test_scan_reconciles_add_modify_delete_and_is_idempotent(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        first = scan_workspace(connection, workspace_id)
        assert first.added == 1
        assert first.updated == 0
        assert first.removed == 0

        second = scan_workspace(connection, workspace_id)
        assert (second.added, second.updated, second.removed) == (0, 0, 0)

        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (root / "added.txt").write_text("added\n", encoding="utf-8")
        third = scan_workspace(connection, workspace_id)
        assert (third.added, third.updated, third.removed) == (1, 1, 0)

        (root / "tracked.txt").unlink()
        fourth = scan_workspace(connection, workspace_id)
        assert fourth.removed == 1
        paths = [record.relative_path for record in list_indexed_files(connection, workspace_id)]
        assert paths == ["added.txt"]
    finally:
        connection.close()


def test_scan_hashes_symlink_target_without_following_outside_workspace(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("do-not-read-this-content\n", encoding="utf-8")
        link = root / "outside-link"
        link.symlink_to(outside)

        scan_workspace(connection, workspace_id)
        records = {
            record.relative_path: record for record in list_indexed_files(connection, workspace_id)
        }
        record = records["outside-link"]
        assert record.kind is IndexedFileKind.SYMLINK
        expected = hashlib.sha256(b"symlink\0" + os.fsencode(str(outside))).hexdigest()
        assert record.content_sha256 == expected
        assert record.content_sha256 != hashlib.sha256(outside.read_bytes()).hexdigest()
    finally:
        connection.close()


def test_scan_does_not_read_external_target_after_regular_file_is_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    tracked = root / "tracked.txt"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do-not-read-this-content\n", encoding="utf-8")
    original_open = Path.open
    swapped = False
    hash_updates: list[bytes] = []

    class TrackingHash:
        def update(self, data: bytes) -> None:
            hash_updates.append(data)

        def hexdigest(self) -> str:
            return "0" * 64

    def tracking_sha256(data: bytes = b"") -> TrackingHash:
        digest = TrackingHash()
        if data:
            digest.update(data)
        return digest

    def racing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if self == tracked and not swapped:
            swapped = True
            self.unlink()
            self.symlink_to(outside)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    monkeypatch.setattr(hashlib, "sha256", tracking_sha256)
    try:
        with pytest.raises(IndexingError, match="changed while scanning"):
            scan_workspace(connection, workspace_id)
        assert swapped is True
        assert hash_updates == []
    finally:
        connection.close()


def test_scan_does_not_read_external_harnessignore_after_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    harnessignore = root / ".harnessignore"
    harnessignore.write_text("tracked.txt\n", encoding="utf-8")
    outside = tmp_path / "outside-ignore.txt"
    outside.write_text("external-secret-pattern\n", encoding="utf-8")
    original_open = Path.open
    swapped = False
    reads: list[bool] = []

    class ReadTrackingStream:
        def __init__(self, stream: BinaryIO) -> None:
            self._stream = stream

        def __enter__(self) -> "ReadTrackingStream":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            self._stream.close()

        def fileno(self) -> int:
            return self._stream.fileno()

        def read(self, *args: Any, **kwargs: Any) -> bytes:
            reads.append(True)
            return self._stream.read(*args, **kwargs)

    def racing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if self == harnessignore and not swapped:
            swapped = True
            self.unlink()
            self.symlink_to(outside)
            stream = cast(BinaryIO, original_open(self, *args, **kwargs))
            return ReadTrackingStream(stream)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    try:
        with pytest.raises(IndexingError, match="changed while scanning"):
            scan_workspace(connection, workspace_id)
        assert swapped is True
        assert reads == []
    finally:
        connection.close()


def test_scan_fails_closed_if_harnessignore_disappears_after_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    harnessignore = root / ".harnessignore"
    harnessignore.write_text("tracked.txt\n", encoding="utf-8")
    original_open = Path.open
    removed = False

    def racing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal removed
        if self == harnessignore and not removed:
            removed = True
            self.unlink()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    try:
        with pytest.raises(IndexingError, match=r"changed while scanning: \.harnessignore"):
            scan_workspace(connection, workspace_id)
        assert removed is True
        assert list_indexed_files(connection, workspace_id) == ()
    finally:
        connection.close()


def test_scan_ignores_inherited_git_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, connection, workspace_id = _registered(tmp_path / "target")
    decoy = _repo(tmp_path / "decoy")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    try:
        result = scan_workspace(connection, workspace_id)
        assert result.file_count == 1
        assert [
            record.relative_path for record in list_indexed_files(connection, workspace_id)
        ] == ["tracked.txt"]
    finally:
        connection.close()


def test_scan_refuses_symlinked_parent_that_escapes_workspace(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered(tmp_path)
    try:
        nested = root / "nested"
        nested.mkdir()
        (nested / "inside.txt").write_text("inside\n", encoding="utf-8")
        _git(root, "add", "nested/inside.txt")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "inside.txt").write_text("outside-secret\n", encoding="utf-8")
        for child in nested.iterdir():
            child.unlink()
        nested.rmdir()
        nested.symlink_to(outside, target_is_directory=True)

        with pytest.raises(WorkspaceIndexMismatchError, match="escapes"):
            scan_workspace(connection, workspace_id)
    finally:
        connection.close()
