from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import harness.cursor_adapter as cursor_module
from harness.cursor_adapter import CursorAdapter, discover_cursor_adapter
from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    workspace_hints_from_environment,
)
from harness.workspace_resolution import WorkspaceHintMatchMode


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "init",
    )
    return path.resolve()


def _entry(command: Path, *, project: bool = False) -> dict[str, object]:
    env = {"HARNESS_HOST_PROFILE": "cursor"}
    if project:
        env["HARNESS_WORKSPACE_ROOT"] = "${workspaceFolder}"
    return {
        "type": "stdio",
        "command": str(command),
        "args": ["-m", "harness.mcp_process"],
        "env": env,
    }


def test_cursor_global_registration_preserves_user_config_and_file_on_uninstall(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"theme": "user", "mcpServers": {"other": {"url": "https://example.invalid"}}}),
        encoding="utf-8",
    )
    adapter = CursorAdapter(home=home, python_executable=Path("/venv/bin/python"))

    assert adapter.registration_state() is HostRegistrationState.ABSENT
    assert adapter.register_mcp() is IntegrationChange.CHANGED
    value = json.loads(config.read_text(encoding="utf-8"))
    assert value["theme"] == "user"
    assert value["mcpServers"]["other"] == {"url": "https://example.invalid"}
    assert value["mcpServers"]["harness"] == _entry(Path("/venv/bin/python"))
    assert adapter.register_mcp() is IntegrationChange.UNCHANGED

    assert adapter.unregister_mcp() is IntegrationChange.CHANGED
    assert config.is_file()
    value = json.loads(config.read_text(encoding="utf-8"))
    assert value == {"theme": "user", "mcpServers": {"other": {"url": "https://example.invalid"}}}


def test_cursor_global_uninstall_preserves_preexisting_empty_config_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    adapter = CursorAdapter(home=home, python_executable=Path("/venv/bin/python"))

    assert adapter.register_mcp() is IntegrationChange.CHANGED
    assert adapter.unregister_mcp() is IntegrationChange.CHANGED
    assert json.loads(config.read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_cursor_global_registration_replaces_only_stale_owned_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "harness": _entry(Path("/old/python")),
                    "other": {"command": "other"},
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = CursorAdapter(home=home, python_executable=Path("/new/python"))

    assert adapter.registration_state() is HostRegistrationState.STALE_OWNED
    assert adapter.register_mcp() is IntegrationChange.CHANGED
    value = json.loads(config.read_text(encoding="utf-8"))
    assert value["mcpServers"]["harness"] == _entry(Path("/new/python"))
    assert value["mcpServers"]["other"] == {"command": "other"}


def test_cursor_refuses_foreign_same_name_registration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"harness": {"command": "/foreign"}}}),
        encoding="utf-8",
    )
    adapter = CursorAdapter(home=home, python_executable=Path("/python"))

    diagnostic = adapter.registration_diagnostic()
    assert diagnostic.state is HostRegistrationState.FOREIGN
    assert diagnostic.configured_python is None
    assert diagnostic.configured_workspace_root is None
    with pytest.raises(HostRegistrationCollisionError):
        adapter.register_mcp()
    with pytest.raises(HostRegistrationCollisionError):
        adapter.unregister_mcp()


def test_cursor_project_override_uses_workspace_folder_and_is_git_ignored(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    adapter = CursorAdapter(home=home, python_executable=Path("/venv/bin/python"))

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    config = root / ".cursor" / "mcp.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    assert value == {"mcpServers": {"harness": _entry(Path("/venv/bin/python"), project=True)}}
    assert adapter.project_registration_state(root) is HostRegistrationState.CURRENT
    ignored = _git(root, "check-ignore", "-q", ".cursor/mcp.json")
    assert ignored.returncode == 0
    assert _git(root, "status", "--porcelain").stdout == ""

    assert adapter.remove_project(root) is IntegrationChange.CHANGED
    assert not config.exists()
    assert not (root / ".cursor" / ".harness-mcp-owner.json").exists()
    assert _git(root, "status", "--porcelain").stdout == ""


def test_cursor_existing_untracked_project_config_preserves_other_servers(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"mcpServers": {"other": {"url": "https://example.invalid"}}}),
        encoding="utf-8",
    )
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    assert not (root / ".cursor" / ".harness-mcp-owner.json").exists()
    assert adapter.remove_project(root) is IntegrationChange.CHANGED
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "mcpServers": {"other": {"url": "https://example.invalid"}}
    }


def test_cursor_tracked_project_config_requires_manual_adoption(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _git(root, "add", ".cursor/mcp.json")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "tracked cursor config",
    )
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    with pytest.raises(HostIntegrationError, match="manual adoption"):
        adapter.reconcile_project(root)
    assert _git(root, "status", "--porcelain").stdout == ""


def test_cursor_project_diagnostic_exposes_stale_runtime_and_workspace_contract(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    stale = _entry(Path("/old/python"), project=True)
    stale["env"] = {
        "HARNESS_HOST_PROFILE": "cursor",
        "HARNESS_WORKSPACE_ROOT": "/wrong/root",
    }
    config.write_text(json.dumps({"mcpServers": {"harness": stale}}), encoding="utf-8")
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/new/python"))

    diagnostic = adapter.project_registration_diagnostic(root)

    assert diagnostic.path == config
    assert diagnostic.state is HostRegistrationState.STALE_OWNED
    assert diagnostic.expected_python == Path("/new/python")
    assert diagnostic.configured_python == "/old/python"
    assert diagnostic.configured_workspace_root == "/wrong/root"
    assert diagnostic.preflight_error is None


def test_cursor_project_diagnostic_reports_tracked_manual_adoption(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _git(root, "add", ".cursor/mcp.json")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "tracked cursor config",
    )
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    diagnostic = adapter.project_registration_diagnostic(root)

    assert diagnostic.state is HostRegistrationState.ABSENT
    assert diagnostic.preflight_error is not None
    assert "manual adoption" in diagnostic.preflight_error
    assert str(config) in diagnostic.preflight_error


def test_cursor_project_diagnostic_reports_malformed_ownership_marker(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    marker = root / ".cursor" / ".harness-mcp-owner.json"
    marker.write_text("not-json\n", encoding="utf-8")

    diagnostic = adapter.project_registration_diagnostic(root)

    assert diagnostic.state is HostRegistrationState.CURRENT
    assert diagnostic.preflight_error is not None
    assert "ownership marker is malformed" in diagnostic.preflight_error
    assert str(marker) in diagnostic.preflight_error


def test_cursor_tracked_exact_project_config_is_accepted_but_uninstall_is_manual(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"mcpServers": {"harness": _entry(Path("/python"), project=True)}}),
        encoding="utf-8",
    )
    _git(root, "add", ".cursor/mcp.json")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "manual adoption",
    )
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    assert adapter.reconcile_project(root) is IntegrationChange.UNCHANGED
    with pytest.raises(HostIntegrationError, match="manual removal"):
        adapter.remove_project(root)
    assert _git(root, "status", "--porcelain").stdout == ""


def test_cursor_linked_worktrees_keep_distinct_project_configs_with_shared_exclude(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked", str(linked))
    linked = linked.resolve()
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    assert adapter.reconcile_project(linked) is IntegrationChange.CHANGED
    assert adapter.project_registration_state(root) is HostRegistrationState.CURRENT
    assert adapter.project_registration_state(linked) is HostRegistrationState.CURRENT
    assert _git(root, "check-ignore", "-q", ".cursor/mcp.json").returncode == 0
    assert _git(linked, "check-ignore", "-q", ".cursor/mcp.json").returncode == 0

    assert adapter.remove_project(linked) is IntegrationChange.CHANGED
    assert adapter.project_registration_state(root) is HostRegistrationState.CURRENT
    assert adapter.remove_project(root) is IntegrationChange.CHANGED


def test_cursor_linked_worktree_exclude_ownership_transfers_when_owner_removed_first(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked-owner-transfer", str(linked))
    linked = linked.resolve()
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    assert adapter.reconcile_project(linked) is IntegrationChange.CHANGED
    root_marker = json.loads(
        (root / ".cursor" / ".harness-mcp-owner.json").read_text(encoding="utf-8")
    )
    linked_marker = json.loads(
        (linked / ".cursor" / ".harness-mcp-owner.json").read_text(encoding="utf-8")
    )
    assert root_marker["exclude_owned"] is True
    assert linked_marker["exclude_owned"] is False

    assert adapter.remove_project(root) is IntegrationChange.CHANGED
    linked_marker = json.loads(
        (linked / ".cursor" / ".harness-mcp-owner.json").read_text(encoding="utf-8")
    )
    assert linked_marker["exclude_owned"] is True
    assert _git(linked, "check-ignore", "-q", ".cursor/mcp.json").returncode == 0
    assert adapter.project_registration_state(linked) is HostRegistrationState.CURRENT

    assert adapter.remove_project(linked) is IntegrationChange.CHANGED
    exclude_path = Path(_git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    assert "BEGIN HARNESS CURSOR MCP" not in exclude_path.read_text(encoding="utf-8")


def test_cursor_workspace_hint_requires_exact_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    hints = workspace_hints_from_environment(
        environment={
            "HARNESS_HOST_PROFILE": "cursor",
            "HARNESS_WORKSPACE_ROOT": str(root),
        },
        cwd=elsewhere,
    )
    assert len(hints) == 1
    assert hints[0].path == root.resolve()
    assert hints[0].source == "cursor-workspace-folder"
    assert hints[0].match_mode is WorkspaceHintMatchMode.ROOT

    with pytest.raises(HostIntegrationError, match="HARNESS_WORKSPACE_ROOT"):
        workspace_hints_from_environment(
            environment={"HARNESS_HOST_PROFILE": "cursor"}, cwd=elsewhere
        )


def test_discover_cursor_adapter_uses_home_and_exact_python(tmp_path: Path) -> None:
    adapter = discover_cursor_adapter(
        environment={"HOME": str(tmp_path / "home")},
        python_executable=Path("relative/python"),
    )
    assert adapter.home == (tmp_path / "home").resolve()
    assert adapter.python_executable == Path(os.path.abspath("relative/python"))


def test_cursor_atomic_replacement_preserves_concurrent_target_and_prior_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    original = b'{"mcpServers": {}, "owner": "user"}\n'
    config.write_bytes(original)
    adapter = CursorAdapter(home=home, python_executable=Path("/python"))
    real_move = cursor_module._move_if_absent
    injected = False

    def race_move(source: Path, target: Path) -> bool:
        nonlocal injected
        if target == config and source.name.startswith(".harness-cursor-") and not injected:
            injected = True
            config.write_text(
                json.dumps({"mcpServers": {"harness": {"command": "/foreign"}}}),
                encoding="utf-8",
            )
            return False
        return real_move(source, target)

    monkeypatch.setattr(cursor_module, "_move_if_absent", race_move)
    with pytest.raises(HostRegistrationCollisionError, match="appeared during mutation"):
        adapter.register_mcp()

    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["harness"] == {
        "command": "/foreign"
    }
    backups = list(config.parent.glob(".harness-cursor-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def _isolated_entry() -> dict[str, object]:
    return {
        "type": "stdio",
        "command": "${workspaceFolder}/scripts/dev",
        "args": ["harness", "mcp"],
        "env": {"HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"},
    }


def test_cursor_isolated_development_overlay_is_left_unchanged(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"mcpServers": {"harness": _isolated_entry()}}) + "\n", encoding="utf-8"
    )
    _git(root, "add", ".cursor/mcp.json")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "isolated overlay",
    )
    original = config.read_text(encoding="utf-8")
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    diagnostic = adapter.project_registration_diagnostic(root)
    assert diagnostic.isolated_development is True
    assert diagnostic.preflight_error is None
    assert diagnostic.state is HostRegistrationState.FOREIGN
    assert adapter.reconcile_project(root) is IntegrationChange.UNCHANGED
    assert adapter.remove_project(root) is IntegrationChange.UNCHANGED
    assert config.read_text(encoding="utf-8") == original
    assert _git(root, "status", "--porcelain").stdout == ""


def test_cursor_foreign_non_isolation_entry_still_collides(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    config = root / ".cursor" / "mcp.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps({"mcpServers": {"harness": {"command": "/foreign"}}}),
        encoding="utf-8",
    )
    adapter = CursorAdapter(home=tmp_path / "home", python_executable=Path("/python"))

    with pytest.raises(HostRegistrationCollisionError, match="non-Harness"):
        adapter.reconcile_project(root)
    diagnostic = adapter.project_registration_diagnostic(root)
    assert diagnostic.isolated_development is False
    assert diagnostic.preflight_error is not None
