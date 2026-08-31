from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from harness.host_adapters import IntegrationChange
from harness.host_integration_state import (
    HostIntegrationState,
    HostIntegrationStateError,
    add_host_profiles,
    host_integration_state_path,
    load_host_integration_state,
    remove_host_profiles,
    write_host_integration_state,
)
from harness.runtime_paths import RuntimePaths, default_runtime_paths

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX host integration state")


def _paths(tmp_path: Path) -> RuntimePaths:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    os.chmod(state, 0o700)
    return default_runtime_paths(
        environment={"XDG_STATE_HOME": str(state), "XDG_RUNTIME_DIR": str(runtime)}
    )


def test_host_integration_state_round_trip_and_idempotence(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert load_host_integration_state(paths).profiles == frozenset()
    assert add_host_profiles(paths, ("cursor",)) is IntegrationChange.CHANGED
    path = host_integration_state_path(paths)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value == {"version": 1, "profiles": ["cursor"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_host_integration_state(paths).includes("cursor")
    assert add_host_profiles(paths, ("cursor",)) is IntegrationChange.UNCHANGED
    assert remove_host_profiles(paths, ("cursor",)) is IntegrationChange.CHANGED
    assert not path.exists()
    assert remove_host_profiles(paths, ("cursor",)) is IntegrationChange.UNCHANGED


def test_host_integration_state_refuses_symlink(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_host_integration_state(paths, HostIntegrationState(profiles=frozenset({"cursor"})))
    path = host_integration_state_path(paths)
    outside = tmp_path / "outside.json"
    outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(HostIntegrationStateError, match="not a real regular file"):
        load_host_integration_state(paths)


def test_host_integration_state_refuses_unknown_version(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    add_host_profiles(paths, ("cursor",))
    path = host_integration_state_path(paths)
    path.write_text(json.dumps({"version": 2, "profiles": ["cursor"]}) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(HostIntegrationStateError, match="version is unsupported"):
        load_host_integration_state(paths)


def test_host_integration_state_refuses_unknown_profile(tmp_path: Path) -> None:
    with pytest.raises(HostIntegrationStateError, match="unsupported Harness host profile"):
        add_host_profiles(_paths(tmp_path), ("antigravity-ide",))


def test_host_integration_state_strips_retired_claude_code(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    path = host_integration_state_path(paths)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(
        json.dumps({"version": 1, "profiles": ["claude-code", "codex", "cursor"]}) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)

    state = load_host_integration_state(paths)

    assert state.profiles == frozenset({"codex", "cursor"})
    assert not state.includes("claude-code")
