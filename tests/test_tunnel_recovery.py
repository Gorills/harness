"""Recovery regressions run only against mapped disposable roots and fixed programs."""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import pathlib
import platform
import signal
import socket
import subprocess
import sys
import tarfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import harness.tunnel_installer as installer
import harness.tunnel_platform as native
import harness.tunnel_service as service
from harness.tunnel_config import connected_lan_routes, runtime_settings
from harness.tunnel_ssh import REMOTE_BOOTSTRAP, maintenance_script


def test_real_iproute_connected_ipv6_shape() -> None:
    routes = [
        {"dst": "192.168.33.0/24", "dev": "eno1", "protocol": "kernel", "scope": "link"},
        {
            "dst": "fd12:3456::/64",
            "dev": "eno1",
            "protocol": "kernel",
            "metric": 256,
            "flags": [],
            "pref": "medium",
        },
        {
            "dst": "fe80::/64",
            "dev": "eno1",
            "protocol": "kernel",
            "metric": 256,
            "flags": [],
            "pref": "medium",
        },
        {"dst": "default", "gateway": "fe80::1", "dev": "eno1", "protocol": "ra"},
        {"dst": "fd22::/64", "gateway": "fe80::1", "dev": "eno1", "protocol": "kernel"},
        {"dst": "fd33::/64", "dev": "eno1", "protocol": "static"},
        {"dst": "2001:db8::/64", "dev": "eno1", "protocol": "kernel"},
        {"dst": "fd44::/64", "dev": "harness-tun", "protocol": "kernel"},
    ]
    assert connected_lan_routes(routes) == ["192.168.33.0/24", "fd12:3456::/64", "fe80::/64"]


def make_runtime(root: Path, hosts: list[str]) -> service.ServiceRuntime:
    instance = service.ServiceRuntime.__new__(service.ServiceRuntime)
    instance.path_hints = {}
    instance.root = root
    instance.operations = Lock()
    instance.observations = Lock()
    settings = runtime_settings(
        {"name": "Fixture", "endpoints": [{"host": host} for host in hosts]}
    )
    instance.state = {
        "profiles": {
            "fixture": {
                "revision": 1,
                "settings": settings,
                "secrets": {
                    "endpoints": [{"prepared": True, "maintenance_user": "root"} for _ in hosts]
                },
            }
        },
        "active": None,
        "desired": False,
        "state": "disconnected",
        "protection": "inactive",
        "snapshot": {},
    }
    return instance


@pytest.mark.parametrize("lost_ack", [False, True])
def test_removal_progress_reload_and_fresh_ssh_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_ack: bool
) -> None:
    hosts = ["203.0.113.10"] if lost_ack else ["203.0.113.10", "198.51.100.20"]
    instance = make_runtime(tmp_path, hosts)
    calls: list[str] = []
    removed: set[str] = set()
    failures = 0

    def maintain(root: Path, profile: str, target: dict[str, Any], action: str, user: str) -> None:
        nonlocal failures
        host = target["host"]
        calls.append(host)
        if host in removed:
            raise native.PlatformError("key no longer exists; absence is not proven")
        if lost_ack:
            removed.add(host)
        if failures == 0 and (lost_ack or host == hosts[-1]):
            failures += 1
            raise native.PlatformError("connection interrupted")
        removed.add(host)

    recovery: list[str] = []

    def reconcile(
        root: Path, profile: str, target: dict[str, Any], credentials: dict[str, str]
    ) -> None:
        assert credentials["password"] == "TRANSIENT_RECOVERY_CANARY"
        recovery.append(target["host"])
        removed.add(target["host"])

    monkeypatch.setattr(service, "maintain", maintain)
    monkeypatch.setattr(service, "reconcile_remove", reconcile)
    payload = {
        "profile_id": "fixture",
        "revision": 2,
        "settings": instance.state["profiles"]["fixture"]["settings"],
    }
    assert instance.execute("remove", payload)["code"] == "removal_unconfirmed"
    instance.state = json.loads((tmp_path / "applied.json").read_text())  # service restart
    assert instance.public("fixture")["snapshot"]["removal_pending"] is True
    if lost_ack:
        assert instance.execute("remove", payload)["ok"] is False
        assert "fixture" in instance.state["profiles"]
        payload["credentials"] = {"password": "TRANSIENT_RECOVERY_CANARY"}
    assert instance.execute("remove", payload)["ok"] is True
    assert "fixture" not in instance.state["profiles"]
    assert removed == set(hosts)
    assert calls == [hosts[0], hosts[0]] if lost_ack else calls == [*hosts, hosts[1]]
    assert recovery == [hosts[0]] if lost_ack else not recovery
    assert "TRANSIENT_RECOVERY_CANARY" not in (tmp_path / "applied.json").read_text()


def mapped_paths(root: Path) -> Callable[..., Path]:
    original = pathlib.Path

    def mapped(value: object = ".", *parts: str) -> Path:
        text = str(value)
        if text in {"/etc", "/opt", "/run", "/var", "/usr", "/root"} or text.startswith(
            ("/etc/", "/opt/", "/run/", "/var/", "/usr/", "/root/")
        ):
            return original(root, text.lstrip("/"), *parts)
        return original(text, *parts)

    return mapped


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("preexisting", [False, True])
def test_actual_bootstrap_failure_or_signal_then_retry_preserves_prior_files(
    tmp_path: Path, interrupted: bool, preexisting: bool
) -> None:
    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    (tmp_path / "run/systemd/system").mkdir(parents=True)
    (tmp_path / "etc/os-release").write_text("ID=debian\n")
    target = tmp_path / "opt/harness-tunnel/fixture"
    etc = tmp_path / "etc/harness-tunnel"
    if preexisting:
        for directory in (target, etc):
            directory.mkdir(parents=True)
            (directory / "owner.json").write_text('{"profile_id":"fixture", "prior":true}')
            (directory / "unrelated.txt").write_text("preserve")
    engine = b"non-executable test artifact"
    config = {
        "profile_id": "fixture",
        "engine_sha256": hashlib.sha256(engine).hexdigest(),
        "server_config": {},
        "certificate_server_name": "fixture.invalid",
        "control_script": "fixed helper",
        "maintenance_key": "ssh-ed25519 fixture",
    }
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, value in (("settings.json", json.dumps(config).encode()), ("sing-box", engine)):
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    for attempt in (1, 2):
        callbacks: list[Any] = []
        handlers: dict[int, Any] = {}

        def run(
            command: list[str],
            _attempt: int = attempt,
            _handlers: dict[int, Any] = handlers,
            **kwargs: Any,
        ) -> subprocess.CompletedProcess[bytes]:
            if "generate" in command:
                if _attempt == 1:
                    if interrupted:
                        _handlers[signal.SIGTERM](signal.SIGTERM, None)
                    return subprocess.CompletedProcess(command, 1, stdout=b"")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=b"-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
                )
            return subprocess.CompletedProcess(
                command, 1 if "is-active" in command else 0, stdout=b""
            )

        with ExitStack() as stack:
            stack.enter_context(patch.object(pathlib, "Path", mapped_paths(tmp_path)))
            stack.enter_context(patch.object(atexit, "register", callbacks.append))
            stack.enter_context(
                patch.object(
                    signal,
                    "signal",
                    lambda number, handler, bound=handlers: bound.__setitem__(number, handler),
                )
            )
            stack.enter_context(patch.object(platform, "machine", lambda: "x86_64"))
            stack.enter_context(patch("os.geteuid", return_value=0))
            stack.enter_context(patch.object(subprocess, "run", run))
            stack.enter_context(patch.object(socket, "socket"))
            stack.enter_context(
                patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data.getvalue())))
            )
            try:
                if attempt == 1:
                    with pytest.raises(SystemExit if interrupted else RuntimeError):
                        exec(compile(REMOTE_BOOTSTRAP, "fixed bootstrap", "exec"), {})
                else:
                    exec(compile(REMOTE_BOOTSTRAP, "fixed bootstrap", "exec"), {})
            finally:
                for callback in reversed(callbacks):
                    callback()
        if attempt == 1:
            assert target.exists() is preexisting
            assert etc.exists() is preexisting
            if preexisting:
                assert (
                    target / "owner.json"
                ).read_text() == '{"profile_id":"fixture", "prior":true}'
                assert (etc / "unrelated.txt").read_text() == "preserve"
    assert (target / "owner.json").is_file()
    if preexisting:
        assert (target / "unrelated.txt").read_text() == "preserve"


def test_fresh_remove_fixed_program_confirms_absence_and_preserves_unrelated(
    tmp_path: Path,
) -> None:
    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    (tmp_path / "root/.ssh").mkdir(parents=True)
    keys = tmp_path / "root/.ssh/authorized_keys"
    keys.write_text("ssh-ed25519 unrelated\n")
    script = maintenance_script("fixture")
    with (
        patch.object(pathlib, "Path", mapped_paths(tmp_path)),
        patch("os.geteuid", return_value=0),
        patch.object(subprocess, "run"),
        patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
    ):
        exec(compile(script, "fixed removal", "exec"), {})
    assert keys.read_text() == "ssh-ed25519 unrelated\n"
    collision = tmp_path / "etc/systemd/system/harness-tunnel-node-fixture.service"
    collision.write_text("unrelated service")
    with (
        patch.object(pathlib, "Path", mapped_paths(tmp_path)),
        patch("os.geteuid", return_value=0),
        patch.object(subprocess, "run") as commands,
        patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
    ):
        with pytest.raises(RuntimeError, match="unit collision"):
            exec(compile(script, "fixed removal", "exec"), {})
        assert not commands.called
    assert collision.read_text() == "unrelated service"


def test_actual_normal_setup_late_failure_then_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual copier shell/finalize, mapped roots; interpreter/OS commands stand in."""
    install, state, units = tmp_path / "install", tmp_path / "state", tmp_path / "units"
    units.mkdir()
    fail = tmp_path / "fail-once"
    fail.touch()
    bridge = tmp_path / "finalize_fixture.py"
    bridge.write_text(f"""import pathlib,sys,subprocess
sys.path.insert(0,{str(Path(__file__).resolve().parents[1] / "src")!r})
from types import SimpleNamespace
from unittest.mock import patch
import harness.tunnel_installer as installer
import harness.tunnel_service as service
import harness.tunnel_platform as native
service.INSTALL=pathlib.Path({str(install)!r});service.ROOT=pathlib.Path({str(state)!r})
original=pathlib.Path
account=SimpleNamespace(pw_uid=2000,pw_gid=2000,pw_gecos='harness-tunnel-runtime-v1',pw_shell='/usr/sbin/nologin')
def run(command,**kwargs):
 if 'enable' in command and pathlib.Path({str(fail)!r}).exists():
  pathlib.Path({str(fail)!r}).unlink()
  raise subprocess.CalledProcessError(1,command)
 return subprocess.CompletedProcess(command,0)
with patch.object(installer,'Path',lambda value:original({str(units)!r}) if str(value)=='/etc/systemd/system' else original(value)),patch.object(installer,'verify_bundle',return_value={{}}),patch.object(native,'require_owned'),patch('os.chown'),patch('pwd.getpwnam',return_value=account),patch.object(subprocess,'run',run):
 installer.finalize('1000')
""")

    def prepare(source: Path) -> dict[str, Any]:
        (source / "bundle").mkdir()
        (source / "packages/harness").mkdir(parents=True)
        (source / "packages/harness/tunnel_installer.py").write_text("fixture")
        binary = source / "runtime/bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        binary.parent.mkdir(parents=True)
        binary.write_text(f"#!/bin/sh\nexec {sys.executable} {bridge}\n")
        binary.chmod(0o700)
        return {}

    original_path = Path
    original_run = subprocess.run

    def execute(command: list[str], **kwargs: Any) -> Any:
        assert command[:3] == ["/usr/bin/pkexec", "/bin/sh", "-c"]
        shell = command[3]
        assert "/opt/harness-tunnel" not in shell
        shell = shell.replace("/usr/bin/chown -R root:root", "/usr/bin/true")
        return original_run(["/bin/sh", "-c", shell], **kwargs)

    monkeypatch.delenv("HARNESS_DEV_ROOT", raising=False)
    monkeypatch.setattr(
        installer, "__file__", str(tmp_path / "installed/package/tunnel_installer.py")
    )
    monkeypatch.setattr(
        installer,
        "Path",
        lambda value: install if str(value) == "/opt/harness-tunnel" else original_path(value),
    )
    monkeypatch.setattr(installer, "prepare_install", prepare)
    monkeypatch.setattr(subprocess, "run", execute)
    with pytest.raises(native.PlatformError):
        installer.request_install()
    assert not install.exists() and not state.exists()
    assert not native.fakeip_cache_root(state).exists()
    assert not list(units.iterdir())
    installer.request_install()
    assert install.exists() and (state / "service.json").is_file()
    assert (native.fakeip_cache_root(state) / "owner.json").is_file()
    assert len(list(units.iterdir())) == 2


def test_finalize_preflights_all_units_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, state, units = tmp_path / "install", tmp_path / "state", tmp_path / "units"
    install.mkdir()
    units.mkdir()
    main = units / "harness-tunnel.service"
    main.write_text("unrelated")
    original = Path
    monkeypatch.setattr(service, "INSTALL", install)
    monkeypatch.setattr(service, "ROOT", state)
    monkeypatch.setattr(
        installer,
        "Path",
        lambda value: units if str(value) == "/etc/systemd/system" else original(value),
    )
    with pytest.raises(native.PlatformError):
        installer.finalize("1000")
    assert not state.exists() and list(units.iterdir()) == [main]


@pytest.mark.parametrize(
    "answer", ["real", "fakeip", "wrong_transaction", "truncated", "http200_only", "no_answer"]
)
def test_dns_health_requires_selected_upstream_wire_answer(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    import base64
    import struct
    from urllib.parse import parse_qs, urlsplit

    import harness.tunnel_health as health
    from harness.tunnel_config import TUNNEL_DNS_NAME, TUNNEL_DNS_SERVER

    def request(url: str, **kwargs: Any) -> tuple[int, bytes]:
        assert url.startswith(f"https://{TUNNEL_DNS_SERVER}/dns-query?dns=")
        assert kwargs["accept"] == "application/dns-message"
        assert kwargs["server_name"] == TUNNEL_DNS_NAME
        encoded = parse_qs(urlsplit(url).query)["dns"][0]
        query = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        header = query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
        reply = (
            header
            + query[12:]
            + b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, 60, 4)
            + socket.inet_aton("198.18.0.2" if answer == "fakeip" else "104.16.132.229")
        )
        if answer == "wrong_transaction":
            reply = bytes([reply[0] ^ 1]) + reply[1:]
        if answer == "truncated":
            reply = reply[:-1]
        if answer == "http200_only":
            reply = b"success"
        if answer == "no_answer":
            reply = reply[:6] + b"\x00\x00" + reply[8:]
        return 200, reply

    monkeypatch.setattr(health, "request", request)
    assert health.dns_check() is (answer == "real")


def test_existing_install_and_state_are_never_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install, state = tmp_path / "install", tmp_path / "state"
    install.mkdir()
    state.mkdir()
    (install / "unrelated").write_text("preserve installation")
    (state / "applied.json").write_text('{"desired":true}')
    monkeypatch.setattr(service, "INSTALL", install)
    monkeypatch.setattr(service, "ROOT", state)
    with pytest.raises(native.PlatformError):
        installer.finalize("1000")
    assert (state / "applied.json").read_text() == '{"desired":true}'
    assert (install / "unrelated").read_text() == "preserve installation"


def test_windows_service_collision_is_preflight_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCM boundary double; this does not assert Windows execution acceptance."""
    from unittest.mock import Mock

    install, state = tmp_path / "install", tmp_path / "state"
    manager, existing = Mock(), Mock()
    scm = SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=2,
        OpenSCManager=lambda *args: manager,
        OpenService=lambda *args: existing,
    )
    monkeypatch.setitem(sys.modules, "win32service", scm)
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=OSError))
    monkeypatch.setattr(installer, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(service, "INSTALL", install)
    monkeypatch.setattr(service, "ROOT", state)
    with pytest.raises(native.PlatformError, match="Windows"):
        installer.finalize("S-1-5-21-fixture")
    assert not state.exists() and not install.exists()
    manager.Close.assert_called_once()
    existing.Close.assert_called_once()


def test_fresh_remove_owned_resources_then_lost_ack_retry_confirms_absence(tmp_path: Path) -> None:
    target = tmp_path / "opt/harness-tunnel/fixture"
    etc = tmp_path / "etc/harness-tunnel"
    units = tmp_path / "etc/systemd/system"
    keys = tmp_path / "root/.ssh/authorized_keys"
    units.mkdir(parents=True)
    keys.parent.mkdir(parents=True)
    for directory in (target, etc):
        directory.mkdir(parents=True)
        (directory / "owner.json").write_text('{"profile_id":"fixture"}')
    (target / "firewall.json").write_text("[]")  # minimal Debian without UFW
    (units / "harness-tunnel-node-fixture.service").write_text(
        f"ExecStart={target / 'sing-box'} run -c /etc/harness-tunnel/sing-box.json\n"
    )
    unrelated_unit = units / "unrelated.service"
    unrelated_unit.write_text("preserve")
    keys.write_text(
        'ssh-ed25519 unrelated\nrestrict,command="/usr/bin/python3 -I -S '
        + str(target / "control.py")
        + '" ssh-ed25519 fixture\n'
    )
    for _ in range(2):
        with (
            patch.object(pathlib, "Path", mapped_paths(tmp_path)),
            patch("os.geteuid", return_value=0),
            patch.object(subprocess, "run"),
            patch.object(subprocess, "check_output") as ufw,
            patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
        ):
            exec(compile(maintenance_script("fixture"), "fixed removal", "exec"), {})
            assert not ufw.called
    assert not target.exists() and not etc.exists()
    assert keys.read_text() == "ssh-ed25519 unrelated\n"
    assert list(units.iterdir()) == [unrelated_unit]


@pytest.mark.parametrize("key_kind", ["root", "maintenance"])
@pytest.mark.parametrize("failure", ["absent", "write", "replace", "none"])
def test_fixed_remove_keys_atomic_preservation(tmp_path: Path, key_kind: str, failure: str) -> None:
    """Actual fixed program and real temp IO; errors occur at write/replace boundaries."""
    import errno

    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    target = tmp_path / "opt/harness-tunnel/fixture"
    keys = tmp_path / (
        "root/.ssh/authorized_keys"
        if key_kind == "root"
        else "var/lib/harness-tunnel-maint/fixture/.ssh/authorized_keys"
    )
    keys.parent.mkdir(parents=True)
    foreign = b"# arbitrary foreign comment \xff\r\nssh-ed25519 unrelated-access\r\n"
    prefix = (
        'restrict,command="'
        + ("sudo -n " if key_kind == "maintenance" else "")
        + "/usr/bin/python3 -I -S "
        + str(target / "control.py")
        + '" '
    )
    own = (prefix + "ssh-ed25519 fixture\n").encode("ascii")
    before = foreign if failure == "absent" else foreign + own
    keys.write_bytes(before)
    keys.chmod(0o640)
    metadata = keys.stat()
    original_open, original_fdopen, original_replace = io.open, os.fdopen, os.replace
    mutations: list[str] = []

    class PartialWrite:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> PartialWrite:
            self.stream.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self.stream.__exit__(*args)

        def fileno(self) -> int:
            return int(self.stream.fileno())

        def write(self, data: bytes) -> None:
            self.stream.write(data[:3])
            self.stream.flush()
            mutations.append("partial temporary write")
            raise OSError(errno.ENOSPC, "fixture disk full while writing temp")

    def file_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if (
            isinstance(file, (str, os.PathLike))
            and Path(file) == keys
            and any(flag in mode for flag in "wax+")
        ):
            pytest.fail("the original key file must never be opened for writing")
        return original_open(file, mode, *args, **kwargs)

    def fdopen(descriptor: int, mode: str) -> Any:
        mutations.append("temporary opened")
        stream = original_fdopen(descriptor, mode)
        return PartialWrite(stream) if failure == "write" else stream

    def replace(source: Any, destination: Any) -> None:
        assert Path(destination) == keys
        assert keys.read_bytes() == before  # unchanged until the atomic publication
        mutations.append("replace")
        if failure == "replace":
            raise OSError(errno.EIO, "fixture atomic replace failure")
        original_replace(source, destination)

    with (
        patch.object(pathlib, "Path", mapped_paths(tmp_path)),
        patch("os.geteuid", return_value=0),
        patch.object(subprocess, "run"),
        patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
        patch.object(io, "open", file_open),
        patch.object(os, "fdopen", fdopen),
        patch.object(os, "replace", replace),
    ):
        if failure in {"write", "replace"}:
            with pytest.raises(OSError):
                exec(compile(maintenance_script("fixture"), "fixed removal", "exec"), {})
        else:
            exec(compile(maintenance_script("fixture"), "fixed removal", "exec"), {})
    assert keys.read_bytes() == (foreign if failure == "none" else before)
    actual = keys.stat()
    assert actual.st_mode == metadata.st_mode
    assert (actual.st_uid, actual.st_gid) == (metadata.st_uid, metadata.st_gid)
    assert list(keys.parent.iterdir()) == [keys]  # no orphan temporary files
    if failure == "absent":
        assert not mutations and actual.st_ino == metadata.st_ino
    else:
        assert mutations
    if failure in {"write", "replace"}:
        # Same shipped helper with fresh initial access can complete next time.
        with (
            patch.object(pathlib, "Path", mapped_paths(tmp_path)),
            patch("os.geteuid", return_value=0),
            patch.object(subprocess, "run"),
            patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
        ):
            exec(compile(maintenance_script("fixture"), "fresh removal retry", "exec"), {})
        assert keys.read_bytes() == foreign
        assert list(keys.parent.iterdir()) == [keys]


@pytest.mark.parametrize("key_kind", ["symlink", "directory"])
def test_fixed_remove_refuses_foreign_key_targets(tmp_path: Path, key_kind: str) -> None:
    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    keys = tmp_path / "root/.ssh/authorized_keys"
    keys.parent.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"ssh-ed25519 preserve\n")
    if key_kind == "symlink":
        keys.symlink_to(foreign)
    else:
        keys.mkdir()
    with (
        patch.object(pathlib, "Path", mapped_paths(tmp_path)),
        patch("os.geteuid", return_value=0),
        patch.object(subprocess, "run") as commands,
        patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
    ):
        with pytest.raises(RuntimeError, match="key collision"):
            exec(compile(maintenance_script("fixture"), "fixed removal", "exec"), {})
        assert not commands.called
    assert foreign.read_bytes() == b"ssh-ed25519 preserve\n"
    assert keys.is_symlink() if key_kind == "symlink" else keys.is_dir()


@pytest.mark.parametrize("directory_kind", ["target", "etc"])
@pytest.mark.parametrize("failure_point", ["contents", "rmdir"])
def test_fixed_remove_directory_io_failure_retains_owner_and_fresh_retry(
    tmp_path: Path, directory_kind: str, failure_point: str
) -> None:
    """Execute the actual helper twice; inject EIO at actual unlink/final rmdir."""
    import errno

    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    target, etc = tmp_path / "opt/harness-tunnel/fixture", tmp_path / "etc/harness-tunnel"
    foreign = tmp_path / "foreign-directory"
    foreign.mkdir()
    (foreign / "preserve").write_text("unrelated")
    evidence = b'{"profile_id":"fixture", "prior":"retained"}\n'
    for directory in (target, etc):
        directory.mkdir(parents=True)
        (directory / "owner.json").write_bytes(evidence)
        (directory / "owner.json").chmod(0o640)
        (directory / "owned-child").write_bytes(b"owned")
        (directory / "foreign-link").symlink_to(foreign, target_is_directory=True)
        (directory / "nested").mkdir()
        (directory / "nested/owned").write_text("nested")
    selected = target if directory_kind == "target" else etc
    marker = selected / "owner.json"
    metadata = marker.stat()
    original_rmdir, original_unlink = os.rmdir, os.unlink
    failures: list[str] = []

    def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == str(selected) and failure_point == "rmdir":
            assert not marker.exists()  # exercise the final marker-unlinked boundary
            failures.append("rmdir")
            raise OSError(errno.EIO, "fixture final directory removal error")
        original_rmdir(path, *args, **kwargs)

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == str(selected / "owned-child") and failure_point == "contents":
            assert marker.read_bytes() == evidence
            failures.append("contents")
            raise OSError(errno.EIO, "fixture child deletion error")
        original_unlink(path, *args, **kwargs)

    def execute() -> None:
        with (
            patch.object(pathlib, "Path", mapped_paths(tmp_path)),
            patch("os.geteuid", return_value=0),
            patch.object(subprocess, "run"),
            patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
        ):
            exec(compile(maintenance_script("fixture"), "fixed removal", "exec"), {})

    with patch.object(os, "rmdir", rmdir), patch.object(os, "unlink", unlink):
        with pytest.raises(OSError):
            execute()
    assert failures == [failure_point]
    assert selected.is_dir() and marker.read_bytes() == evidence
    actual = marker.stat()
    assert (actual.st_mode, actual.st_uid, actual.st_gid) == (
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )
    assert not list(selected.glob(".owner.json.harness-*"))
    execute()  # the same fixed program via fresh original SSH access
    assert not target.exists() and not etc.exists()
    execute()  # lost acknowledgement: confirmed absence is idempotent
    assert (foreign / "preserve").read_text() == "unrelated"


@pytest.mark.parametrize("directory_kind", ["target", "etc"])
def test_fixed_remove_still_refuses_unowned_directory(tmp_path: Path, directory_kind: str) -> None:
    (tmp_path / "etc/systemd/system").mkdir(parents=True)
    directory = tmp_path / (
        "opt/harness-tunnel/fixture" if directory_kind == "target" else "etc/harness-tunnel"
    )
    directory.mkdir(parents=True)
    (directory / "foreign").write_text("preserve")
    with (
        patch.object(pathlib, "Path", mapped_paths(tmp_path)),
        patch("os.geteuid", return_value=0),
        patch.object(subprocess, "run") as commands,
        patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"remove\n"))),
    ):
        with pytest.raises(RuntimeError, match="unowned directory"):
            exec(compile(maintenance_script("fixture"), "fixed removal", "exec"), {})
        assert not commands.called
    assert (directory / "foreign").read_text() == "preserve"
