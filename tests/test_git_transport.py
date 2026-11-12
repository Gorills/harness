from __future__ import annotations

from pathlib import Path
from time import monotonic

import pytest
from test_dashboard_actions import _database, _git
from test_git_operations import _identity, _output

import harness.git_operations as operations
from harness.git_operations import (
    GitOperationError,
    GitOperationRequest,
    read_git_snapshot,
    run_git_operation,
)
from harness.storage import connect_database


def _remote(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    root, database, workspace = _database(tmp_path)
    _identity(root)
    remote = tmp_path / "server.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "published")
    _git(root, "remote", "add", "origin", str(remote))
    return root, database, workspace, remote


def _publish(database: Path, workspace: str) -> None:
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        result = run_git_operation(
            connection,
            GitOperationRequest("git_publish", workspace, state.token, "published", "origin"),
        )
        assert result.success and "--set-upstream" in result.output


def _peer(tmp_path: Path, remote: Path) -> Path:
    peer = tmp_path / "peer"
    _git(tmp_path, "clone", str(remote), str(peer))
    _identity(peer)
    return peer


def test_publish_binds_and_push_uses_exact_branch_despite_mirror_defaults(tmp_path: Path) -> None:
    root, database, workspace, remote = _remote(tmp_path)
    _git(root, "branch", "other")
    _git(root, "tag", "-a", "do-not-push", "-m", "tag")
    _git(root, "config", "remote.origin.mirror", "true")
    _git(root, "config", "push.followTags", "true")
    _git(root, "config", "remote.origin.push", "+refs/heads/*:refs/heads/*")
    _publish(database, workspace)
    assert _output(remote, "for-each-ref", "--format=%(refname)") == "refs/heads/published"
    assert _output(root, "config", "branch.main.remote") == "origin"
    assert _output(root, "config", "branch.main.merge") == "refs/heads/published"
    (root / "tracked.txt").write_text("new commit\n")
    _git(root, "commit", "-am", "new")
    (root / "uncommitted.txt").write_text("local only\n")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        assert (state.upstream_remote, state.upstream_branch, state.ahead, state.behind) == (
            "origin",
            "published",
            1,
            0,
        )
        result = run_git_operation(
            connection, GitOperationRequest("git_push", workspace, state.token)
        )
        assert (
            result.success and "published" in result.output and "Код завершения: 0" in result.output
        )
    assert _output(remote, "rev-parse", "published") == _output(root, "rev-parse", "HEAD")
    assert (root / "uncommitted.txt").read_text() == "local only\n"


def test_fetch_updates_counts_and_pull_fast_forwards_the_linked_branch(tmp_path: Path) -> None:
    root, database, workspace, remote = _remote(tmp_path)
    _publish(database, workspace)
    peer = _peer(tmp_path, remote)
    (peer / "incoming.txt").write_text("from server\n")
    _git(peer, "add", "incoming.txt")
    _git(peer, "commit", "-m", "incoming")
    _git(peer, "push")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_fetch", workspace, state.token, "origin")
        )
        fresh = read_git_snapshot(connection, workspace)
        assert (fresh.ahead, fresh.behind) == (0, 1)
        result = run_git_operation(
            connection, GitOperationRequest("git_pull", workspace, fresh.token)
        )
        assert result.success and "Fast-forward" in result.output
    assert (root / "incoming.txt").read_text() == "from server\n"


def test_pull_refuses_dirty_and_diverged_states_and_push_never_forces(tmp_path: Path) -> None:
    root, database, workspace, remote = _remote(tmp_path)
    _publish(database, workspace)
    peer = _peer(tmp_path, remote)
    (peer / "incoming.txt").write_text("server\n")
    _git(peer, "add", "incoming.txt")
    _git(peer, "commit", "-m", "server")
    _git(peer, "push")
    (root / "local.txt").write_text("local\n")
    with connect_database(database) as connection:
        dirty = read_git_snapshot(connection, workspace)
        with pytest.raises(GitOperationError, match="Перед Pull"):
            run_git_operation(connection, GitOperationRequest("git_pull", workspace, dirty.token))
        _git(root, "add", "local.txt")
        _git(root, "commit", "-m", "local")
        head = _output(root, "rev-parse", "HEAD")
        for action in ("git_pull", "git_push"):
            state = read_git_snapshot(connection, workspace)
            with pytest.raises(GitOperationError) as error:
                run_git_operation(connection, GitOperationRequest(action, workspace, state.token))
            assert error.value.result is not None
            assert not error.value.result.success and "Код завершения:" in error.value.result.output
            assert _output(root, "rev-parse", "HEAD") == head
        assert read_git_snapshot(connection, workspace).operation is None
    assert _output(remote, "rev-parse", "published") == _output(peer, "rev-parse", "HEAD")


def test_changed_remote_config_rejects_stale_forms(tmp_path: Path) -> None:
    root, database, workspace, _remote_path = _remote(tmp_path)
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        _git(root, "remote", "set-url", "origin", str(tmp_path / "different.git"))
        with pytest.raises(GitOperationError, match="изменилось"):
            run_git_operation(
                connection,
                GitOperationRequest("git_publish", workspace, state.token, "published", "origin"),
            )


def test_network_commands_release_sqlite_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, database, workspace, _remote_path = _remote(tmp_path)
    original = operations._run
    calls = []

    def run(
        root: Path, arguments: list[str], deadline: float, *, console: list[str] | None = None
    ) -> bytes:
        command = arguments[2] if arguments[0] == "-c" else arguments[0]
        if command in {"push", "fetch", "pull"}:
            with connect_database(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ROLLBACK")
            calls.append(command)
        return original(root, arguments, deadline, console=console)

    monkeypatch.setattr(operations, "_run", run)
    _publish(database, workspace)
    assert calls == ["push"]


def test_console_redacts_credentials_and_timeout_preserves_partial_output(tmp_path: Path) -> None:
    value = operations._console_text(
        "https://user:secret@example.com/repo?token=private <script>\x1b[31m"
    )
    assert "secret" not in value and "private" not in value and "\x1b" not in value
    root, _database_path, _workspace = _database(tmp_path)
    _git(root, "config", "alias.slow", "!echo partial-output; sleep 20")
    console: list[str] = []
    with pytest.raises(GitOperationError, match="не завершил"):
        operations._run(root, ["slow"], monotonic() + 0.15, console=console)
    assert "partial-output" in "\n".join(console)


def test_fetch_ignores_mirror_refmaps_and_pull_rechecks_local_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database, workspace, remote = _remote(tmp_path)
    _publish(database, workspace)
    peer = _peer(tmp_path, remote)
    _git(peer, "branch", "other")
    _git(peer, "push", "origin", "other")
    _git(root, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    _git(root, "config", "fetch.prune", "true")
    _git(root, "config", "fetch.pruneTags", "true")
    head = _output(root, "rev-parse", "HEAD")
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        run_git_operation(
            connection, GitOperationRequest("git_fetch", workspace, state.token, "origin")
        )
        assert (
            _output(root, "for-each-ref", "--format=%(refname)", "refs/heads/") == "refs/heads/main"
        )
        assert _output(root, "rev-parse", "HEAD") == head
    original = operations._run

    def run(
        path: Path, arguments: list[str], deadline: float, *, console: list[str] | None = None
    ) -> bytes:
        result = original(path, arguments, deadline, console=console)
        if arguments[0] == "fetch":
            (root / "appeared-during-fetch.txt").write_text("keep working file\n")
        return result

    monkeypatch.setattr(operations, "_run", run)
    with connect_database(database) as connection:
        state = read_git_snapshot(connection, workspace)
        with pytest.raises(GitOperationError, match="во время Fetch"):
            run_git_operation(connection, GitOperationRequest("git_pull", workspace, state.token))
    assert _output(root, "rev-parse", "HEAD") == head
    assert (root / "appeared-during-fetch.txt").read_text() == "keep working file\n"
