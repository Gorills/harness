from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_dashboard_actions import _database, _git

import harness.git_operations as operations
from harness.git_operations import (
    FILE_LIMIT,
    RECENT_BRANCH_LIMIT,
    GitOperationError,
    GitOperationRequest,
    read_git_snapshot,
    run_git_operation,
)
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database


def _output(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root).decode().strip()


def _identity(root: Path) -> None:
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgSign", "false")


def test_branch_search_is_bounded_and_checks_entire_inventory(tmp_path: Path) -> None:
    root, database, workspace = _database(tmp_path)
    head = _output(root, "rev-parse", "HEAD")
    updates = "".join(f"create refs/heads/feature/branch-{i:03d} {head}\n" for i in range(300))
    subprocess.run(
        ["git", "update-ref", "--stdin"],
        input=updates.encode(),
        cwd=root,
        check=True,
        capture_output=True,
    )
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        assert len(state.branches) == RECENT_BRANCH_LIMIT
        assert state.branch_count == state.matching_branch_count == 301
        assert state.branches[0].name == "main"
        match = read_git_snapshot(connection, workspace, query="BRANCH-299")
        assert [branch.name for branch in match.branches] == ["feature/branch-299"]
        assert match.matching_branch_count == 1
        _git(root, "branch", "feature/branch")
        exact = read_git_snapshot(connection, workspace, query="feature/branch")
        assert exact.branches[0].name == "feature/branch"


def test_switch_and_create_preserve_dirty_files_and_reject_stale_state(tmp_path: Path) -> None:
    root, database, workspace = _database(tmp_path)
    _git(root, "branch", "feature")
    with connect_database(database) as connection:
        initial = read_git_snapshot(connection, workspace)
        (root / "tracked.txt").write_text("unsaved\n")
        with pytest.raises(GitOperationError, match="изменилось"):
            run_git_operation(
                connection,
                GitOperationRequest("git_switch", workspace, initial.token, "refs/heads/feature"),
            )
        dirty = read_git_snapshot(connection, workspace)
        with pytest.raises(GitOperationError, match="коммит"):
            run_git_operation(
                connection,
                GitOperationRequest("git_switch", workspace, dirty.token, "refs/heads/feature"),
            )
        run_git_operation(
            connection, GitOperationRequest("git_create", workspace, dirty.token, "new-work")
        )
        assert _output(root, "branch", "--show-current") == "new-work"
        assert (root / "tracked.txt").read_text() == "unsaved\n"
        state = read_git_snapshot(connection, workspace)
        with pytest.raises(GitOperationError):
            run_git_operation(
                connection, GitOperationRequest("git_create", workspace, state.token, "--force")
            )
        assert _output(root, "branch", "--show-current") == "new-work"


def test_staging_uses_literal_paths_and_commit_contains_only_prepared_changes(
    tmp_path: Path,
) -> None:
    root, database, workspace = _database(tmp_path)
    _identity(root)
    special = ":(glob)*.txt"
    (root / special).write_text("selected\n")
    (root / "other.txt").write_text("keep outside commit\n")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_stage", workspace, state.token, special)
        )
        staged = read_git_snapshot(connection, workspace)
        assert staged.staged_count == 1
        (root / special).write_text("later changes\n")
        with pytest.raises(GitOperationError, match="изменилось"):
            run_git_operation(
                connection,
                GitOperationRequest("git_commit", workspace, staged.token, "selected only"),
            )
        fresh = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_commit", workspace, fresh.token, "selected only")
        )
        assert _output(root, "show", f"HEAD:{special}") == "selected"
        assert (root / special).read_text() == "later changes\n"
        assert _output(root, "ls-tree", "--name-only", "HEAD") == special + "\ntracked.txt"


def test_unstage_rename_keeps_files_and_removes_both_index_entries(tmp_path: Path) -> None:
    root, database, workspace = _database(tmp_path)
    _git(root, "mv", "tracked.txt", "renamed.txt")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        assert state.files[0].original_path == "tracked.txt"
        run_git_operation(
            connection, GitOperationRequest("git_unstage", workspace, state.token, "renamed.txt")
        )
        assert _output(root, "diff", "--cached", "--name-only") == ""
        assert (root / "renamed.txt").read_text() == "baseline\n"
        _git(root, "add", "-A")
        (root / "renamed.txt").write_text("new rename changes\n")
        renamed = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_stage", workspace, renamed.token, "renamed.txt")
        )
        assert _output(root, "show", ":renamed.txt") == "new rename changes"


@pytest.mark.parametrize("unborn", [False, True])
def test_bulk_staging_covers_all_pages_and_unstaging_preserves_working_files(
    tmp_path: Path, unborn: bool
) -> None:
    root, database, workspace = _database(tmp_path)
    if unborn:
        _git(root, "update-ref", "-d", "refs/heads/main")
        _git(root, "read-tree", "--empty")
    else:
        (root / "tracked.txt").unlink()
    (root / ".gitignore").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("leave ignored\n")
    for i in range(FILE_LIMIT + 3):
        (root / f"new-{i:03d}").write_text("new file\n")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace, page=2)
        assert state.file_count > FILE_LIMIT
        assert len(state.files) < state.file_count
        run_git_operation(connection, GitOperationRequest("git_stage_all", workspace, state.token))
        staged = read_git_snapshot(connection, workspace)
        assert staged.staged_count == state.file_count
        assert "ignored.txt" not in _output(root, "ls-files").splitlines()
        (root / "new-000").write_text("later working copy\n")
        with pytest.raises(GitOperationError, match="изменилось"):
            run_git_operation(
                connection, GitOperationRequest("git_unstage_all", workspace, staged.token)
            )
        fresh = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_unstage_all", workspace, fresh.token)
        )
        assert read_git_snapshot(connection, workspace).staged_count == 0
        assert (root / "new-000").read_text() == "later working copy\n"
        assert (root / "new-026").read_text() == "new file\n"
        assert (root / "ignored.txt").read_text() == "leave ignored\n"
        assert (root / "tracked.txt").exists() == unborn


@pytest.mark.parametrize("finish", ["abort", "commit", "commit_all"])
def test_merge_direction_conflict_recovery_and_abort(tmp_path: Path, finish: str) -> None:
    root, database, workspace = _database(tmp_path)
    _identity(root)
    _git(root, "switch", "-c", "source")
    (root / "tracked.txt").write_text("source\n")
    _git(root, "commit", "-am", "source change")
    _git(root, "switch", "main")
    (root / "tracked.txt").write_text("destination\n")
    _git(root, "commit", "-am", "destination change")
    source_head = _output(root, "rev-parse", "source")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        with pytest.raises(GitOperationError):
            run_git_operation(
                connection,
                GitOperationRequest("git_merge", workspace, state.token, "refs/heads/source"),
            )
        conflict = read_git_snapshot(connection, workspace)
        assert conflict.operation == "merge"
        assert conflict.conflict_count == 1
        assert conflict.branch == "main"
        with pytest.raises(GitOperationError):
            run_git_operation(
                connection,
                GitOperationRequest("git_switch", workspace, conflict.token, "refs/heads/source"),
            )
        if finish == "abort":
            run_git_operation(
                connection, GitOperationRequest("git_abort_merge", workspace, conflict.token)
            )
            assert (root / "tracked.txt").read_text() == "destination\n"
        else:
            with pytest.raises(GitOperationError, match="текущую операцию"):
                run_git_operation(
                    connection, GitOperationRequest("git_unstage_all", workspace, conflict.token)
                )
            (root / "tracked.txt").write_text("resolved\n")
            fresh = read_git_snapshot(connection, workspace)
            run_git_operation(
                connection,
                GitOperationRequest(
                    "git_stage_all" if finish == "commit_all" else "git_stage",
                    workspace,
                    fresh.token,
                    "" if finish == "commit_all" else "tracked.txt",
                ),
            )
            staged = read_git_snapshot(connection, workspace)
            run_git_operation(
                connection,
                GitOperationRequest("git_commit", workspace, staged.token, "resolve merge"),
            )
            assert len(_output(root, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
        assert read_git_snapshot(connection, workspace).operation is None
        assert _output(root, "rev-parse", "source") == source_head


def test_worktree_occupied_branch_and_remote_tracking(tmp_path: Path) -> None:
    root, database, workspace = _database(tmp_path)
    worktree = tmp_path / "second"
    _git(root, "worktree", "add", "-b", "busy", str(worktree))
    _git(root, "remote", "add", "origin", str(tmp_path / "remote.git"))
    _git(root, "update-ref", "refs/remotes/origin/topic", "HEAD")
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/topic")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        assert next(branch for branch in state.branches if branch.name == "busy").occupied
        assert "origin/HEAD" not in [branch.name for branch in state.branches]
        with pytest.raises(GitOperationError):
            run_git_operation(
                connection,
                GitOperationRequest("git_switch", workspace, state.token, "refs/heads/busy"),
            )
        assert _output(root, "branch", "--show-current") == "main"
        run_git_operation(
            connection,
            GitOperationRequest("git_switch", workspace, state.token, "refs/remotes/origin/topic"),
        )
        assert _output(root, "branch", "--show-current") == "topic"


def test_unborn_detached_and_file_pagination(tmp_path: Path) -> None:
    root = tmp_path / "new"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _identity(root)
    database = tmp_path / "harness.db"
    initialize_database(database)
    with connect_database(database) as connection:
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        for i in range(FILE_LIMIT + 3):
            (root / f"file-{i:03d}").write_text("new file\n")
        state = read_git_snapshot(connection, workspace.workspace_id)
        assert state.branch == "main" and state.head is None
        assert len(state.files) == FILE_LIMIT
        assert len(read_git_snapshot(connection, workspace.workspace_id, page=2).files) == 3
        run_git_operation(
            connection,
            GitOperationRequest("git_stage", workspace.workspace_id, state.token, "file-000"),
        )
        (root / "file-000").write_text("later working copy\n")
        staged = read_git_snapshot(connection, workspace.workspace_id)
        run_git_operation(
            connection,
            GitOperationRequest("git_unstage", workspace.workspace_id, staged.token, "file-000"),
        )
        assert (root / "file-000").read_text() == "later working copy\n"
        unprepared = read_git_snapshot(connection, workspace.workspace_id)
        run_git_operation(
            connection,
            GitOperationRequest("git_stage", workspace.workspace_id, unprepared.token, "file-000"),
        )
        prepared = read_git_snapshot(connection, workspace.workspace_id)
        run_git_operation(
            connection,
            GitOperationRequest("git_commit", workspace.workspace_id, prepared.token, "first"),
        )
        _git(root, "switch", "--detach", "HEAD")
        detached = read_git_snapshot(connection, workspace.workspace_id)
        assert detached.branch is None and detached.head is not None
        run_git_operation(
            connection,
            GitOperationRequest("git_create", workspace.workspace_id, detached.token, "continued"),
        )
        assert _output(root, "branch", "--show-current") == "continued"


@pytest.mark.parametrize("changed", ["refs", "index"])
def test_snapshot_rejects_refs_or_index_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    root, database, workspace = _database(tmp_path)
    _identity(root)
    _git(root, "branch", "source")
    (root / "tracked.txt").write_text("staged\n")
    _git(root, "add", "tracked.txt")
    (root / "tracked.txt").write_text("working copy\n")
    blob = (
        subprocess.check_output(
            ["git", "hash-object", "-w", "--stdin"], input=b"unseen staged\n", cwd=root
        )
        .decode()
        .strip()
    )
    original_run = operations._run
    changed_once = False

    def run(
        path: Path, arguments: list[str], deadline: float, *, console: list[str] | None = None
    ) -> bytes:
        nonlocal changed_once
        result = original_run(path, arguments, deadline, console=console)
        if arguments == ["ls-files", "--stage", "-z"] and not changed_once:
            changed_once = True
            if changed == "index":
                _git(root, "update-index", "--cacheinfo", f"100644,{blob},tracked.txt")
            else:
                _git(root, "branch", "new-ref")
        return result

    monkeypatch.setattr(operations, "_run", run)
    with (
        connect_database(database) as connection,
        pytest.raises(GitOperationError, match="во время чтения"),
    ):
        read_git_snapshot(connection, workspace)


def test_non_utf8_file_names_render_without_lossy_actions(tmp_path: Path) -> None:
    from harness.dashboard_git import render_git_panel

    root, database, workspace = _database(tmp_path)
    filename = b"unusual-\xff.txt"
    path = os.fsencode(root) + b"/" + filename
    with open(path, "wb") as stream:
        stream.write(b"file\n")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        html = render_git_panel(state, f"/workspaces/{workspace}/git/")
    html.encode("utf-8")
    assert "для этого имени используйте терминал" in html
    assert 'value="git_stage"' not in html


def test_copy_actions_do_not_change_independent_source_index(tmp_path: Path) -> None:
    root, database, workspace = _database(tmp_path)
    _identity(root)
    contents = "".join(f"line {i}\n" for i in range(100))
    (root / "tracked.txt").write_text(contents)
    _git(root, "commit", "-am", "copy baseline")
    _git(root, "config", "status.renames", "copies")
    (root / "tracked.txt").write_text("prepared source\n" + contents)
    (root / "copy.txt").write_text(contents)
    _git(root, "add", "tracked.txt", "copy.txt")
    original_oid = _output(root, "rev-parse", ":tracked.txt")
    (root / "tracked.txt").write_text("unprepared source\n" + contents)
    (root / "copy.txt").write_text("copy changes\n" + contents)
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        copied = next(file for file in state.files if file.path == "copy.txt")
        assert copied.index_status == "C"
        run_git_operation(
            connection, GitOperationRequest("git_stage", workspace, state.token, "copy.txt")
        )
        assert _output(root, "rev-parse", ":tracked.txt") == original_oid
        fresh = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_unstage", workspace, fresh.token, "copy.txt")
        )
        assert _output(root, "rev-parse", ":tracked.txt") == original_oid
        assert (root / "copy.txt").read_text().startswith("copy changes")


def test_merge_uses_source_oid_seen_in_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, workspace = _database(tmp_path)
    _identity(root)
    _git(root, "switch", "-c", "source")
    (root / "first.txt").write_text("reviewed change\n")
    _git(root, "add", "first.txt")
    _git(root, "commit", "-m", "reviewed source")
    reviewed = _output(root, "rev-parse", "HEAD")
    (root / "later.txt").write_text("unseen source change\n")
    _git(root, "add", "later.txt")
    _git(root, "commit", "-m", "unseen source")
    unseen = _output(root, "rev-parse", "HEAD")
    _git(root, "switch", "main")
    _git(root, "update-ref", "refs/heads/source", reviewed)
    original_run = operations._run

    def run(
        path: Path, arguments: list[str], deadline: float, *, console: list[str] | None = None
    ) -> bytes:
        if arguments[0] == "merge":
            assert arguments[-1] == reviewed
            _git(root, "update-ref", "refs/heads/source", unseen)
        return original_run(path, arguments, deadline, console=console)

    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        monkeypatch.setattr(operations, "_run", run)
        run_git_operation(
            connection,
            GitOperationRequest("git_merge", workspace, state.token, "refs/heads/source"),
        )
        assert (root / "first.txt").is_file()
        assert not (root / "later.txt").exists()
