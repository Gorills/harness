from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from threading import Lock
from time import monotonic

import pytest

from harness.daemon import read_project_search
from harness.index import scan_workspace
from harness.ipc import ProjectSearchResult
from harness.registry import create_project, get_workspace, register_workspace
from harness.retrieval import (
    PROJECT_SEARCH_MAX_BYTES,
    ProjectExactSearchCoverage,
    ProjectSearchScope,
    exact_coverage_response_reserve,
    project_exact_search_coverage_payload,
    project_search_hit_payload,
    search_exact_source_coverage,
    search_project,
)
from harness.search_currentness import (
    ensure_workspace_search_index_current,
    workspace_search_state_is_unchanged,
)
from harness.storage import connect_database, initialize_database
from harness.workspace_resolution import WorkspaceHint


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _commit(cwd: Path, message: str) -> None:
    _git(cwd, "add", "-A")
    _git(
        cwd,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        message,
    )


def _registered(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str, Lock]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "service.py").write_text(
        "def oldSymbol():\n    return 'old'\n",
        encoding="utf-8",
    )
    _git(root, "init", "-b", "main")
    _commit(root, "initial")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id, Lock()


def _search(
    connection: sqlite3.Connection,
    root: Path,
    scan_lock: Lock,
    query: str,
) -> ProjectSearchResult:
    return read_project_search(
        connection,
        (WorkspaceHint(root, "explicit-root"),),
        query,
        5,
        ProjectSearchScope.CODE,
        scan_lock,
    )


def test_project_search_reads_immediate_dirty_edits_and_reverts(tmp_path: Path) -> None:
    root, connection, workspace_id, scan_lock = _registered(tmp_path)
    try:
        initial = _search(connection, root, scan_lock, "oldSymbol")
        assert initial.workspace_state == "current"
        assert initial.results[0].path == "src/service.py"
        assert initial.exact_coverage is not None
        assert initial.exact_coverage.complete is True
        assert initial.exact_coverage.matched_occurrences == 1

        path = root / "src" / "service.py"
        path.write_text("def newSymbol():\n    return 'new'\n", encoding="utf-8")

        dirty = _search(connection, root, scan_lock, "newSymbol")
        assert dirty.workspace_state == "current"
        assert dirty.results[0].path == "src/service.py"
        assert dirty.exact_coverage is not None
        assert dirty.exact_coverage.complete is True
        assert dirty.exact_coverage.matched_occurrences == 1
        indexed_sha = connection.execute(
            "SELECT content_sha256 FROM indexed_files WHERE workspace_id = ? AND relative_path = ?",
            (workspace_id, "src/service.py"),
        ).fetchone()
        assert indexed_sha is not None

        _git(root, "restore", "src/service.py")
        reverted = _search(connection, root, scan_lock, "newSymbol")
        assert reverted.results == ()
        assert reverted.exact_coverage is not None
        assert reverted.exact_coverage.complete is True
        assert reverted.exact_coverage.matched_occurrences == 0
        restored = _search(connection, root, scan_lock, "oldSymbol")
        assert restored.results[0].path == "src/service.py"
    finally:
        connection.close()


def test_search_snapshot_detects_aba_index_change_during_retrieval(tmp_path: Path) -> None:
    root, connection, workspace_id, scan_lock = _registered(tmp_path)
    try:
        workspace = get_workspace(connection, workspace_id)
        currentness = ensure_workspace_search_index_current(
            connection,
            workspace,
            scan_lock,
            deadline=monotonic() + 5,
        )
        path = root / "src" / "service.py"
        path.write_text("def intermediateSymbol():\n    return 'intermediate'\n", encoding="utf-8")
        scan_workspace(connection, workspace_id)
        _git(root, "restore", "src/service.py")

        assert (
            workspace_search_state_is_unchanged(
                connection,
                workspace,
                currentness,
                deadline=monotonic() + 5,
            )
            is False
        )
    finally:
        connection.close()


def test_project_search_retries_aba_index_change_during_retrieval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, connection, workspace_id, scan_lock = _registered(tmp_path)
    try:
        _search(connection, root, scan_lock, "oldSymbol")
        original = search_exact_source_coverage
        calls = 0

        def racing_exact_coverage(
            connection_arg: sqlite3.Connection,
            workspace_id_arg: str,
            query_arg: str,
            *,
            scope: ProjectSearchScope,
        ) -> ProjectExactSearchCoverage | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                path = root / "src" / "service.py"
                path.write_text(
                    "def intermediateSymbol():\n    return 'intermediate'\n",
                    encoding="utf-8",
                )
                writer = connect_database(tmp_path / "harness.db")
                try:
                    scan_workspace(writer, workspace_id)
                finally:
                    writer.close()
                _git(root, "restore", "src/service.py")
            return original(
                connection_arg,
                workspace_id_arg,
                query_arg,
                scope=scope,
            )

        monkeypatch.setattr("harness.daemon.search_exact_source_coverage", racing_exact_coverage)
        result = _search(connection, root, scan_lock, "oldSymbol")

        assert calls == 2
        assert result.workspace_state == "current"
        assert result.results[0].path == "src/service.py"
        assert result.exact_coverage is not None
        assert result.exact_coverage.matched_occurrences == 1
    finally:
        connection.close()


def test_project_search_detects_intervening_watcher_scan_after_exact_revert(
    tmp_path: Path,
) -> None:
    root, connection, workspace_id, scan_lock = _registered(tmp_path)
    try:
        initial = _search(connection, root, scan_lock, "oldSymbol")
        assert initial.results[0].path == "src/service.py"
        persisted_revision = connection.execute(
            "SELECT index_revision FROM workspace_search_index_state WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        assert persisted_revision is not None

        path = root / "src" / "service.py"
        path.write_text("def transientSymbol():\n    return 'transient'\n", encoding="utf-8")
        scan_workspace(connection, workspace_id)
        watcher_revision = connection.execute(
            "SELECT index_revision FROM workspace_index_reconcile WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        assert watcher_revision is not None
        assert watcher_revision[0] > persisted_revision[0]

        _git(root, "restore", "src/service.py")
        restored = _search(connection, root, scan_lock, "oldSymbol")
        assert restored.results[0].path == "src/service.py"
        assert restored.exact_coverage is not None
        assert restored.exact_coverage.matched_occurrences == 1
        assert _search(connection, root, scan_lock, "transientSymbol").results == ()
        assert connection.execute(
            "SELECT last_reconcile_kind FROM workspace_index_reconcile WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone() == ("full",)
    finally:
        connection.close()


def test_project_search_reconciles_clean_branch_switch_by_changed_paths(tmp_path: Path) -> None:
    root, connection, _workspace_id, scan_lock = _registered(tmp_path)
    try:
        _search(connection, root, scan_lock, "oldSymbol")
        _git(root, "switch", "-c", "feature")
        (root / "src" / "service.py").write_text(
            "def featureSymbol():\n    return 'feature'\n",
            encoding="utf-8",
        )
        _commit(root, "feature")

        feature = _search(connection, root, scan_lock, "featureSymbol")
        assert feature.results[0].path == "src/service.py"
        assert feature.exact_coverage is not None
        assert feature.exact_coverage.matched_occurrences == 1
        assert connection.execute(
            "SELECT last_reconcile_kind FROM workspace_index_reconcile"
        ).fetchone() == ("incremental",)

        _git(root, "switch", "main")
        main = _search(connection, root, scan_lock, "featureSymbol")
        assert main.results == ()
        assert main.exact_coverage is not None
        assert main.exact_coverage.complete is True
        assert main.exact_coverage.matched_occurrences == 0
        assert _search(connection, root, scan_lock, "oldSymbol").results[0].path == "src/service.py"
    finally:
        connection.close()


def test_project_search_reconciles_candidate_set_after_gitignore_change(tmp_path: Path) -> None:
    root, connection, _workspace_id, scan_lock = _registered(tmp_path)
    try:
        (root / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
        (root / "hidden.py").write_text("def hiddenSymbol():\n    pass\n", encoding="utf-8")
        _commit(root, "ignore hidden")
        _search(connection, root, scan_lock, "oldSymbol")

        (root / ".gitignore").write_text("", encoding="utf-8")
        visible = _search(connection, root, scan_lock, "hiddenSymbol")
        assert visible.results[0].path == "hidden.py"
        assert visible.exact_coverage is not None
        assert visible.exact_coverage.matched_occurrences == 1

        (root / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
        hidden = _search(connection, root, scan_lock, "hiddenSymbol")
        assert hidden.results == ()
        assert hidden.exact_coverage is not None
        assert hidden.exact_coverage.matched_occurrences == 0
    finally:
        connection.close()


def test_exact_coverage_counts_all_text_occurrences_and_ignores_binary(tmp_path: Path) -> None:
    root, connection, workspace_id, _scan_lock = _registered(tmp_path)
    try:
        (root / "src" / "one.py").write_text(
            "needle = 'needle'\n",
            encoding="utf-8",
        )
        (root / "src" / "two.py").write_text("print('needle')\n", encoding="utf-8")
        (root / "src" / "asset.bin").write_bytes(b"needle\x00needle")
        scan_workspace(connection, workspace_id)

        coverage = search_exact_source_coverage(
            connection,
            workspace_id,
            "needle",
            scope=ProjectSearchScope.CODE,
        )

        assert coverage is not None
        assert coverage.complete is True
        assert coverage.matched_files == 2
        assert coverage.matched_occurrences == 3
        assert coverage.matched_lines == 2
        assert coverage.non_text_files == 1
        assert coverage.unavailable_files == 0
        assert [(item.path, item.line) for item in coverage.locations] == [
            ("src/one.py", 1),
            ("src/one.py", 1),
            ("src/two.py", 1),
        ]
    finally:
        connection.close()


def test_exact_coverage_preserves_totals_when_locations_are_truncated(tmp_path: Path) -> None:
    root, connection, workspace_id, _scan_lock = _registered(tmp_path)
    try:
        (root / "src" / "many.py").write_text(
            "\n".join(f"needle = {index}" for index in range(40)) + "\n",
            encoding="utf-8",
        )
        scan_workspace(connection, workspace_id)

        coverage = search_exact_source_coverage(
            connection,
            workspace_id,
            "needle",
            scope=ProjectSearchScope.CODE,
        )

        assert coverage is not None
        assert coverage.complete is True
        assert coverage.matched_occurrences == 40
        assert coverage.matched_lines == 40
        assert coverage.locations_truncated is True
        assert len(coverage.locations) == 24
    finally:
        connection.close()


def test_exact_coverage_and_rich_hits_share_the_project_search_budget(tmp_path: Path) -> None:
    root, connection, workspace_id, _scan_lock = _registered(tmp_path)
    try:
        for file_index in range(5):
            (root / "src" / f"needle_{file_index}.py").write_text(
                "\n".join(
                    f"def needleFunction{line_index}(): return 'needleFunction'"
                    for line_index in range(20)
                )
                + "\n",
                encoding="utf-8",
            )
        scan_workspace(connection, workspace_id)
        coverage = search_exact_source_coverage(
            connection,
            workspace_id,
            "needleFunction",
            scope=ProjectSearchScope.CODE,
        )
        assert coverage is not None
        hits = search_project(
            connection,
            workspace_id,
            "needleFunction",
            scope=ProjectSearchScope.CODE,
            limit=5,
            response_reserve_bytes=exact_coverage_response_reserve(coverage),
        )
        payload = {
            "query": "needleFunction",
            "scope": "code",
            "workspace_state": "current",
            "exact_coverage": project_exact_search_coverage_payload(coverage),
            "results": [project_search_hit_payload(hit) for hit in hits],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= PROJECT_SEARCH_MAX_BYTES
        assert coverage.matched_occurrences == 200
        assert coverage.locations_truncated is True
    finally:
        connection.close()


def test_schema_v17_adds_search_currentness_without_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harness.storage as storage

    database = tmp_path / "migration.db"
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 16)
    storage.initialize_database(database)
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
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        assert "workspace_search_index_state" not in tables
    finally:
        connection.close()

    monkeypatch.setattr(storage, "SCHEMA_VERSION", 17)
    status = storage.initialize_database(database)
    assert status.schema_version == 17
    connection = storage.connect_database(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_search_index_state"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_search_index_dirty_paths"
        ).fetchone() == (0,)
    finally:
        connection.close()
