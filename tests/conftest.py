from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_harness_xdg_from_user_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep tests off the caller's canonical Harness state and socket paths."""
    root = tmp_path_factory.mktemp("harness-xdg")
    state = root / "state"
    runtime = root / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
