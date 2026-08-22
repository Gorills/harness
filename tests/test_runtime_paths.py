import os
import stat
from pathlib import Path

import pytest

from harness.runtime_paths import (
    InsecureStateDirectoryError,
    RuntimePaths,
    default_runtime_paths,
    ensure_private_runtime_directory,
    ensure_private_state_directory,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX runtime-path slice")


def test_default_runtime_paths_use_absolute_xdg_locations() -> None:
    paths = default_runtime_paths(
        environment={
            "XDG_STATE_HOME": "/state/alice",
            "XDG_RUNTIME_DIR": "/run/user/1001",
        },
        home=Path("/home/ignored"),
        temp_directory=Path("/tmp/ignored"),
        effective_uid=1001,
    )

    assert paths == RuntimePaths(
        database=Path("/state/alice/harness/harness.db"),
        socket=Path("/run/user/1001/harness/harness.sock"),
    )


def test_default_runtime_paths_ignore_relative_xdg_values() -> None:
    paths = default_runtime_paths(
        environment={
            "XDG_STATE_HOME": "relative-state",
            "XDG_RUNTIME_DIR": "relative-runtime",
        },
        home=Path("/home/alice"),
        temp_directory=Path("/var/tmp"),
        effective_uid=1001,
    )

    assert paths == RuntimePaths(
        database=Path("/home/alice/.local/state/harness/harness.db"),
        socket=Path("/var/tmp/harness-1001/harness.sock"),
    )


def test_ensure_private_state_directory_creates_current_user_only_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "state" / "harness"

    ensure_private_state_directory(directory)

    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    assert directory.stat().st_uid == os.geteuid()


def test_ensure_private_runtime_directory_creates_current_user_only_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "run" / "harness"

    ensure_private_runtime_directory(directory)

    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    assert directory.stat().st_uid == os.geteuid()


def test_ensure_private_state_directory_rejects_insecure_existing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "harness"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    with pytest.raises(InsecureStateDirectoryError, match="must be owned by the current user"):
        ensure_private_state_directory(directory)
