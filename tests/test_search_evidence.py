from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import harness.retrieval as retrieval_module
from harness.index import SearchEvidenceReadStatus, read_current_search_text, scan_workspace
from harness.registry import create_project, get_workspace, register_workspace
from harness.retrieval import (
    EVIDENCE_REASON_CHANGED_SINCE_INDEX,
    EVIDENCE_REASON_NOT_RELOCATED,
    EVIDENCE_REASON_PATH_ONLY,
    EVIDENCE_REASON_RESPONSE_BUDGET,
    MAX_SEARCH_EVIDENCE_HITS,
    MAX_SEARCH_EVIDENCE_SNIPPET_LINES,
    PROJECT_SEARCH_MAX_BYTES,
    ProjectSearchEvidence,
    ProjectSearchHit,
    ProjectSearchKind,
    ProjectSearchScope,
    project_search_hit_payload,
    search_project,
)
from harness.storage import connect_database, initialize_database


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _indexed_repo(connection: sqlite3.Connection, root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-b", "main")
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    return workspace.workspace_id


def _commit(root: Path) -> None:
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-m",
        "index",
    )


def _search_code(connection: sqlite3.Connection, workspace_id: str, query: str, limit: int = 5):
    return search_project(
        connection,
        workspace_id,
        query,
        scope=ProjectSearchScope.CODE,
        limit=limit,
    )


def test_search_evidence_rereads_current_file(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        marker = "live_reread_marker_alpha"
        (root / "src").mkdir()
        (root / "src" / "evidence_reread.py").write_text(
            f"def locate_{marker}():\n    return '{marker}'\n",
            encoding="utf-8",
        )
        _commit(root)
        scan_workspace(connection, workspace_id)

        results = _search_code(connection, workspace_id, marker)
        assert results
        hit = results[0]
        assert hit.ref == "code:src/evidence_reread.py"
        assert hit.evidence is not None
        assert hit.evidence_reason is None
        assert marker in hit.evidence.snippet
        assert hit.evidence.start_line >= 1
        assert hit.evidence.end_line >= hit.evidence.start_line
        encoded = json.dumps(hit.evidence.to_wire(), ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= PROJECT_SEARCH_MAX_BYTES
    finally:
        connection.close()


def test_search_evidence_requires_indexed_sha_match(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        (root / "sha_lock.py").write_text(
            "def sha_lock_unique():\n    return 1\n", encoding="utf-8"
        )
        _commit(root)
        scan_workspace(connection, workspace_id)
        workspace = get_workspace(connection, workspace_id)
        mismatched = read_current_search_text(
            workspace,
            "sha_lock.py",
            expected_content_sha256="ab" * 32,
        )
        assert mismatched.status is SearchEvidenceReadStatus.CHANGED_SINCE_INDEX
        assert mismatched.text is None

        fake = "cd" * 32
        connection.execute(
            "UPDATE indexed_files SET content_sha256 = ? WHERE relative_path = ?",
            (fake, "sha_lock.py"),
        )
        connection.execute(
            "UPDATE indexed_search_documents SET content_sha256 = ? WHERE relative_path = ?",
            (fake, "sha_lock.py"),
        )
        results = _search_code(connection, workspace_id, "sha_lock_unique")
        assert results[0].path == "sha_lock.py"
        assert results[0].evidence is None
        assert results[0].evidence_reason == EVIDENCE_REASON_CHANGED_SINCE_INDEX
        assert "return 1" not in json.dumps(project_search_hit_payload(results[0]))
    finally:
        connection.close()


def test_search_evidence_rejects_symlink_swap(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        target = tmp_path / "outside_secret.txt"
        target.write_text("SYMLINK_SWAP_SECRET_OUTSIDE\n", encoding="utf-8")
        source = root / "symlink_swap.py"
        source.write_text("def symlink_swap_unique():\n    return 'inside'\n", encoding="utf-8")
        _commit(root)
        scan_workspace(connection, workspace_id)
        source.unlink()
        source.symlink_to(target)

        results = _search_code(connection, workspace_id, "symlink_swap_unique")
        assert results[0].path == "symlink_swap.py"
        assert results[0].evidence is None
        assert results[0].evidence_reason == EVIDENCE_REASON_NOT_RELOCATED
        payload = json.dumps(project_search_hit_payload(results[0]))
        assert "SYMLINK_SWAP_SECRET_OUTSIDE" not in payload
        assert "inside" not in payload
    finally:
        connection.close()


def test_search_evidence_is_workspace_contained(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        escaped = tmp_path / "escaped.txt"
        escaped.write_text("ESCAPED_WORKSPACE_SECRET\n", encoding="utf-8")
        source = root / "contained_unique.py"
        source.write_text("def contained_unique():\n    return True\n", encoding="utf-8")
        _commit(root)
        scan_workspace(connection, workspace_id)
        source.unlink()
        os.symlink(escaped, source)

        results = _search_code(connection, workspace_id, "contained_unique")
        assert results[0].path == "contained_unique.py"
        assert results[0].evidence is None
        assert results[0].evidence_reason == EVIDENCE_REASON_NOT_RELOCATED
        assert "ESCAPED_WORKSPACE_SECRET" not in json.dumps(project_search_hit_payload(results[0]))
    finally:
        connection.close()


def test_search_evidence_rejects_binary_or_invalid_utf8(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        (root / "utf8_probe.py").write_text(
            "def utf8_probe_unique():\n    return 'ok'\n",
            encoding="utf-8",
        )
        _commit(root)
        scan_workspace(connection, workspace_id)

        payload = b"\xff\xfe utf8_probe_unique not valid"
        (root / "utf8_probe.py").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        connection.execute(
            "UPDATE indexed_files SET content_sha256 = ?, size_bytes = ? WHERE relative_path = ?",
            (digest, len(payload), "utf8_probe.py"),
        )
        connection.execute(
            "UPDATE indexed_search_documents SET content_sha256 = ? WHERE relative_path = ?",
            (digest, "utf8_probe.py"),
        )
        results = _search_code(connection, workspace_id, "utf8_probe_unique")
        assert results[0].path == "utf8_probe.py"
        assert results[0].evidence is None
        assert results[0].evidence_reason == EVIDENCE_REASON_NOT_RELOCATED
        assert "return 'ok'" not in json.dumps(project_search_hit_payload(results[0]))
    finally:
        connection.close()


def test_search_evidence_respects_per_hit_line_budget(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        compact = ["line_budget_alpha"]
        compact.extend("pad" for _ in range(MAX_SEARCH_EVIDENCE_SNIPPET_LINES - 2))
        compact.append("line_budget_beta")
        (root / "compact_window.py").write_text("\n".join(compact) + "\n", encoding="utf-8")
        wide = [
            "line_budget_alpha",
            *("pad" for _ in range(MAX_SEARCH_EVIDENCE_SNIPPET_LINES + 2)),
            "line_budget_beta",
        ]
        (root / "wide_window.py").write_text("\n".join(wide) + "\n", encoding="utf-8")
        _commit(root)
        scan_workspace(connection, workspace_id)

        compact_hits = _search_code(connection, workspace_id, "line_budget_alpha line_budget_beta")
        compact_hit = next(hit for hit in compact_hits if hit.path == "compact_window.py")
        assert compact_hit.evidence is not None
        assert (
            compact_hit.evidence.end_line - compact_hit.evidence.start_line + 1
            <= MAX_SEARCH_EVIDENCE_SNIPPET_LINES
        )
        wide_hit = next(hit for hit in compact_hits if hit.path == "wide_window.py")
        assert wide_hit.evidence is None
        assert wide_hit.evidence_reason == EVIDENCE_REASON_NOT_RELOCATED
    finally:
        connection.close()


def test_search_evidence_respects_global_response_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        original = retrieval_module.read_current_search_text
        reads: list[str] = []

        def counting_read(workspace, relative_path, **kwargs):
            reads.append(relative_path)
            return original(workspace, relative_path, **kwargs)

        monkeypatch.setattr(retrieval_module, "read_current_search_text", counting_read)
        for index in range(1000):
            (root / f"bulk_{index:04}_evidence_token.py").write_text(
                f"evidence_token = {index}\n" + ("x" * 200) + "\n",
                encoding="utf-8",
            )
        _commit(root)
        scan_workspace(connection, workspace_id)

        results = _search_code(connection, workspace_id, "evidence_token", limit=10)
        assert len(results) == 10
        assert len(reads) == MAX_SEARCH_EVIDENCE_HITS
        assert sum(1 for hit in results if hit.evidence is not None) <= MAX_SEARCH_EVIDENCE_HITS
        payload = {
            "query": "evidence_token",
            "scope": "code",
            "results": [project_search_hit_payload(hit) for hit in results],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= PROJECT_SEARCH_MAX_BYTES
        assert "bm25" not in encoded.decode("utf-8").casefold()
    finally:
        connection.close()


def _code_hit(
    path: str,
    *,
    evidence: ProjectSearchEvidence | None,
    evidence_reason: str | None = None,
    title: str | None = None,
) -> ProjectSearchHit:
    return ProjectSearchHit(
        ref=f"code:{path}",
        kind=ProjectSearchKind.CODE,
        title=path if title is None else title,
        location=path,
        short_summary=None,
        match_reason="lexical content (all terms)",
        freshness="indexed_snapshot",
        path=path,
        evidence=evidence,
        evidence_reason=evidence_reason,
    )


def test_response_budget_drop_is_distinct_from_relocate_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "budget_token"
    snippet = f"{query} lives in the current window"
    evidence = ProjectSearchEvidence(
        start_line=1,
        end_line=1,
        snippet=snippet,
        truncated=False,
    )
    hits = [
        _code_hit("kept.py", evidence=evidence),
        _code_hit(
            "reloc_fail.py",
            evidence=None,
            evidence_reason=EVIDENCE_REASON_NOT_RELOCATED,
        ),
        _code_hit("dropped.py", evidence=evidence),
        _code_hit("unread.py", evidence=None, evidence_reason=None),
    ]

    def payload_len(candidates: list[ProjectSearchHit]) -> int:
        return len(
            json.dumps(
                {
                    "query": query,
                    "scope": "all",
                    "results": [project_search_hit_payload(hit) for hit in candidates],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    two_evidence = payload_len(hits)
    monkeypatch.setattr(
        retrieval_module,
        "_SEARCH_EVIDENCE_ENVELOPE_RESERVE_BYTES",
        PROJECT_SEARCH_MAX_BYTES - two_evidence + 1,
    )

    fitted = retrieval_module._fit_search_hits_to_response_budget(hits, query)
    by_path = {hit.path: hit for hit in fitted}
    assert by_path["reloc_fail.py"].evidence is None
    assert by_path["reloc_fail.py"].evidence_reason == EVIDENCE_REASON_NOT_RELOCATED
    assert by_path["unread.py"].evidence is None
    assert by_path["unread.py"].evidence_reason is None
    assert by_path["dropped.py"].evidence is None
    assert by_path["dropped.py"].evidence_reason == EVIDENCE_REASON_RESPONSE_BUDGET
    assert by_path["kept.py"].evidence is not None
    assert query in by_path["kept.py"].evidence.snippet
    assert by_path["kept.py"].evidence_reason is None


def test_search_evidence_contains_query_terms_when_relocated(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        (root / "terms.py").write_text(
            "prefix\nrelocate_alpha_term and relocate_beta_term together\nsuffix\n",
            encoding="utf-8",
        )
        _commit(root)
        scan_workspace(connection, workspace_id)
        results = _search_code(connection, workspace_id, "relocate_alpha_term relocate_beta_term")
        assert results[0].evidence is not None
        snippet = results[0].evidence.snippet.casefold()
        assert "relocate_alpha_term" in snippet
        assert "relocate_beta_term" in snippet
    finally:
        connection.close()


def test_changed_file_returns_locator_without_stale_evidence(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        stale = "stale_source_body_unique"
        fresh = "fresh_source_body_unique"
        (root / "changed.py").write_text(f"def {stale}():\n    return 1\n", encoding="utf-8")
        _commit(root)
        scan_workspace(connection, workspace_id)
        (root / "changed.py").write_text(f"def {fresh}():\n    return 2\n", encoding="utf-8")

        results = _search_code(connection, workspace_id, stale)
        assert results[0].path == "changed.py"
        assert results[0].evidence is None
        assert results[0].evidence_reason == EVIDENCE_REASON_CHANGED_SINCE_INDEX
        payload = json.dumps(project_search_hit_payload(results[0]))
        assert stale not in payload
        assert fresh not in payload
    finally:
        connection.close()


def test_path_only_hit_returns_no_evidence_with_reason(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        workspace_id = _indexed_repo(connection, root)
        (root / "path_only_unique_name.bin").write_bytes(b"\x00binary-not-content-fts")
        _commit(root)
        scan_workspace(connection, workspace_id)
        results = _search_code(connection, workspace_id, "path_only_unique_name")
        assert results[0].path == "path_only_unique_name.bin"
        assert results[0].evidence is None
        assert results[0].evidence_reason == EVIDENCE_REASON_PATH_ONLY
    finally:
        connection.close()
