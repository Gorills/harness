from __future__ import annotations

import sqlite3

import pytest

import harness.task_baseline as task_baseline
from harness.index import IndexedFileKind, IndexedFileRecord
from harness.task_baseline import TaskBaselineTimeoutError


def test_persisted_index_snapshot_stops_when_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE indexed_files(
            workspace_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            kind TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO indexed_files(
            workspace_id, relative_path, kind, size_bytes, content_sha256
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("workspace", "a.py", "file", 1, "0" * 64),
            ("workspace", "b.py", "file", 1, "1" * 64),
        ],
    )
    observed_times = iter((0.0, 0.0, 6.0))
    monkeypatch.setattr(task_baseline, "monotonic", lambda: next(observed_times))
    try:
        with pytest.raises(TaskBaselineTimeoutError, match="deadline exceeded"):
            task_baseline._persisted_index_snapshot(
                connection,
                "workspace",
                deadline=5.0,
            )
    finally:
        connection.close()


def test_index_snapshot_digest_stops_when_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexed_files = (
        IndexedFileRecord(
            workspace_id="workspace",
            relative_path="a.py",
            kind=IndexedFileKind.FILE,
            size_bytes=1,
            content_sha256="0" * 64,
        ),
        IndexedFileRecord(
            workspace_id="workspace",
            relative_path="b.py",
            kind=IndexedFileKind.FILE,
            size_bytes=1,
            content_sha256="1" * 64,
        ),
    )
    observed_times = iter((0.0, 0.0, 6.0))
    monkeypatch.setattr(task_baseline, "monotonic", lambda: next(observed_times))

    with pytest.raises(TaskBaselineTimeoutError, match="deadline exceeded"):
        task_baseline._index_snapshot_sha256(indexed_files, deadline=5.0)
