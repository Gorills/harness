from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from harness.daemon import serve_daemon
from harness.hidden_policy import HIDDEN_INSTRUCTION_BODY
from harness.hidden_projection import (
    CLAUDE_HIDDEN_RULE_RELATIVE,
    CURSOR_HIDDEN_MARKER_RELATIVE,
    CURSOR_HIDDEN_RULE_RELATIVE,
    HiddenProjectionCollisionError,
    HiddenProjectionError,
    apply_hidden_projection,
    remove_hidden_projection,
)
from harness.ipc import (
    IpcError,
    request_set_visibility,
    request_status,
    request_workspace_status,
)
from harness.registry import VisibilityMode, create_project, get_project, register_workspace
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database
from harness.visibility import set_project_visibility
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX Hidden projection slice")


def _git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=cwd, check=check, capture_output=True)


def _make_repo(path: Path, files: dict[str, str] | None = None) -> None:
    path.mkdir()
    for relative, content in (files or {"README.md": "repo\n"}).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "init", "-b", "main")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )


def _write_profiles(database: Path, *profiles: str) -> None:
    path = database.parent / "host-integrations.json"
    path.write_text(
        json.dumps({"version": 1, "profiles": list(profiles)}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _exclude_path(root: Path) -> Path:
    raw = _git(root, "rev-parse", "--git-path", "info/exclude").stdout.decode().strip()
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _start_daemon(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop_event = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop_event)
    deadline = time.monotonic() + 3
    while True:
        if future.done():
            future.result()
        try:
            if request_status(socket_path).schema_version == SCHEMA_VERSION:
                return stop_event, executor, future
        except IpcError:
            pass
        if time.monotonic() >= deadline:
            stop_event.set()
            executor.shutdown(wait=True)
            raise AssertionError("daemon did not become ready")
        time.sleep(0.01)


def _stop_daemon(stop_event: Event, executor: ThreadPoolExecutor, future: Future[None]) -> None:
    stop_event.set()
    executor.shutdown(wait=True)
    future.result()


def test_hidden_projection_is_untracked_ignored_and_leaves_gitignore(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {".gitignore": "user-cache\n", "README.md": "repo\n"})
    original_gitignore = (root / ".gitignore").read_bytes()
    original_exclude = _exclude_path(root).read_bytes()

    first = apply_hidden_projection((root,), ("cursor",))
    second = apply_hidden_projection((root,), ("cursor",))

    rule = root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()
    marker = root / CURSOR_HIDDEN_MARKER_RELATIVE.as_posix()
    assert first.materialized == 1
    assert second.materialized == 0
    assert second.unchanged == 1
    assert rule.is_file()
    assert marker.is_file()
    assert "alwaysApply: true" in rule.read_text(encoding="utf-8")
    assert (root / ".gitignore").read_bytes() == original_gitignore
    assert _exclude_path(root).read_bytes() != original_exclude
    assert _git(root, "check-ignore", "-q", CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).returncode == 0
    assert (
        _git(
            root,
            "ls-files",
            "--error-unmatch",
            "--",
            CURSOR_HIDDEN_RULE_RELATIVE.as_posix(),
            check=False,
        ).returncode
        == 1
    )
    assert (
        _git(
            root,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            CURSOR_HIDDEN_RULE_RELATIVE.as_posix(),
            CURSOR_HIDDEN_MARKER_RELATIVE.as_posix(),
        ).stdout
        == b""
    )


def test_hidden_projection_refuses_user_owned_and_tracked_targets(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    user_rule = root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()
    user_rule.parent.mkdir(parents=True)
    user_rule.write_text("# user rule\n", encoding="utf-8")

    with pytest.raises(HiddenProjectionCollisionError, match="user-owned"):
        apply_hidden_projection((root,), ("cursor",))
    assert user_rule.read_text(encoding="utf-8") == "# user rule\n"

    tracked = tmp_path / "tracked"
    _make_repo(
        tracked,
        {CURSOR_HIDDEN_RULE_RELATIVE.as_posix(): "# tracked\n", "README.md": "repo\n"},
    )
    with pytest.raises(HiddenProjectionCollisionError, match="Git-tracked"):
        apply_hidden_projection((tracked,), ("cursor",))


def test_hidden_projection_uses_shared_exclude_for_linked_worktree(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _make_repo(primary, {"README.md": "repo\n"})
    worktree = tmp_path / "worktree"
    _git(primary, "worktree", "add", "-b", "feature", str(worktree))
    original = _exclude_path(worktree).read_bytes()

    apply_hidden_projection((primary, worktree), ("cursor",))

    assert (primary / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).is_file()
    assert (worktree / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).is_file()
    assert _exclude_path(primary) == _exclude_path(worktree)
    assert (
        _git(worktree, "check-ignore", "-q", CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).returncode == 0
    )
    assert original != _exclude_path(worktree).read_bytes()


def test_remove_hidden_projection_preserves_foreign_exclude_bytes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    exclude = _exclude_path(root)
    user_exclude = exclude.read_bytes() + b"# keep-user-scratch\n/local-scratch\n"
    exclude.write_bytes(user_exclude)

    apply_hidden_projection((root,), ("cursor",))
    after_apply = exclude.read_bytes()
    assert b"# keep-user-scratch\n" in after_apply
    assert b"/local-scratch\n" in after_apply
    assert CURSOR_HIDDEN_RULE_RELATIVE.as_posix().encode() in after_apply

    remove_hidden_projection((root,), ("cursor",))
    assert exclude.read_bytes() == user_exclude


def test_force_add_still_stages_ignored_hidden_rule(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    apply_hidden_projection((root,), ("cursor",))
    _git(root, "add", "-f", CURSOR_HIDDEN_RULE_RELATIVE.as_posix())
    listed = _git(root, "ls-files", "--", CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).stdout
    assert CURSOR_HIDDEN_RULE_RELATIVE.as_posix().encode() in listed


def test_set_visibility_projects_then_persists_and_restores(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {".gitignore": "keep\n", "README.md": "repo\n"})
    original_gitignore = (root / ".gitignore").read_bytes()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=root)
        hidden = set_project_visibility(
            connection,
            mode=VisibilityMode.HIDDEN,
            host_profiles=("cursor", "claude-code"),
            project_id=project.project_id,
        )
        assert hidden.project.visibility_mode is VisibilityMode.HIDDEN
        assert get_project(connection, project.project_id).visibility_mode is VisibilityMode.HIDDEN
        assert (root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).is_file()
        assert (root / CLAUDE_HIDDEN_RULE_RELATIVE.as_posix()).is_file()
        assert (root / ".gitignore").read_bytes() == original_gitignore

        restored = set_project_visibility(
            connection,
            mode=VisibilityMode.NORMAL,
            host_profiles=("cursor", "claude-code"),
            project_id=project.project_id,
        )
        assert restored.project.visibility_mode is VisibilityMode.NORMAL
        assert not (root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).exists()
        assert not (root / CLAUDE_HIDDEN_RULE_RELATIVE.as_posix()).exists()
        assert (root / ".gitignore").read_bytes() == original_gitignore
    finally:
        connection.close()


def test_collision_does_not_change_visibility_mode(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    user_rule = root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()
    user_rule.parent.mkdir(parents=True)
    user_rule.write_text("mine\n", encoding="utf-8")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=root)
        with pytest.raises(HiddenProjectionCollisionError):
            set_project_visibility(
                connection,
                mode=VisibilityMode.HIDDEN,
                host_profiles=("cursor",),
                project_id=project.project_id,
            )
        assert get_project(connection, project.project_id).visibility_mode is VisibilityMode.NORMAL
    finally:
        connection.close()


def test_codex_hidden_uses_project_developer_instructions_without_overwriting_agents_md(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"AGENTS.md": "# User-owned instructions\n", "README.md": "repo\n"})
    agents_before = (root / "AGENTS.md").read_bytes()
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=root)
        hidden = set_project_visibility(
            connection,
            mode=VisibilityMode.HIDDEN,
            host_profiles=("codex",),
            project_id=project.project_id,
        )
        assert hidden.project.visibility_mode is VisibilityMode.HIDDEN
        assert (root / "AGENTS.md").read_bytes() == agents_before
        config_path = root / ".codex" / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert config["developer_instructions"] == HIDDEN_INSTRUCTION_BODY
        assert _git(root, "check-ignore", "-q", ".codex/config.toml").returncode == 0

        normal = set_project_visibility(
            connection,
            mode=VisibilityMode.NORMAL,
            host_profiles=("codex",),
            project_id=project.project_id,
        )
        assert normal.project.visibility_mode is VisibilityMode.NORMAL
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert "developer_instructions" not in config
        assert (root / "AGENTS.md").read_bytes() == agents_before
    finally:
        connection.close()


def test_codex_hidden_refuses_user_config_without_exact_developer_instructions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    config = root / ".codex" / "config.toml"
    config.parent.mkdir()
    original = '[mcp_servers.harness]\ncommand = "/manual/python"\n'
    config.write_text(original, encoding="utf-8")
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=root)
        with pytest.raises(HiddenProjectionError, match="Codex Hidden developer instructions"):
            set_project_visibility(
                connection,
                mode=VisibilityMode.HIDDEN,
                host_profiles=("codex",),
                project_id=project.project_id,
            )
        assert get_project(connection, project.project_id).visibility_mode is VisibilityMode.NORMAL
        assert config.read_text(encoding="utf-8") == original
    finally:
        connection.close()


def test_hidden_does_not_rewrite_isolated_overlay_mcp(tmp_path: Path) -> None:
    overlay = {
        "mcpServers": {
            "harness-dev": {
                "command": "${workspaceFolder}/scripts/dev",
                "args": ["harness", "mcp"],
                "env": {"HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"},
            }
        }
    }
    root = tmp_path / "overlay"
    _make_repo(
        root,
        {
            ".cursor/mcp.json": json.dumps(overlay, indent=2) + "\n",
            ".cursor/rules/isolated-development.mdc": "---\nalwaysApply: true\n---\n# isolated\n",
            "README.md": "overlay\n",
        },
    )
    mcp_before = (root / ".cursor" / "mcp.json").read_bytes()
    isolated_before = (root / ".cursor" / "rules" / "isolated-development.mdc").read_bytes()
    apply_hidden_projection((root,), ("cursor",))
    assert (root / ".cursor" / "mcp.json").read_bytes() == mcp_before
    assert (root / ".cursor" / "rules" / "isolated-development.mdc").read_bytes() == isolated_before
    assert (root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).is_file()


def test_visibility_ipc_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {".gitignore": "cache\n", "README.md": "repo\n"})
    original_gitignore = (root / ".gitignore").read_bytes()
    database = tmp_path / "harness.db"
    socket_path = tmp_path / "ipc" / "harness.sock"
    socket_path.parent.mkdir(mode=0o700)
    socket_path.parent.chmod(0o700)
    initialize_database(database)
    connection = connect_database(database)
    try:
        project = create_project(connection)
        register_workspace(connection, project_id=project.project_id, path=root)
    finally:
        connection.close()
    _write_profiles(database, "cursor")

    stop_event, executor, future = _start_daemon(database, socket_path)
    try:
        hidden = request_set_visibility(socket_path, root, "hidden")
        assert hidden.visibility_mode == "hidden"
        assert hidden.scm_write_enforcement == "unsupported"
        status = request_workspace_status(
            socket_path,
            [
                WorkspaceHint(
                    path=root,
                    source="test",
                    match_mode=WorkspaceHintMatchMode.ROOT,
                )
            ],
        )
        assert status.visibility_mode == "hidden"
        assert not hasattr(status, "scm_write_enforcement")
        assert "unsupported" not in str(status)
        assert (root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).is_file()
        assert (root / ".gitignore").read_bytes() == original_gitignore

        restored = request_set_visibility(socket_path, root, "normal")
        assert restored.visibility_mode == "normal"
        assert not (root / CURSOR_HIDDEN_RULE_RELATIVE.as_posix()).exists()
    finally:
        _stop_daemon(stop_event, executor, future)
