from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import harness.git_applicability as applicability_module
from harness.git_applicability import GitApplicabilityError, WorkspaceApplicability
from harness.index import scan_workspace
from harness.knowledge import (
    KnowledgeAnchorDraft,
    KnowledgeDraft,
    KnowledgeKind,
    get_knowledge_card,
)
from harness.registry import create_project, register_workspace, register_workspace_for_init
from harness.retrieval import (
    ProjectRetrievalRefError,
    ProjectSearchScope,
    read_project_context,
    search_project,
)
from harness.storage import connect_database, initialize_database
from harness.task_checkpoints import TaskCheckpointMechanicalError, TaskCheckpointMutation
from harness.task_workflow import task_checkpoint, task_resume, task_start
from harness.tasks import (
    TaskRevisionConflictError,
    TaskState,
    TaskWorkspaceConflictError,
    get_relevant_task,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Harness Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgSign=false",
            *arguments,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@contextmanager
def _workspace(
    tmp_path: Path, *, git: bool = True
) -> Iterator[tuple[sqlite3.Connection, Path, str]]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "service.py").write_text("value = 'base'\n")
    if git:
        _git(root, "init", "-b", "main")
        _commit(root, "base")
    database = tmp_path / "state.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        scan_workspace(connection, workspace.workspace_id)
        yield connection, root, workspace.workspace_id
    finally:
        connection.close()


def _checkpoint(
    connection: sqlite3.Connection,
    workspace_id: str,
    task_id: str,
    revision: int,
    summary: str = "FeatureOnlyEvidence",
) -> TaskCheckpointMutation:
    return task_checkpoint(
        connection,
        workspace_id,
        task_id,
        expected_revision=revision,
        state=TaskState.WORKING,
        summary=summary,
        knowledge=(
            KnowledgeDraft(
                kind=KnowledgeKind.BEHAVIOR,
                title=summary,
                body=summary,
                anchors=(KnowledgeAnchorDraft(path="service.py"),),
            ),
            KnowledgeDraft(
                kind=KnowledgeKind.ARCHITECTURE_RATIONALE,
                title=summary + " rationale",
                body=summary + " rationale",
            ),
        ),
    )


@pytest.mark.parametrize("integration", ["ff", "merge", "squash", "cherry-pick"])
def test_branch_visibility_before_and_after_code_integration(
    tmp_path: Path, integration: str
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "FeatureOnlyEvidence")
        (root / "service.py").write_text("value = 'feature'\n")
        feature_head = _commit(root, "feature")
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, task.revision)
        refs = (
            f"task:{task.task_id}",
            *(f"knowledge:{k.knowledge_id}" for k in checkpoint.knowledge_cards),
        )
        _git(root, "checkout", "main")
        for query in ("FeatureOnlyEvidence", task.task_id, task.task_id[:12]):
            assert (
                search_project(
                    connection, workspace_id, query, scope=ProjectSearchScope.TASKS, limit=5
                )
                == ()
            )
        assert (
            search_project(
                connection,
                workspace_id,
                "FeatureOnlyEvidence",
                scope=ProjectSearchScope.KNOWLEDGE,
                limit=5,
            )
            == ()
        )
        resolver = WorkspaceApplicability(connection, workspace_id)
        assert get_relevant_task(connection, workspace_id, applicability=resolver) is None
        resolver.validate()
        for ref in refs:
            with pytest.raises(ProjectRetrievalRefError):
                read_project_context(connection, workspace_id, (ref,))
        with pytest.raises(TaskWorkspaceConflictError):
            task_resume(
                connection, workspace_id, task.task_id, expected_revision=checkpoint.task.revision
            )
        if integration == "ff":
            _git(root, "merge", "--ff-only", "feature")
        else:
            (root / "unrelated.py").write_text("unrelated = True\n")
            _commit(root, "unrelated")
            if integration == "merge":
                _git(root, "merge", "--no-ff", "feature", "-m", "merge")
            elif integration == "squash":
                _git(root, "merge", "--squash", "feature")
                _commit(root, "squashed")
            else:
                _git(root, "cherry-pick", feature_head)
        assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        assert len(read_project_context(connection, workspace_id, refs)) == 3
        (root / "service.py").write_text("value = 'later edit'\n")
        _commit(root, "later edit")
        later = WorkspaceApplicability(connection, workspace_id)
        assert later.task_visible(task.task_id) is (integration in {"ff", "merge"})
        assert not any(later.knowledge_visible(card) for card in checkpoint.knowledge_cards)


def test_dirty_checkpoint_merge_and_originating_task_continuity(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Draft")
        (root / "service.py").write_text("value = 'draft'\n")
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, 1)
        (root / "service.py").write_text("value = 'ongoing edit'\n")
        resolver = WorkspaceApplicability(connection, workspace_id)
        assert resolver.task_visible(task.task_id)
        assert not any(resolver.knowledge_visible(card) for card in checkpoint.knowledge_cards)
        _git(root, "restore", "service.py")
        resolver = WorkspaceApplicability(connection, workspace_id)
        assert resolver.task_visible(task.task_id)
        assert not any(resolver.knowledge_visible(card) for card in checkpoint.knowledge_cards)
        (root / "service.py").write_text("value = 'draft'\n")
        _commit(root, "draft committed after checkpoint")
        _git(root, "checkout", "main")
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        _git(root, "merge", "--ff-only", "feature")
        assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        merged = WorkspaceApplicability(connection, workspace_id)
        assert all(merged.knowledge_visible(card) for card in checkpoint.knowledge_cards)


@pytest.mark.parametrize("resume_first", [False, True])
def test_explicit_initial_handoff_after_branch_edits_and_commit(
    tmp_path: Path, resume_first: bool
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        task = task_start(connection, workspace_id, "Started before branch")
        _git(root, "checkout", "-b", "feature")
        (root / "service.py").write_text("value = 'feature'\n")
        _commit(root, "feature")
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        if resume_first:
            with pytest.raises(TaskRevisionConflictError):
                task_resume(connection, workspace_id, task.task_id, expected_revision=999)
            task = task_resume(connection, workspace_id, task.task_id, expected_revision=1)
            assert task.revision == 2
            assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, task.revision)
        assert checkpoint.task.task_id == task.task_id
        _git(root, "checkout", "main")
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)


def test_no_git_then_initial_commit_keeps_task_identity(tmp_path: Path) -> None:
    with _workspace(tmp_path, git=False) as (connection, root, workspace_id):
        task = task_start(connection, workspace_id, "Greenfield")
        _git(root, "init", "-b", "main")
        _commit(root, "initial")
        registration = register_workspace_for_init(connection, path=root)
        assert registration.workspace.workspace_id == workspace_id
        _git(root, "checkout", "-b", "feature")
        (root / "service.py").write_text("value = 'feature'\n")
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, 1)
        _commit(root, "feature")
        _git(root, "checkout", "main")
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        _git(root, "merge", "--ff-only", "feature")
        assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        assert checkpoint.task.task_id == task.task_id


def test_earlier_knowledge_uses_its_checkpoint_not_later_task_work(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Multiple changes")
        (root / "service.py").write_text("value = 'first'\n")
        first_head = _commit(root, "first")
        first = _checkpoint(connection, workspace_id, task.task_id, 1)
        (root / "service.py").write_text("value = 'second'\n")
        _commit(root, "second")
        second = _checkpoint(connection, workspace_id, task.task_id, first.task.revision)
        _git(root, "checkout", "main")
        _git(root, "merge", "--ff-only", first_head)
        resolver = WorkspaceApplicability(connection, workspace_id)
        assert not resolver.task_visible(task.task_id)
        assert resolver.knowledge_visible(first.knowledge_cards[0])
        assert not resolver.knowledge_visible(second.knowledge_cards[0])
        read_project_context(
            connection, workspace_id, (f"knowledge:{first.knowledge_cards[0].knowledge_id}",)
        )


def test_incompatible_old_checkpoint_is_hidden_from_search_and_recent_history(
    tmp_path: Path,
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Evolution")
        (root / "service.py").write_text("value = 'obsolete'\n")
        first = _checkpoint(connection, workspace_id, task.task_id, 1, "obsolete_unique_payload")
        (root / "service.py").write_text("value = 'current'\n")
        second = _checkpoint(
            connection, workspace_id, task.task_id, first.task.revision, "current_unique_payload"
        )
        _commit(root, "final")
        _git(root, "checkout", "main")
        _git(root, "merge", "--squash", "feature")
        _commit(root, "squash")
        resolver = WorkspaceApplicability(connection, workspace_id)
        assert resolver.task_visible(task.task_id)
        assert not resolver.checkpoint_visible(first.checkpoint.checkpoint_id)
        assert resolver.checkpoint_visible(second.checkpoint.checkpoint_id)
        hits = search_project(
            connection,
            workspace_id,
            "obsolete_unique_payload",
            scope=ProjectSearchScope.TASKS,
            limit=5,
        )
        assert all("obsolete_unique_payload" not in (hit.short_summary or "") for hit in hits)
        context = read_project_context(connection, workspace_id, (f"task:{task.task_id}",))
        assert "obsolete_unique_payload" not in str(context)


def test_branch_and_consulted_content_changes_invalidate_request(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        resolver = WorkspaceApplicability(connection, workspace_id)
        _git(root, "checkout", "-b", "feature")
        with pytest.raises(GitApplicabilityError, match="Git state changed"):
            resolver.validate()
        resolver = WorkspaceApplicability(connection, workspace_id)
        resolver.fingerprint("service.py")
        (root / "service.py").write_text("value = 'edited'\n")
        with pytest.raises(GitApplicabilityError):
            resolver.validate()


def test_same_named_branch_reset_does_not_rescue_committed_task(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        base = _git(root, "rev-parse", "HEAD")
        task = task_start(connection, workspace_id, "Reset")
        (root / "service.py").write_text("value = 'feature'\n")
        _commit(root, "feature")
        _checkpoint(connection, workspace_id, task.task_id, 1)
        _git(root, "reset", "--hard", base)
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)


def test_operator_knowledge_anchors_are_current_but_general_notes_remain_visible(
    tmp_path: Path,
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Manual knowledge")
        (root / "service.py").write_text("value = 'feature'\n")
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, 1)
        for card in checkpoint.knowledge_cards:
            connection.execute(
                "UPDATE knowledge_cards SET source_type='operator',source_task_id=NULL,source_checkpoint_id=NULL WHERE id=?",
                (card.knowledge_id,),
            )
        _git(root, "restore", "service.py")
        _git(root, "checkout", "main")
        resolver = WorkspaceApplicability(connection, workspace_id)
        anchored, general = (
            get_knowledge_card(connection, card.knowledge_id) for card in checkpoint.knowledge_cards
        )
        assert not resolver.knowledge_visible(anchored)
        assert resolver.knowledge_visible(general)
        with pytest.raises(ProjectRetrievalRefError):
            read_project_context(connection, workspace_id, (f"knowledge:{anchored.knowledge_id}",))
        read_project_context(connection, workspace_id, (f"knowledge:{general.knowledge_id}",))


def test_checkpoint_evidence_rejects_dirty_movement_after_mechanical_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import harness.task_checkpoints as checkpoints

    with _workspace(tmp_path) as (connection, root, workspace_id):
        task = task_start(connection, workspace_id, "Race")
        (root / "service.py").write_text("value = 'feature'\n")
        _commit(root, "feature")
        original = applicability_module.capture_checkpoint_evidence

        def change_before_capture(connection: sqlite3.Connection, checkpoint_id: str) -> None:
            (root / "service.py").write_text("value = 'new dirty change'\n")
            original(connection, checkpoint_id)

        monkeypatch.setattr(checkpoints, "capture_checkpoint_evidence", change_before_capture)
        with pytest.raises(GitApplicabilityError, match="dirty state changed"):
            _checkpoint(connection, workspace_id, task.task_id, 1)
        assert connection.execute(
            "SELECT revision FROM tasks WHERE id=?", (task.task_id,)
        ).fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM task_checkpoints").fetchone() == (0,)


def test_checkpoint_rolls_back_same_count_changed_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.task_checkpoints as checkpoints

    with _workspace(tmp_path) as (connection, root, workspace_id):
        task = task_start(connection, workspace_id, "Untracked swap")
        (root / "first.py").write_text("first = True\n")
        original = applicability_module.capture_checkpoint_evidence

        def swap_before_capture(connection: sqlite3.Connection, checkpoint_id: str) -> None:
            (root / "first.py").unlink()
            (root / "second.py").write_text("second = True\n")
            original(connection, checkpoint_id)

        monkeypatch.setattr(checkpoints, "capture_checkpoint_evidence", swap_before_capture)
        with pytest.raises(
            TaskCheckpointMechanicalError, match="changed during checkpoint evidence"
        ):
            _checkpoint(connection, workspace_id, task.task_id, 1)
        assert connection.execute(
            "SELECT revision FROM tasks WHERE id=?", (task.task_id,)
        ).fetchone() == (1,)
        for table in (
            "task_checkpoints",
            "task_git_evidence",
            "task_git_evidence_paths",
            "knowledge_cards",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_unknown_bounded_evidence_cannot_transfer_dirty_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Bounded")
        (root / "service.py").write_text("value = 'feature content exceeds test bound'\n")
        monkeypatch.setattr(applicability_module, "MAX_APPLICABILITY_FILE_BYTES", 4)
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, 1)
        assert connection.execute(
            "SELECT complete FROM task_git_evidence WHERE checkpoint_id=?",
            (checkpoint.checkpoint.checkpoint_id,),
        ).fetchone() == (0,)
        assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        _commit(root, "feature")
        _git(root, "checkout", "main")
        _git(root, "merge", "--ff-only", "feature")
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)


def test_request_deadline_applies_even_to_cached_task_visibility(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (connection, _root, workspace_id):
        task = task_start(connection, workspace_id, "Deadline")
        resolver = WorkspaceApplicability(connection, workspace_id)
        assert resolver.task_visible(task.task_id)
        resolver._deadline = 0
        with pytest.raises(GitApplicabilityError, match="deadline exceeded"):
            resolver.task_visible(task.task_id)


def test_capture_byte_budget_records_incomplete_proof_without_blocking_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Large change")
        (root / "one.py").write_text("one = 1\n")
        (root / "two.py").write_text("two = 2\n")
        monkeypatch.setattr(applicability_module, "MAX_APPLICABILITY_READ_BYTES", 10)
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, 1)
        assert checkpoint.task.revision == 2
        assert connection.execute(
            "SELECT complete FROM task_git_evidence WHERE checkpoint_id=?",
            (checkpoint.checkpoint.checkpoint_id,),
        ).fetchone() == (0,)
        assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        _commit(root, "large change")
        _git(root, "checkout", "main")
        _git(root, "merge", "--ff-only", "feature")
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)


def test_v23_migration_does_not_fabricate_legacy_dirty_evidence(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        _git(root, "checkout", "-b", "feature")
        task = task_start(connection, workspace_id, "Legacy dirty")
        (root / "service.py").write_text("value = 'feature'\n")
        _checkpoint(connection, workspace_id, task.task_id, 1)
        # Reconstruct v22 exactly for this additive migration; historical rows remain intact.
        connection.execute("DROP TABLE task_git_evidence_paths")
        connection.execute("DROP TABLE task_git_evidence")
        connection.execute("DROP TABLE task_git_origins")
        connection.execute("DELETE FROM schema_migrations WHERE version=23")
        _commit(root, "feature")
        _git(root, "checkout", "main")
        _git(root, "merge", "--ff-only", "feature")
        initialize_database(tmp_path / "state.db")
        for table in ("task_git_origins", "task_git_evidence", "task_git_evidence_paths"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert not WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)
        _git(root, "checkout", "feature")
        assert WorkspaceApplicability(connection, workspace_id).task_visible(task.task_id)


def test_checkpoint_counts_individual_untracked_paths_and_rejects_null_hashes(
    tmp_path: Path,
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        task = task_start(connection, workspace_id, "Untracked directory")
        directory = root / "newdir"
        directory.mkdir()
        (directory / "one.py").write_text("one = 1\n")
        (directory / "two.py").write_text("two = 2\n")
        checkpoint = _checkpoint(connection, workspace_id, task.task_id, 1)
        assert checkpoint.checkpoint.current_dirty_path_count == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM task_git_evidence_paths WHERE checkpoint_id=?",
            (checkpoint.checkpoint.checkpoint_id,),
        ).fetchone() == (2,)
        for kind in ("file", "symlink"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO task_git_evidence_paths(checkpoint_id,relative_path,kind,content_sha256) VALUES (?,'invalid',?,NULL)",
                    (checkpoint.checkpoint.checkpoint_id, kind),
                )


def test_visibility_filters_before_limit_and_memoizes_duplicate_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _workspace(tmp_path) as (connection, root, workspace_id):
        base = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "-b", "feature")
        (root / "service.py").write_text("value = 'feature'\n")
        feature = _commit(root, "feature")
        _git(root, "checkout", "main")
        for index in range(110):
            task_id = f"hidden{index}"
            connection.execute(
                "INSERT INTO tasks(id,workspace_id,title,state,revision,created_at,updated_at) VALUES (?,?,'Needle','completed',2,'c','u')",
                (task_id, workspace_id),
            )
            connection.execute(
                "INSERT INTO task_baselines(task_id,head,branch,captured_at,index_is_fresh,index_file_count,index_snapshot_sha256) VALUES (?,?,'feature','c',1,0,?)",
                (task_id, base, "0" * 64),
            )
            connection.execute(
                "INSERT INTO task_checkpoints(id,task_id,task_revision,state,summary,created_at,current_head,current_branch,current_dirty_path_count) VALUES (?,?,2,'completed','Needle','u',?,'feature',0)",
                (task_id, task_id, feature),
            )
            connection.execute(
                "INSERT INTO task_checkpoint_changed_paths(checkpoint_id,relative_path) VALUES (?,'service.py')",
                (task_id,),
            )
        visible = task_start(connection, workspace_id, "Relevant Needle")
        calls: list[object] = []
        original = subprocess.run

        def counted(*args: Any, **kwargs: Any) -> Any:
            if "merge-base" in args[0]:
                calls.append(args[0])
            return original(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counted)
        hits = search_project(
            connection, workspace_id, "Needle", scope=ProjectSearchScope.TASKS, limit=1
        )
        assert [hit.ref for hit in hits] == [f"task:{visible.task_id}"]
        assert len(calls) == 1
