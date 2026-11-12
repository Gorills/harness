"""Private cache ownership and cached numeric routes across actual engine replacement."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast

import pytest

import harness.tunnel_installer as installer
import harness.tunnel_platform as native
import harness.tunnel_service as service
from harness.tunnel_platform import PlatformError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX filesystem fixture")


def fixture_owner(path: Path, *, directory: bool = False, **_: Any) -> None:
    """Explicit root/ancestor boundary for disposable unprivileged filesystem tests."""
    info = path.lstat()
    assert info.st_uid == os.getuid()
    if (
        stat.S_ISLNK(info.st_mode)
        or info.st_mode & 0o022
        or directory != stat.S_ISDIR(info.st_mode)
        or (not directory and info.st_nlink != 1)
    ):
        raise PlatformError("unowned fixture path")


@pytest.fixture
def cache_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    (state / "credential").write_text("SECRET_STATE_CANARY")
    (state / "credential").chmod(0o600)
    configuration = {
        "owner_uid": os.getuid(),
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
    }
    monkeypatch.setattr(native, "require_owned", fixture_owner)
    real_chown = os.chown

    def root_chown(path: Path, uid: int, gid: int) -> None:
        # Only root ownership assignment is mocked; modes, FD operations and
        # all data operations below use the real temporary filesystem.
        assert path == native.fakeip_cache_root(state) and uid == 0 and gid == os.getgid()
        real_chown(path, os.getuid(), gid)

    monkeypatch.setattr(os, "chown", root_chown)
    native.create_fakeip_cache(state, configuration)
    return state, configuration


def test_cache_survives_runtime_reconstruction_without_exposing_secret_tree(
    cache_fixture: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, configuration = cache_fixture
    path = native.fakeip_cache_file(state, configuration)
    path.write_bytes(b"opaque engine-owned cache, not a JSON/service credential")
    inode = path.stat().st_ino
    for _ in range(3):
        runtime = service.ServiceRuntime.__new__(service.ServiceRuntime)
        runtime.root, runtime.configuration = state, configuration.copy()
        assert native.fakeip_cache_file(runtime.root, runtime.configuration) == path
        assert path.stat().st_ino == inode
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "credential").stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent.parent == state.parent and not path.is_relative_to(state)
    assert "SECRET_STATE_CANARY" not in (path.parent / "owner.json").read_text()
    other_owner = {**configuration, "owner_uid": configuration["owner_uid"] + 1}
    with pytest.raises(PlatformError):
        native.fakeip_cache_file(state, other_owner)
    assert path.stat().st_ino == inode


@pytest.mark.parametrize("foreign", ["symlink", "hardlink", "directory", "readable"])
def test_foreign_cache_file_is_refused_without_repair_or_following(
    cache_fixture: tuple[Path, dict[str, Any]], tmp_path: Path, foreign: str
) -> None:
    state, configuration = cache_fixture
    path = native.fakeip_cache_root(state) / "maps.db"
    canary = tmp_path / "foreign"
    canary.write_bytes(b"FOREIGN_CANARY")
    canary.chmod(0o600)
    before = canary.stat()
    if foreign == "symlink":
        path.symlink_to(canary)
    elif foreign == "hardlink":
        path.hardlink_to(canary)
    elif foreign == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"EXISTING_CACHE_CANARY")
        path.chmod(0o640)
    with pytest.raises(PlatformError):
        native.fakeip_cache_file(state, configuration)
    assert canary.read_bytes() == b"FOREIGN_CANARY"
    assert canary.stat().st_ino == before.st_ino
    assert canary.stat().st_mode == before.st_mode
    if foreign == "readable":
        assert (
            path.read_bytes() == b"EXISTING_CACHE_CANARY" and path.stat().st_mode & 0o777 == 0o640
        )


def test_wrong_cache_uid_and_pool_marker_are_refused(
    cache_fixture: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, configuration = cache_fixture
    path = native.fakeip_cache_file(state, configuration)
    real_stat = os.stat

    def wrong_owner(name: Any, *args: Any, **kwargs: Any) -> Any:
        value = real_stat(name, *args, **kwargs)
        if name == "maps.db":
            fields = list(value)
            fields[4] += 1
            return os.stat_result(fields)
        return value

    with monkeypatch.context() as changed:
        changed.setattr(os, "stat", wrong_owner)
        with pytest.raises(PlatformError):
            native.fakeip_cache_file(state, configuration)
    marker = path.parent / "owner.json"
    data = json.loads(marker.read_text())
    data["pools"] = ["198.19.0.0/16"]
    marker.write_text(json.dumps(data))
    with pytest.raises(PlatformError):
        native.fakeip_cache_file(state, configuration)
    assert path.stat().st_size == 0


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0, reason="requires nonroot POSIX DAC enforcement"
)
def test_effective_cache_file_write_does_not_require_directory_write(tmp_path: Path) -> None:
    directory = tmp_path / "read-only-parent"
    directory.mkdir(mode=0o700)
    path = directory / "maps.db"
    path.write_bytes(b"existing")
    path.chmod(0o600)
    # The fixture process owns the directory; remove its owner-write bit to
    # exercise the same effective no-directory-write condition as engine GID0750.
    directory.chmod(0o500)
    code = """import os,sys
with open(sys.argv[1],'ab') as output: output.write(b' updated')
try: os.unlink(sys.argv[1])
except PermissionError: pass
else: raise SystemExit('unexpected directory write')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(path)], capture_output=True, timeout=5
        )
        assert result.returncode == 0, result.stderr
        assert path.read_bytes() == b"existing updated"
    finally:
        directory.chmod(0o700)


def test_foreign_cache_parent_and_creation_failure_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    configuration = {"owner_uid": 42, "runtime_uid": os.getuid(), "runtime_gid": os.getgid()}
    monkeypatch.setattr(native, "require_owned", fixture_owner)
    cache = native.fakeip_cache_root(state)
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    (foreign / "canary").write_text("KEEP")
    cache.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(FileExistsError):
        native.create_fakeip_cache(state, configuration)
    with pytest.raises(PlatformError):
        native.fakeip_cache_file(state, configuration)
    assert (foreign / "canary").read_text() == "KEEP"
    cache.unlink()
    monkeypatch.setattr(os, "chown", lambda *args: None)
    monkeypatch.setattr(
        native, "write_private", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        native.create_fakeip_cache(state, configuration)
    assert not cache.exists()


def test_installer_cache_collision_and_owned_failure_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd

    state, install, units = tmp_path / "state", tmp_path / "install", tmp_path / "units"
    (install / "bundle").mkdir(parents=True)
    install.chmod(0o755)
    (install / "bundle").chmod(0o755)
    units.mkdir()
    cache = native.fakeip_cache_root(state)
    cache.mkdir(mode=0o700)
    (cache / "foreign").write_text("KEEP")
    monkeypatch.setattr(service, "ROOT", state)
    monkeypatch.setattr(service, "INSTALL", install)
    monkeypatch.setattr(native, "require_owned", fixture_owner)
    monkeypatch.setattr(installer, "verify_bundle", lambda *args, **kwargs: {})
    actual_path = Path
    monkeypatch.setattr(
        installer,
        "Path",
        lambda value: units if str(value) == "/etc/systemd/system" else actual_path(value),
    )
    account = SimpleNamespace(
        pw_uid=os.getuid(),
        pw_gid=os.getgid(),
        pw_gecos="harness-tunnel-runtime-v1",
        pw_shell="/usr/sbin/nologin",
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda *args: account)
    calls = []

    def run(command: list[str], **kwargs: Any) -> Any:
        assert command[0] == "/usr/bin/systemctl"
        calls.append(command)
        if "enable" in command:
            assert cache.is_dir() and (cache / "owner.json").is_file()
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PlatformError):
        installer.finalize(str(os.getuid() + 1000))
    assert not calls and not state.exists() and (cache / "foreign").read_text() == "KEEP"
    shutil.rmtree(cache)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    with pytest.raises(subprocess.CalledProcessError):
        installer.finalize(str(os.getuid() + 1000))
    assert not state.exists() and not cache.exists() and not list(units.iterdir())
    assert calls
    unit = native.systemd_units(Path("/opt/python"), Path("/opt/launcher"), state)
    assert f"ReadWritePaths={state} {cache} /run/harness-tunnel" in unit["harness-tunnel.service"]


def test_windows_cache_requests_only_system_admin_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.tunnel_windows_ipc as windows

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = {"owner_uid": 0, "owner_sid": "S-1-5-21-42", "runtime_uid": 0, "runtime_gid": 0}
    calls: list[tuple[Path, bool, str | None]] = []
    monkeypatch.setattr(native, "os", WindowsOS())
    monkeypatch.setattr(native, "require_owned", fixture_owner)
    monkeypatch.setattr(
        windows,
        "secure_admin_path",
        lambda path, *, directory, reader_sid=None: calls.append((path, directory, reader_sid)),
    )
    checks: list[tuple[Path, bool, str | None]] = []
    monkeypatch.setattr(
        windows,
        "require_admin_path",
        lambda path, *, directory, reader_sid=None: checks.append((path, directory, reader_sid)),
    )
    native.create_fakeip_cache(state, config)
    path = native.fakeip_cache_file(state, config)
    assert path == state / "fakeip-cache/maps.db"
    assert calls and all(sid is None for _, _, sid in calls)
    assert checks == [(path.parent, True, "S-1-5-18")]


def test_service_replacement_disconnect_restore_and_profile_remove_keep_mapping(
    cache_fixture: tuple[Path, dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.tunnel_config import runtime_settings

    state, configuration = cache_fixture
    install = tmp_path / "install"
    install.mkdir(mode=0o700)
    executable = install / "synthetic-non-executable"
    executable.write_bytes(b"fixture")
    native.write_private(state / "service.json", json.dumps(configuration).encode())
    cache = native.fakeip_cache_file(state, configuration)
    cache.write_bytes(b"PRIVATE_MAPPING_CANARY")
    monkeypatch.setattr(service, "require_owned", fixture_owner)
    monkeypatch.setattr(service, "verify_bundle", lambda *args, **kwargs: {"components": {}})
    monkeypatch.setattr(service, "component_path", lambda *args: executable)
    captures: list[dict[str, Any]] = []
    processes = []

    class Process:
        running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def terminate(self) -> None:
            self.running = False

        def wait(self, **kwargs: Any) -> int:
            assert not self.running
            return 0

    def spawn(command: list[str], **kwargs: Any) -> Process:
        assert command[0] == str(executable) and command[1] == "run"
        assert kwargs["umask"] == 0o077
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "maintain", lambda *args: None)

    def prepare(runtime: service.ServiceRuntime) -> None:
        runtime.guard = SimpleNamespace(
            disarm=lambda: None,
            configure_dns=lambda: None,
            underlay_interface=lambda host: "lo",
            engine_command=lambda engine, args, gid: [str(engine), *args],
        )
        monkeypatch.setattr(runtime.stop, "wait", lambda *args: False)

        def write_config(name: str, data: dict[str, Any]) -> Path:
            captures.append(data)
            path = tmp_path / name
            path.write_text(json.dumps(data))
            return path

        monkeypatch.setattr(runtime, "_runtime_file", write_config)

    settings = runtime_settings(
        {"name": "Fixture", "endpoints": [{"host": "203.0.113.10"}], "lan_bypass": False}
    )
    private = {
        k: "fixture"
        for k in (
            "uuid",
            "reality_server_name",
            "reality_public_key",
            "reality_short_id",
            "hysteria_password",
            "certificate_server_name",
            "certificate",
            "obfs_password",
        )
    }
    profile = {"settings": settings, "secrets": {"endpoints": [private]}, "revision": 1}
    runtime = service.ServiceRuntime(state, install)
    prepare(runtime)
    runtime.state["profiles"] = {"fixture": profile}
    runtime._start_path(profile, 0, "reality", "hysteria2")
    runtime._start_path(profile, 0, "reality", "reality")
    runtime._disconnect()
    runtime._start_path(profile, 0, "hysteria2", "reality")
    runtime._disconnect()
    restored = service.ServiceRuntime(state, install)
    prepare(restored)
    restored._start_path(restored.state["profiles"]["fixture"], 0, "reality", "hysteria2")
    restored._disconnect()
    restored._remove("fixture", {"credentials": {}})
    assert all(not p.running for p in processes)
    assert len(captures) == 4
    for config in captures:
        remote = [
            outbound
            for outbound in config["outbounds"]
            if outbound["type"] in {"vless", "hysteria2"}
        ]
        assert len(remote) == 2 and all(outbound["bind_interface"] == "lo" for outbound in remote)
        assert "bind_interface" not in config["outbounds"][-1]
    assert all(c["experimental"]["cache_file"]["path"] == str(cache) for c in captures)
    assert cache.read_bytes() == b"PRIVATE_MAPPING_CANARY"
    assert "PRIVATE_MAPPING_CANARY" not in (state / "applied.json").read_text()
    assert "PRIVATE_MAPPING_CANARY" not in json.dumps(restored.public("fixture"))


@pytest.mark.skipif(not os.environ.get("HARNESS_TEST_ENGINE"), reason="requires admitted engine")
@pytest.mark.parametrize("version", [4, 6])
@pytest.mark.parametrize("crash", [False, True])
def test_actual_cached_fakeip_survives_restart_and_applies_new_profile_policy(
    tmp_path: Path, version: int, crash: bool
) -> None:
    import ipaddress
    import runpy

    helpers = runpy.run_path(str(Path(__file__).with_name("test_tunnel_engine_routes.py")))
    engine, client, port, socks = (
        helpers[k] for k in ("engine", "client", "free_port", "socks_numeric")
    )
    stop = Event()
    seen: list[bytes] = []
    cache = tmp_path / "private-maps.db"
    cache.touch(mode=0o600)
    domain = "fixture.example"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        target_port = listener.getsockname()[1]

        def serve() -> None:
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                with peer:
                    peer.settimeout(2)
                    with contextlib.suppress(OSError):
                        seen.append(peer.recv(4096))
                        peer.sendall(b"CACHED ROUTE OK")

        thread = Thread(target=serve)
        thread.start()
        socks_port = port()

        def config(exceptions: list[str]) -> dict[str, Any]:
            full = client(tmp_path, exceptions, cache=cache)
            full["inbounds"] = [
                {"type": "socks", "tag": "tun-in", "listen": "127.0.0.1", "listen_port": socks_port}
            ]
            full["dns"]["servers"] = [full["dns"]["servers"][0]] + [
                {
                    "type": "hosts",
                    "tag": tag,
                    "predefined": {domain: "127.0.0.1", "other.example": "127.0.0.1"},
                }
                for tag in ("direct-dns", "tunnel-dns")
            ]
            dead = port()
            full["outbounds"] = [
                {"type": "socks", "tag": tag, "server": "127.0.0.1", "server_port": dead}
                for tag in ("reality", "hysteria2", "naive")
            ] + [full["outbounds"][-1]]
            full["route"].pop("auto_detect_interface", None)
            assert all(
                i["type"] == "socks" and i["listen"] == "127.0.0.1" for i in full["inbounds"]
            )
            assert all(s["type"] in {"fakeip", "hosts"} for s in full["dns"]["servers"])
            assert all(o.get("server", "127.0.0.1") == "127.0.0.1" for o in full["outbounds"])
            return cast(dict[str, Any], full)

        def query(name: str = domain) -> str:
            control, relay, relay_port = socks(socks_port, 3, "0.0.0.0", 0)
            labels = b"".join(bytes([len(s)]) + s.encode() for s in name.split(".")) + b"\0"
            question = labels + struct.pack("!HH", 28 if version == 6 else 1, 1)
            dns = b"\x12\x34" + struct.pack("!HHHHH", 0x100, 1, 0, 0, 0) + question
            frame = b"\0\0\0\x01" + socket.inet_aton("1.1.1.1") + struct.pack("!H", 53) + dns
            with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
                peer.settimeout(3)
                peer.sendto(frame, ("127.0.0.1" if relay == "0.0.0.0" else relay, relay_port))
                answer, _ = peer.recvfrom(4096)
                return str(ipaddress.ip_address(answer[-16:] if version == 6 else answer[-4:]))

        def request(address: str) -> bytes:
            peer, _, _ = socks(socks_port, 1, address, target_port)
            with peer:
                peer.settimeout(2)
                peer.sendall(b"opaque cached application data")
                return cast(bytes, peer.recv(4096))

        try:
            with engine(tmp_path, "profile-a", config([domain]), socks_port, crash=crash):
                old = query()
                assert request(old) == b"CACHED ROUTE OK"
            with engine(tmp_path, "reconnect", config([domain]), socks_port):
                assert request(old) == b"CACHED ROUTE OK"
                assert query("other.example") != old
            # Merely loading metadata must not consume its durable copy: an
            # engine can crash again without allocating any new DNS addresses.
            with engine(tmp_path, "read-only-restart", config([domain]), socks_port, crash=crash):
                assert request(old) == b"CACHED ROUTE OK"
            with engine(tmp_path, "profile-b", config([]), socks_port):
                blocked = b""
                with contextlib.suppress(OSError, AssertionError):
                    blocked = request(old)
                assert blocked != b"CACHED ROUTE OK"
            assert "missing fakeip record" not in (tmp_path / "profile-b.log").read_text()
            assert domain in (tmp_path / "profile-b.log").read_text()
            with engine(tmp_path, "new-service-profile-a", config([domain]), socks_port):
                assert request(old) == b"CACHED ROUTE OK"
            assert len(seen) == 4
        finally:
            stop.set()
            thread.join(timeout=3)
        assert not thread.is_alive()
