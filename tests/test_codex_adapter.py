from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

import harness.codex_adapter as codex_module
from harness.codex_adapter import (
    CODEX_BOOTSTRAP_INSTRUCTION_BODY,
    CODEX_MCP_FORWARD_ENV_VARS,
    CODEX_MCP_MISSING_WORKSPACE_ROOT_MESSAGE,
    CodexAdapter,
    codex_profile_missing_workspace_root,
    discover_codex_adapter,
)
from harness.hidden_policy import HIDDEN_INSTRUCTION_BODY
from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    workspace_hints_from_environment,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    return path.resolve()


def _adapter(python: str = "/venv/bin/python") -> CodexAdapter:
    return CodexAdapter(Path("/usr/bin/codex"), Path(python))


def _config(root: Path) -> Path:
    return root / ".codex" / "config.toml"


def _marker(root: Path) -> Path:
    return root / ".codex" / ".harness-mcp-owner.json"


def _exclude(root: Path) -> Path:
    result = _git(root, "rev-parse", "--git-path", "info/exclude")
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else root / path


def test_codex_project_reconcile_creates_exact_owned_config_and_git_excludes(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()

    assert adapter.project_registration_state(root) is HostRegistrationState.ABSENT
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    assert adapter.reconcile_project(root) is IntegrationChange.UNCHANGED

    value = tomllib.loads(_config(root).read_text(encoding="utf-8"))
    instructions = value.pop("developer_instructions")
    assert "project_status" in instructions
    assert "deferred" in instructions
    assert "initial visible tool list" in instructions
    assert HIDDEN_INSTRUCTION_BODY not in instructions
    assert value == {
        "mcp_servers": {
            "harness": {
                "command": "/venv/bin/python",
                "args": ["-m", "harness.mcp_process"],
                "env_vars": list(CODEX_MCP_FORWARD_ENV_VARS),
                "cwd": str(root),
                "env": {
                    "HARNESS_HOST_PROFILE": "codex",
                    "HARNESS_WORKSPACE_ROOT": str(root),
                },
            }
        }
    }
    assert json.loads(_marker(root).read_text(encoding="utf-8")) == {
        "version": 1,
        "workspace_root": str(root),
    }
    assert adapter.project_registration_state(root) is HostRegistrationState.CURRENT
    assert os.stat(_config(root)).st_mode & 0o777 == 0o600
    assert os.stat(_marker(root)).st_mode & 0o777 == 0o600
    exclude = _exclude(root).read_text(encoding="utf-8")
    assert "# BEGIN HARNESS CODEX MCP" in exclude
    assert "/.codex/config.toml" in exclude
    assert "/.codex/.harness-mcp-owner.json" in exclude
    assert _git(root, "status", "--porcelain", "--untracked-files=all").stdout == ""


def test_codex_bootstrap_is_small_and_front_loads_deferred_tool_discovery() -> None:
    assert len(CODEX_BOOTSTRAP_INSTRUCTION_BODY.encode("utf-8")) < 1024
    first_512 = CODEX_BOOTSTRAP_INSTRUCTION_BODY[:512]
    assert "project_status" in first_512
    assert "deferred" in first_512
    assert "initial visible tool list" in first_512


def test_codex_owned_config_reconciles_hidden_developer_instructions(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    agents = root / "AGENTS.md"
    agents.write_text("# User project instructions\n", encoding="utf-8")
    adapter = _adapter()

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    normal_instructions = tomllib.loads(_config(root).read_text(encoding="utf-8"))[
        "developer_instructions"
    ]
    assert adapter.reconcile_project(root, hidden=True) is IntegrationChange.CHANGED
    hidden = tomllib.loads(_config(root).read_text(encoding="utf-8"))
    assert hidden["developer_instructions"].startswith(normal_instructions)
    assert hidden["developer_instructions"].endswith(HIDDEN_INSTRUCTION_BODY)
    assert adapter.project_registration_state(root, hidden=True) is HostRegistrationState.CURRENT
    assert adapter.project_registration_state(root) is HostRegistrationState.STALE_OWNED
    assert agents.read_text(encoding="utf-8") == "# User project instructions\n"

    assert adapter.reconcile_project(root, hidden=False) is IntegrationChange.CHANGED
    normal = tomllib.loads(_config(root).read_text(encoding="utf-8"))
    assert normal["developer_instructions"] == normal_instructions
    assert adapter.project_registration_state(root) is HostRegistrationState.CURRENT
    assert agents.read_text(encoding="utf-8") == "# User project instructions\n"


def test_codex_project_reconcile_updates_only_marker_owned_config(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    old = _adapter("/old/python")
    current = _adapter("/new/python")
    assert old.reconcile_project(root) is IntegrationChange.CHANGED

    diagnostic = current.project_registration_diagnostic(root)
    assert diagnostic.state is HostRegistrationState.STALE_OWNED
    assert diagnostic.configured_python == "/old/python"
    assert diagnostic.harness_owned
    assert diagnostic.preflight_error is None

    assert current.reconcile_project(root) is IntegrationChange.CHANGED
    assert (
        tomllib.loads(_config(root).read_text(encoding="utf-8"))["mcp_servers"]["harness"][
            "command"
        ]
        == "/new/python"
    )


@pytest.mark.parametrize("hidden", [False, True])
def test_codex_project_reconcile_migrates_exact_legacy_owned_config(
    tmp_path: Path,
    hidden: bool,
) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()
    assert adapter.reconcile_project(root, hidden=hidden) is IntegrationChange.CHANGED
    path = _config(root)
    legacy = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("env_vars = ")
    )
    path.write_text(legacy + "\n", encoding="utf-8")

    diagnostic = adapter.project_registration_diagnostic(root, hidden=hidden)
    assert diagnostic.state is HostRegistrationState.STALE_OWNED
    assert diagnostic.preflight_error is None

    assert adapter.reconcile_project(root, hidden=hidden) is IntegrationChange.CHANGED
    entry = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["harness"]
    assert entry["env_vars"] == list(CODEX_MCP_FORWARD_ENV_VARS)
    instructions = tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]
    assert instructions.startswith(CODEX_BOOTSTRAP_INSTRUCTION_BODY)


@pytest.mark.parametrize("hidden", [False, True])
def test_codex_project_reconcile_migrates_legacy_instruction_shape(
    tmp_path: Path,
    hidden: bool,
) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()
    assert adapter.reconcile_project(root, hidden=hidden) is IntegrationChange.CHANGED
    path = _config(root)
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("developer_instructions = ")
    ]
    if hidden:
        lines.insert(1, f"developer_instructions = {json.dumps(HIDDEN_INSTRUCTION_BODY)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    diagnostic = adapter.project_registration_diagnostic(root, hidden=hidden)
    assert diagnostic.state is HostRegistrationState.STALE_OWNED
    assert diagnostic.preflight_error is None
    assert adapter.reconcile_project(root, hidden=hidden) is IntegrationChange.CHANGED
    instructions = tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]
    assert instructions.startswith(CODEX_BOOTSTRAP_INSTRUCTION_BODY)
    assert (HIDDEN_INSTRUCTION_BODY in instructions) is hidden


def test_codex_project_reconcile_heals_missing_owned_exclude_block(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    exclude = _exclude(root)
    exclude.write_text("/keep-me\n", encoding="utf-8")

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    content = exclude.read_text(encoding="utf-8")
    assert content.startswith("/keep-me\n")
    assert content.count("# BEGIN HARNESS CODEX MCP") == 1
    assert adapter.reconcile_project(root) is IntegrationChange.UNCHANGED


def test_codex_atomic_replacement_preserves_concurrent_target_and_prior_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path / "repo")
    old = _adapter("/old/python")
    current = _adapter("/new/python")
    assert old.reconcile_project(root) is IntegrationChange.CHANGED
    config = _config(root)
    original = config.read_bytes()
    real_move = codex_module._move_if_absent
    injected = False

    def race_move(source: Path, target: Path) -> bool:
        nonlocal injected
        if target == config and source.name.startswith(".harness-codex-") and not injected:
            injected = True
            config.write_text(
                '[mcp_servers.harness]\ncommand = "/foreign"\n',
                encoding="utf-8",
            )
            return False
        return real_move(source, target)

    monkeypatch.setattr(codex_module, "_move_if_absent", race_move)
    with pytest.raises(HostRegistrationCollisionError, match="appeared during mutation"):
        current.reconcile_project(root)

    assert tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["harness"] == {
        "command": "/foreign"
    }
    backups = list(config.parent.glob(".harness-codex-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_codex_existing_user_config_requires_manual_adoption_without_mutation(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    path = _config(root)
    path.parent.mkdir()
    original = b'model = "gpt-5"\n'
    path.write_bytes(original)

    diagnostic = _adapter().project_registration_diagnostic(root)
    assert diagnostic.state is HostRegistrationState.ABSENT
    assert diagnostic.preflight_error is not None
    assert "user-owned" in diagnostic.preflight_error
    with pytest.raises(HostIntegrationError, match="user-owned"):
        _adapter().reconcile_project(root)
    assert path.read_bytes() == original
    assert not _marker(root).exists()


def test_codex_foreign_same_name_fails_closed_without_mutation(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    path = _config(root)
    path.parent.mkdir()
    original = b'[mcp_servers.harness]\ncommand = "foreign"\n'
    path.write_bytes(original)

    diagnostic = _adapter().project_registration_diagnostic(root)
    assert diagnostic.state is HostRegistrationState.FOREIGN
    with pytest.raises(HostRegistrationCollisionError, match="non-Harness"):
        _adapter().reconcile_project(root)
    assert path.read_bytes() == original


def test_codex_exact_tracked_manual_config_is_adopted_but_never_removed(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    generated = _adapter()
    assert generated.reconcile_project(root) is IntegrationChange.CHANGED
    marker = _marker(root)
    marker.unlink()
    _exclude(root).write_text("", encoding="utf-8")
    _git(root, "add", ".codex/config.toml")

    diagnostic = generated.project_registration_diagnostic(root)
    assert diagnostic.state is HostRegistrationState.CURRENT
    assert not diagnostic.harness_owned
    assert diagnostic.preflight_error is None
    assert generated.reconcile_project(root) is IntegrationChange.UNCHANGED
    assert generated.remove_project(root) is IntegrationChange.UNCHANGED
    assert _config(root).is_file()


def test_codex_tracked_config_without_exact_entry_is_never_modified(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    path = _config(root)
    path.parent.mkdir()
    path.write_text('model = "gpt-5"\n', encoding="utf-8")
    _git(root, "add", ".codex/config.toml")

    with pytest.raises(HostIntegrationError, match=r"tracked \.codex/config\.toml"):
        _adapter().reconcile_project(root)
    assert path.read_text(encoding="utf-8") == 'model = "gpt-5"\n'


def test_codex_tracked_manual_entry_without_bootstrap_is_not_adopted(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    path = _config(root)
    without_bootstrap = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("developer_instructions = ")
    )
    path.write_text(without_bootstrap + "\n", encoding="utf-8")
    _marker(root).unlink()
    _exclude(root).write_text("", encoding="utf-8")
    _git(root, "add", ".codex/config.toml")

    diagnostic = adapter.project_registration_diagnostic(root)
    assert diagnostic.state is HostRegistrationState.FOREIGN
    assert diagnostic.preflight_error is not None
    assert "bootstrap developer instructions" in diagnostic.preflight_error
    with pytest.raises(HostIntegrationError, match="bootstrap developer instructions"):
        adapter.reconcile_project(root)


def test_codex_owned_config_refuses_unknown_user_content(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    path = _config(root)
    path.write_bytes(path.read_bytes() + b'model = "gpt-5"\n')

    diagnostic = adapter.project_registration_diagnostic(root)
    assert diagnostic.state is HostRegistrationState.FOREIGN
    assert diagnostic.preflight_error is not None
    assert "unknown user content" in diagnostic.preflight_error
    with pytest.raises(HostIntegrationError, match="unknown user content"):
        adapter.reconcile_project(root)
    with pytest.raises(HostIntegrationError, match="unknown user content"):
        adapter.remove_project(root)


@pytest.mark.parametrize("kind", ["malformed", "symlink"])
def test_codex_malformed_or_symlink_user_config_fails_without_rewrite(
    tmp_path: Path, kind: str
) -> None:
    root = _repository(tmp_path / "repo")
    path = _config(root)
    path.parent.mkdir()
    if kind == "malformed":
        path.write_bytes(b"[mcp_servers.harness\n")
    else:
        target = tmp_path / "user-config.toml"
        target.write_text('model = "gpt-5"\n', encoding="utf-8")
        path.symlink_to(target)

    with pytest.raises(HostIntegrationError):
        _adapter().project_registration_diagnostic(root)
    if kind == "malformed":
        assert path.read_bytes() == b"[mcp_servers.harness\n"
    else:
        assert path.is_symlink()
        assert path.readlink() == target
        assert target.read_text(encoding="utf-8") == 'model = "gpt-5"\n'


def test_codex_remove_owned_project_preserves_unrelated_exclude_content(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    exclude = _exclude(root)
    exclude.write_text("/keep-me\n", encoding="utf-8")
    adapter = _adapter()
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED

    assert adapter.remove_project(root) is IntegrationChange.CHANGED
    assert adapter.remove_project(root) is IntegrationChange.UNCHANGED
    assert not _config(root).exists()
    assert not _marker(root).exists()
    assert exclude.read_text(encoding="utf-8") == "/keep-me\n"


def test_codex_linked_worktrees_keep_distinct_roots_and_shared_exclude_until_last_remove(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path / "repo")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(
        root,
        "-c",
        "user.name=Harness Tests",
        "-c",
        "user.email=harness@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    linked = (tmp_path / "linked").resolve()
    _git(root, "worktree", "add", "-q", "-b", "linked", str(linked))
    adapter = _adapter()

    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    assert adapter.reconcile_project(linked) is IntegrationChange.CHANGED
    root_entry = tomllib.loads(_config(root).read_text(encoding="utf-8"))["mcp_servers"]["harness"]
    linked_entry = tomllib.loads(_config(linked).read_text(encoding="utf-8"))["mcp_servers"][
        "harness"
    ]
    assert root_entry["cwd"] == str(root)
    assert linked_entry["cwd"] == str(linked)
    assert root_entry["env"]["HARNESS_WORKSPACE_ROOT"] == str(root)
    assert linked_entry["env"]["HARNESS_WORKSPACE_ROOT"] == str(linked)
    exclude = _exclude(root)
    assert exclude.read_text(encoding="utf-8").count("# BEGIN HARNESS CODEX MCP") == 1

    assert adapter.remove_project(root) is IntegrationChange.CHANGED
    assert "# BEGIN HARNESS CODEX MCP" in exclude.read_text(encoding="utf-8")
    assert adapter.remove_project(linked) is IntegrationChange.CHANGED
    assert "# BEGIN HARNESS CODEX MCP" not in exclude.read_text(encoding="utf-8")


def test_codex_workspace_hints_require_explicit_project_root(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    adapter = _adapter()
    hints = adapter.workspace_hints({"HARNESS_WORKSPACE_ROOT": str(root)})
    assert [(hint.path, hint.source, hint.match_mode.value) for hint in hints] == [
        (root, "codex-project-config-root", "root")
    ]
    with pytest.raises(HostIntegrationError) as raised:
        adapter.workspace_hints({})
    assert str(raised.value) == CODEX_MCP_MISSING_WORKSPACE_ROOT_MESSAGE
    assert codex_profile_missing_workspace_root({"HARNESS_HOST_PROFILE": "codex"})
    assert not codex_profile_missing_workspace_root(
        {"HARNESS_HOST_PROFILE": "codex", "HARNESS_WORKSPACE_ROOT": str(root)}
    )
    generic = workspace_hints_from_environment(
        environment={
            "HARNESS_HOST_PROFILE": "codex",
            "HARNESS_WORKSPACE_ROOT": str(root),
        },
        cwd=tmp_path,
    )
    assert [(hint.path, hint.source, hint.match_mode.value) for hint in generic] == [
        (root, "codex-project-config-root", "root")
    ]


def test_discover_codex_adapter_uses_path_and_selected_python(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    adapter = discover_codex_adapter(
        environment={"PATH": str(executable.parent)},
        python_executable=Path("relative/python"),
    )
    assert adapter is not None
    assert adapter.executable == executable.resolve()
    assert adapter.python_executable == Path(os.path.abspath("relative/python"))
    assert adapter.profile == "codex"
    assert adapter.skill_projection_surface().profile == "codex"
    assert discover_codex_adapter(environment={"PATH": str(tmp_path / "missing")}) is None


def test_installed_codex_cli_loads_generated_trusted_project_mcp(tmp_path: Path) -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    root = _repository(tmp_path / "repo")
    adapter = CodexAdapter(Path(codex), Path("/venv/bin/python"))
    assert adapter.reconcile_project(root) is IntegrationChange.CHANGED
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)
    environment["HOME"] = str(tmp_path / "home")

    untrusted = subprocess.run(
        [codex, "mcp", "get", "harness", "--json"],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert untrusted.returncode != 0

    (codex_home / "config.toml").write_text(
        f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [codex, "mcp", "get", "harness", "--json"],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed["name"] == "harness"
    assert observed["enabled"] is True
    assert observed["disabled_reason"] is None
    assert observed["transport"] == {
        "type": "stdio",
        "command": "/venv/bin/python",
        "args": ["-m", "harness.mcp_process"],
        "env": {
            "HARNESS_HOST_PROFILE": "codex",
            "HARNESS_WORKSPACE_ROOT": str(root),
        },
        "env_vars": list(CODEX_MCP_FORWARD_ENV_VARS),
        "cwd": str(root),
    }
