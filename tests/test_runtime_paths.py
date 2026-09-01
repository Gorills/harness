import os
import stat
from pathlib import Path

import pytest

from harness.runtime_paths import (
    DASHBOARD_ISOLATED_PORT,
    DASHBOARD_PORT,
    MCP_HTTP_ISOLATED_PORT,
    MCP_HTTP_PORT,
    InsecureStateDirectoryError,
    RuntimePathError,
    RuntimePaths,
    dashboard_listen_port,
    default_runtime_paths,
    ensure_private_state_directory,
    mcp_http_listen_port,
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


def test_default_runtime_paths_read_process_xdg_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    paths = default_runtime_paths()

    assert paths == RuntimePaths(
        database=state / "harness" / "harness.db",
        socket=runtime / "harness" / "harness.sock",
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


def test_ensure_private_state_directory_rejects_insecure_existing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "harness"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    with pytest.raises(InsecureStateDirectoryError, match="must be owned by the current user"):
        ensure_private_state_directory(directory)


def test_dashboard_listen_port_uses_fixed_canonical_and_isolated_ports() -> None:
    environment = {
        "XDG_STATE_HOME": "/state/alice",
        "XDG_RUNTIME_DIR": "/run/user/1001",
    }
    canonical = Path("/run/user/1001/harness/harness.sock")

    assert (
        dashboard_listen_port(
            canonical,
            environment=environment,
            home=Path("/home/ignored"),
            temp_directory=Path("/tmp/ignored"),
            effective_uid=1001,
        )
        == DASHBOARD_PORT
    )
    assert (
        dashboard_listen_port(
            canonical,
            environment={**environment, "HARNESS_DEV_ROOT": "/checkout"},
            home=Path("/home/ignored"),
            temp_directory=Path("/tmp/ignored"),
            effective_uid=1001,
        )
        == DASHBOARD_ISOLATED_PORT
    )
    assert (
        dashboard_listen_port(
            Path("/tmp/override/harness.sock"),
            environment=environment,
            home=Path("/home/ignored"),
            temp_directory=Path("/tmp/ignored"),
            effective_uid=1001,
        )
        == 0
    )


def test_mcp_http_listen_port_uses_distinct_canonical_and_isolated_ports() -> None:
    environment = {
        "XDG_STATE_HOME": "/state/alice",
        "XDG_RUNTIME_DIR": "/run/user/1001",
    }
    canonical = Path("/run/user/1001/harness/harness.sock")

    assert mcp_http_listen_port(canonical, environment=environment) == MCP_HTTP_PORT
    assert (
        mcp_http_listen_port(
            canonical,
            environment={**environment, "HARNESS_DEV_ROOT": "/checkout"},
        )
        == MCP_HTTP_ISOLATED_PORT
    )
    assert mcp_http_listen_port(Path("/tmp/override/harness.sock"), environment=environment) == 0


def test_acceptance_ports_override_canonical_ports() -> None:
    environment = {
        "XDG_STATE_HOME": "/state/alice",
        "XDG_RUNTIME_DIR": "/run/user/1001",
        "HARNESS_ACCEPTANCE_DASHBOARD_PORT": "28101",
        "HARNESS_ACCEPTANCE_MCP_HTTP_PORT": "28102",
    }
    canonical = Path("/run/user/1001/harness/harness.sock")

    assert dashboard_listen_port(canonical, environment=environment) == 28101
    assert mcp_http_listen_port(canonical, environment=environment) == 28102


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_acceptance_port_override_rejects_invalid_values(value: str) -> None:
    environment = {"HARNESS_ACCEPTANCE_MCP_HTTP_PORT": value}

    with pytest.raises(RuntimePathError, match="HARNESS_ACCEPTANCE_MCP_HTTP_PORT"):
        mcp_http_listen_port(Path("/tmp/harness.sock"), environment=environment)
