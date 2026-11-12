from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from harness.builtin_skills import sync_builtin_skills
from harness.codex_servers_adapter import GlobalServersSetupError, setup_global_servers
from harness.skills import DetectedProjectStack, load_skill_registry, resolve_skills


def test_global_delivery_preserves_unknown_config_and_is_idempotent(tmp_path: Path) -> None:
    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    original = b'# operator preference\nmodel = "chosen"\n[mcp_servers.other]\nurl = "http://example.invalid/mcp"\n'
    (codex / "config.toml").write_bytes(original)
    url = "http://127.0.0.1:12345/servers/mcp"
    setup_global_servers(codex, home, url, "SYNTHETIC_CAPABILITY")
    first = (codex / "config.toml").read_bytes()
    setup_global_servers(codex, home, url, "SYNTHETIC_CAPABILITY")
    assert first == (codex / "config.toml").read_bytes()
    assert first.startswith(original)
    parsed = tomllib.loads(first.decode())
    assert (
        "X-Harness-Workspace-Root" not in parsed["mcp_servers"]["harness-servers"]["http_headers"]
    )
    marker = json.loads((codex / ".harness-servers-owner.json").read_bytes())
    assert "SYNTHETIC_CAPABILITY" not in json.dumps(marker)
    assert (home / ".agents/skills/server-operations/SKILL.md").exists()
    setup_global_servers(codex, home, url, "ROTATED")
    assert b"SYNTHETIC_CAPABILITY" not in (codex / "config.toml").read_bytes()
    setup_global_servers(codex, home, url, "ROTATED", remove=True)
    assert (codex / "config.toml").read_bytes() == original
    assert not (home / ".agents/skills/server-operations").exists()


@pytest.mark.parametrize("collision", ["mcp", "skill", "symlink", "edited"])
def test_global_delivery_refuses_unknown_or_edited_ownership(
    tmp_path: Path, collision: str
) -> None:
    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    url = "http://127.0.0.1:12345/servers/mcp"
    if collision == "mcp":
        (codex / "config.toml").write_text(
            '[mcp_servers.harness-servers]\nurl="http://user.invalid"\n'
        )
    elif collision == "skill":
        target = home / ".agents/skills/server-operations"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("user content")
    elif collision == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("user")
        (codex / "config.toml").symlink_to(outside)
    else:
        setup_global_servers(codex, home, url, "FIRST")
        target = home / ".agents/skills/server-operations/SKILL.md"
        target.write_text(target.read_text() + "operator change")
    before = (codex / "config.toml").read_bytes() if (codex / "config.toml").exists() else None
    with pytest.raises(GlobalServersSetupError):
        setup_global_servers(codex, home, url, "SECOND")
    assert (
        (codex / "config.toml").read_bytes() if (codex / "config.toml").exists() else None
    ) == before


def test_global_server_skill_is_not_automatically_projected(tmp_path: Path) -> None:
    sync_builtin_skills(tmp_path / "registry")
    registry = load_skill_registry(tmp_path / "registry")
    stack = DetectedProjectStack(
        frozenset({"python"}),
        frozenset(),
        frozenset({"pyproject.toml"}),
        frozenset({"software-project", "deployment-ops"}),
    )
    assert "server-operations" not in {
        s.definition.skill_id for s in resolve_skills(registry, stack)
    }


def test_global_setup_write_failure_restores_prior_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.codex_servers_adapter as adapter

    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    original = b'model="operator"\n'
    (codex / "config.toml").write_bytes(original)
    atomic = adapter._atomic

    def fail_config(path: Path, data: bytes, expected: bytes | None, skill_file: Path) -> None:
        if path.name == "config.toml" and data != original:
            raise OSError("synthetic disk failure")
        atomic(path, data, expected, skill_file)

    monkeypatch.setattr(adapter, "_atomic", fail_config)
    with pytest.raises(GlobalServersSetupError):
        setup_global_servers(codex, home, "http://127.0.0.1:12345/servers/mcp", "SYNTHETIC")
    assert (codex / "config.toml").read_bytes() == original
    assert not (home / ".agents/skills/server-operations").exists()
    assert not (codex / ".harness-servers-owner.json").exists()


def test_global_setup_removal_restores_config_without_final_newline(tmp_path: Path) -> None:
    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    original = b'model="operator"'
    (codex / "config.toml").write_bytes(original)
    url = "http://127.0.0.1:12345/servers/mcp"
    setup_global_servers(codex, home, url, "SYNTHETIC")
    setup_global_servers(codex, home, url, "ROTATED")
    setup_global_servers(codex, home, url, "ROTATED", remove=True)
    assert (codex / "config.toml").read_bytes() == original


@pytest.mark.parametrize("changed_target", ["config", "skill", "skill_directory"])
def test_setup_rollback_preserves_concurrent_operator_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_target: str
) -> None:
    import harness.codex_servers_adapter as adapter

    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    config = codex / "config.toml"
    original = b'model="operator"\n'
    config.write_bytes(original)
    skill = home / ".agents/skills/server-operations/SKILL.md"
    operator_edit = b"\n# concurrent operator preference\n"
    atomic = adapter._atomic

    def fail_marker_after_edit(
        path: Path, data: bytes, expected: bytes | None, skill_file: Path
    ) -> None:
        if path.name == ".harness-servers-owner.json":
            target = config if changed_target == "config" else skill
            if changed_target == "skill_directory":
                (skill.parent / "operator-notes.md").write_bytes(operator_edit)
            else:
                target.write_bytes(target.read_bytes() + operator_edit)
            raise OSError("synthetic marker write failure")
        atomic(path, data, expected, skill_file)

    monkeypatch.setattr(adapter, "_atomic", fail_marker_after_edit)
    with pytest.raises(GlobalServersSetupError, match="reconcile manually") as error:
        setup_global_servers(codex, home, "http://127.0.0.1:12345/servers/mcp", "SYNTHETIC")
    assert "previous content restored" not in str(error.value)
    assert len(str(error.value)) < 160
    if changed_target == "config":
        assert config.read_bytes().endswith(operator_edit)
        assert not skill.parent.exists()
    else:
        assert config.read_bytes() == original
        assert skill.exists()
        if changed_target == "skill":
            assert skill.read_bytes().endswith(operator_edit)
        else:
            assert (skill.parent / "operator-notes.md").read_bytes() == operator_edit


@pytest.mark.parametrize("failure", ["skill_unlink", "skill_rmdir", "marker_unlink"])
def test_remove_failure_restores_coherent_installed_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_bytes(b'model="operator"\n')
    url = "http://127.0.0.1:12345/servers/mcp"
    setup_global_servers(codex, home, url, "SYNTHETIC")
    skill = home / ".agents/skills/server-operations/SKILL.md"
    marker = codex / ".harness-servers-owner.json"
    snapshots = {path: path.read_bytes() for path in (codex / "config.toml", skill, marker)}
    unlink, rmdir = Path.unlink, Path.rmdir

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == (skill if failure == "skill_unlink" else marker) and failure != "skill_rmdir":
            raise OSError("synthetic unlink failure")
        unlink(path, missing_ok=missing_ok)

    def fail_rmdir(path: Path) -> None:
        if path == skill.parent and failure == "skill_rmdir":
            raise OSError("synthetic rmdir failure")
        rmdir(path)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(Path, "rmdir", fail_rmdir)
    with pytest.raises(GlobalServersSetupError, match="previous content restored"):
        setup_global_servers(codex, home, url, "SYNTHETIC", remove=True)
    assert {path: path.read_bytes() for path in snapshots} == snapshots
    assert set(item.name for item in skill.parent.iterdir()) == {"SKILL.md"}


@pytest.mark.parametrize("changed_target", ["config", "skill"])
def test_remove_rollback_preserves_concurrent_operator_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_target: str
) -> None:
    home, codex = tmp_path / "home", tmp_path / "codex"
    url = "http://127.0.0.1:12345/servers/mcp"
    setup_global_servers(codex, home, url, "SYNTHETIC")
    config = codex / "config.toml"
    installed = config.read_bytes()
    skill = home / ".agents/skills/server-operations/SKILL.md"
    marker = codex / ".harness-servers-owner.json"
    owner = marker.read_bytes()
    operator_edit = b"# concurrent operator content\n"
    unlink = Path.unlink

    def fail_marker_after_edit(path: Path, missing_ok: bool = False) -> None:
        if path == marker:
            if changed_target == "config":
                config.write_bytes(config.read_bytes() + operator_edit)
            else:
                skill.parent.mkdir(parents=True)
                skill.write_bytes(operator_edit)
            raise OSError("synthetic marker removal failure")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_marker_after_edit)
    with pytest.raises(GlobalServersSetupError, match="reconcile manually"):
        setup_global_servers(codex, home, url, "SYNTHETIC", remove=True)
    assert marker.read_bytes() == owner
    if changed_target == "config":
        assert config.read_bytes().endswith(operator_edit)
        assert skill.exists()
    else:
        assert config.read_bytes() == installed
        assert skill.read_bytes() == operator_edit


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("changed_target", ["config", "skill_directory"])
def test_fsync_window_preserves_operator_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: bool, changed_target: str
) -> None:
    import harness.codex_servers_adapter as adapter

    home, codex = tmp_path / "home", tmp_path / "codex"
    codex.mkdir()
    config = codex / "config.toml"
    config.write_bytes(b'model="operator"\n')
    skill = home / ".agents/skills/server-operations/SKILL.md"
    atomic, fsync = adapter._atomic, os.fsync
    active_target: Path | None = None
    undoing = False
    edited = False
    operator_edit = b"# operator edit during actual tempfile fsync\n"
    preserved: bytes | None = None

    def fail_marker(path: Path, data: bytes, expected: bytes | None, skill_file: Path) -> None:
        nonlocal active_target, undoing
        if path.name == ".harness-servers-owner.json" and rollback:
            undoing = True
            raise OSError("synthetic marker failure")
        active_target = path
        try:
            atomic(path, data, expected, skill_file)
        finally:
            active_target = None

    def edit_during_fsync(fd: int) -> None:
        nonlocal edited, preserved
        target = config if changed_target == "config" else skill
        # Undo of a newly created skill deletes it, so exercise skill fsync while updating
        # an existing installation; the real forward/undo writer remains in use.
        if active_target == target and undoing == rollback and not edited:
            if changed_target == "config":
                preserved = config.read_bytes() + operator_edit
                config.write_bytes(preserved)
            else:
                (skill.parent / "operator-notes.md").write_bytes(operator_edit)
            edited = True
        fsync(fd)

    if changed_target == "skill_directory" and rollback:
        setup_global_servers(codex, home, "http://127.0.0.1:12345/servers/mcp", "FIRST")
    monkeypatch.setattr(adapter, "_atomic", fail_marker)
    monkeypatch.setattr(os, "fsync", edit_during_fsync)
    with pytest.raises(GlobalServersSetupError, match="reconcile manually") as error:
        setup_global_servers(codex, home, "http://127.0.0.1:12345/servers/mcp", "SECOND")
    assert edited, "the edit must occur inside the actual atomic writer's fsync"
    assert "previous content restored" not in str(error.value)
    if changed_target == "config":
        assert config.read_bytes() == preserved
    else:
        assert (skill.parent / "operator-notes.md").read_bytes() == operator_edit
        if rollback:
            assert skill.exists()


_LIFECYCLE_WORKER = r"""
import json
import sys
import time
from pathlib import Path
import harness.codex_servers_adapter as adapter

args = json.loads(sys.argv[1])
root = Path(args['signals'])
config = Path(args['codex']) / 'config.toml'
skill = Path(args['home']) / '.agents/skills/server-operations/SKILL.md'
atomic, fsync, acquire, setup = (
    adapter._atomic, adapter.os.fsync, adapter._acquire_lifecycle_lock,
    adapter._setup_global_servers,
)
active = None
paused = False
def atomic_hook(path, data, expected, skill_file):
    global active
    if args.get('fail_marker') and path.name == '.harness-servers-owner.json':
        raise OSError('synthetic marker failure')
    active = path
    try:
        atomic(path, data, expected, skill_file)
    finally:
        active = None
def fsync_hook(fd):
    global paused
    if args.get('pause') and not paused and active == skill:
        paused = True
        (root / 'paused').touch()
        deadline = time.monotonic() + 8
        while not (root / 'release').exists():
            if time.monotonic() >= deadline:
                raise OSError('test release timed out')
            time.sleep(0.01)
    fsync(fd)
def acquire_hook(path, deadline):
    (root / 'attempted').touch()
    return acquire(path, deadline)
def setup_hook(*params, **kwargs):
    (root / 'snapshots-started').touch()
    return setup(*params, **kwargs)
adapter._atomic, adapter.os.fsync = atomic_hook, fsync_hook
adapter._acquire_lifecycle_lock, adapter._setup_global_servers = acquire_hook, setup_hook
try:
    adapter.setup_global_servers(
        Path(args['codex']), Path(args['home']), 'http://127.0.0.1:12345/servers/mcp',
        args['token'], remove=args.get('remove', False),
    )
    result = {'outcome': 'success'}
except adapter.GlobalServersSetupError as exc:
    result = {'outcome': 'error', 'message': str(exc)}
(root / 'result.json').write_text(json.dumps(result))
"""


def _start_lifecycle_worker(
    signals: Path, codex: Path, home: Path, **options: object
) -> subprocess.Popen[str]:
    signals.mkdir()
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _LIFECYCLE_WORKER,
            json.dumps(
                {"signals": str(signals), "codex": str(codex), "home": str(home), **options}
            ),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_signal(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"lifecycle worker did not create {path.name}")
        time.sleep(0.01)


def _delivery_bytes(codexes: list[Path], homes: list[Path]) -> list[bytes | None]:
    targets = [
        path / name for path in codexes for name in ("config.toml", ".harness-servers-owner.json")
    ]
    targets += [path / ".agents/skills/server-operations/SKILL.md" for path in homes]
    return [path.read_bytes() if path.exists() else None for path in targets]


@pytest.mark.parametrize("overlap", ["both", "skill", "codex"])
@pytest.mark.parametrize("second_remove", [False, True])
def test_lifecycle_serializes_overlapping_processes(
    tmp_path: Path, overlap: str, second_remove: bool
) -> None:
    codexes = [tmp_path / "codex-a", tmp_path / ("codex-b" if overlap == "skill" else "codex-a")]
    homes = [tmp_path / "home-a", tmp_path / ("home-b" if overlap == "codex" else "home-a")]
    # For removal and mixed-scope tests, start with known installed files at both scopes.
    if second_remove or overlap != "both":
        setup_global_servers(codexes[0], homes[0], "http://127.0.0.1:12345/servers/mcp", "INITIAL")
        if overlap == "skill":
            codexes[1].mkdir()
            for name in ("config.toml", ".harness-servers-owner.json"):
                shutil.copyfile(codexes[0] / name, codexes[1] / name)
        elif overlap == "codex":
            destination = homes[1] / ".agents/skills/server-operations"
            destination.mkdir(parents=True)
            shutil.copyfile(
                homes[0] / ".agents/skills/server-operations/SKILL.md", destination / "SKILL.md"
            )
    first_signals, second_signals = tmp_path / "first", tmp_path / "second"
    first = _start_lifecycle_worker(first_signals, codexes[0], homes[0], token="FIRST", pause=True)
    second: subprocess.Popen[str] | None = None
    try:
        _wait_for_signal(first_signals / "paused")
        locks = {
            codexes[0] / ".harness-servers.lock",
            homes[0] / ".agents/skills/.harness-servers.lock",
        }
        inodes = {path: (path.stat().st_dev, path.stat().st_ino) for path in locks}
        second = _start_lifecycle_worker(
            second_signals,
            codexes[1],
            homes[1],
            token="SECOND",
            remove=second_remove,
            fail_marker=not second_remove,
        )
        _wait_for_signal(second_signals / "attempted")
        time.sleep(0.15)
        assert not (second_signals / "snapshots-started").exists(), (
            "waiters must not snapshot managed files"
        )
        assert second.poll() is None
        (first_signals / "release").touch()
        output, error = first.communicate(timeout=8)
        assert first.returncode == 0, output + error
        output, error = second.communicate(timeout=8)
        assert second.returncode == 0, output + error
        assert json.loads((first_signals / "result.json").read_text())["outcome"] == "success"
        result = json.loads((second_signals / "result.json").read_text())
        assert result["outcome"] == ("success" if second_remove else "error")
        if second_remove:
            assert (codexes[1] / "config.toml").read_bytes() == b""
            assert not (codexes[1] / ".harness-servers-owner.json").exists()
            assert not (homes[1] / ".agents/skills/server-operations").exists()
        else:
            assert "previous content restored" in result["message"]
            assert b"FIRST" in (codexes[0] / "config.toml").read_bytes()
            assert (codexes[0] / ".harness-servers-owner.json").exists()
            assert (homes[0] / ".agents/skills/server-operations/SKILL.md").exists()
            assert b"SECOND" not in (codexes[1] / "config.toml").read_bytes()
        assert {path: (path.stat().st_dev, path.stat().st_ino) for path in locks} == inodes
    finally:
        (first_signals / "release").touch()
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


def test_lifecycle_lock_timeout_keeps_managed_files_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.codex_servers_adapter as adapter

    codex, home, signals = tmp_path / "codex", tmp_path / "home", tmp_path / "signals"
    process = _start_lifecycle_worker(signals, codex, home, token="FIRST", pause=True)
    try:
        _wait_for_signal(signals / "paused")
        before = _delivery_bytes([codex], [home])
        monkeypatch.setattr(adapter, "_LOCK_TIMEOUT", 0.1)
        with pytest.raises(GlobalServersSetupError, match="busy; no target changes made"):
            setup_global_servers(codex, home, "http://127.0.0.1:12345/servers/mcp", "SECOND")
        assert _delivery_bytes([codex], [home]) == before
        (signals / "release").touch()
        output, error = process.communicate(timeout=8)
        assert process.returncode == 0, output + error
    finally:
        (signals / "release").touch()
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("scope", ["codex", "skill"])
@pytest.mark.parametrize("collision", ["unknown", "symlink", "hardlink"])
def test_lifecycle_refuses_unknown_locks_without_changing_targets(
    tmp_path: Path, scope: str, collision: str
) -> None:
    codex, home = tmp_path / "codex", tmp_path / "home"
    lock = (codex if scope == "codex" else home / ".agents/skills") / ".harness-servers.lock"
    lock.parent.mkdir(parents=True)
    operator_file = tmp_path / "operator-file"
    operator_file.write_bytes(b"operator-owned content")
    if collision == "unknown":
        lock.write_bytes(operator_file.read_bytes())
        lock.chmod(0o600)
    elif collision == "symlink":
        lock.symlink_to(operator_file)
    else:
        os.link(operator_file, lock)
    before = _delivery_bytes([codex], [home])
    with pytest.raises(GlobalServersSetupError, match="lifecycle lock"):
        setup_global_servers(codex, home, "http://127.0.0.1:12345/servers/mcp", "SYNTHETIC")
    assert _delivery_bytes([codex], [home]) == before
    assert lock.read_bytes() == operator_file.read_bytes() == b"operator-owned content"
    assert lock.is_symlink() == (collision == "symlink")
