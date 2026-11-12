from __future__ import annotations

import io
import json
import os
import socket
import ssl
import subprocess
import tarfile
from pathlib import Path
from threading import Lock, Thread
from typing import Any

import pytest

import harness.tunnel_health as health
import harness.tunnel_service as service
from harness.tunnel_artifacts import ArtifactError
from harness.tunnel_builder import extract_archive
from harness.tunnel_config import runtime_settings
from harness.tunnel_installer import catalog_entry
from harness.tunnel_platform import PlatformError, nft_rules, systemd_units
from harness.tunnel_ssh import REMOTE_BOOTSTRAP, maintenance_script
from harness.tunnels import validate_credentials


def settings() -> dict[str, Any]:
    return runtime_settings(
        {
            "name": "Fixture",
            "endpoints": [{"host": "203.0.113.10", "user": "root", "port": 22}],
            "exceptions": [],
            "lan_bypass": True,
        }
    )


@pytest.mark.parametrize("broken_step", [None, "dns", "domain", "default-route", "flush-caches"])
def test_resolved_configuration_is_owned_and_failure_is_not_ignored(
    monkeypatch: pytest.MonkeyPatch, broken_step: str | None
) -> None:
    import harness.tunnel_platform as platform

    calls: list[list[str]] = []

    def command(args: list[str], **kwargs: Any) -> bytes:
        calls.append(args)
        if args[1] == broken_step:
            raise PlatformError("fixture resolver failure")
        return b""

    with monkeypatch.context() as patch:
        patch.setattr(platform, "run", command)
        patch.setattr(Path, "exists", lambda self: True)
        if broken_step:
            with pytest.raises(PlatformError):
                platform.LinuxGuard(995).configure_dns()
            assert calls[-1] == ["/usr/bin/resolvectl", "revert", "harness-tun"]
        else:
            platform.LinuxGuard(995).configure_dns()
            assert calls[-1][1:] == ["flush-caches"]
    # No foreign interface receives DNS changes, even during rollback.
    assert all(args[2] == "harness-tun" for args in calls if len(args) > 2)


@pytest.mark.parametrize("dns_ok,tcp_ok", [(False, False), (True, False), (True, True)])
def test_successful_socks_is_insufficient_for_connected_app_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dns_ok: bool, tcp_ok: bool
) -> None:
    from harness.tunnel_config import PathResult

    instance = runtime(tmp_path)
    monkeypatch.setattr(
        service,
        "probe",
        lambda *args, **kwargs: (PathResult(0, "reality", True, True, True), "203.0.113.2"),
    )
    identities = []

    def app_probe(uid: int, expected_ip: str) -> tuple[bool, bool]:
        identities.append((uid, expected_ip))
        return dns_ok, tcp_ok

    monkeypatch.setattr(service, "system_probe", app_probe)
    result, address = instance._probe_path(0, "reality", bulk=True)
    assert identities == [(1000, "203.0.113.2")]
    assert result.usable == (dns_ok and tcp_ok)
    assert address == ("203.0.113.2" if result.usable else "")


def runtime(tmp_path: Path) -> service.ServiceRuntime:
    instance = service.ServiceRuntime.__new__(service.ServiceRuntime)
    instance.path_hints = {}
    instance.owner_uid = 1000
    instance.root = tmp_path
    instance.engine = tmp_path / "engine"
    instance.engine.write_bytes(b"synthetic non-executable artifact")
    instance.server_engine = instance.engine
    instance.naive = None
    instance.manifest = {"version": "synthetic", "system_packages": {}}
    instance.state = {
        "profiles": {},
        "desired": False,
        "active": None,
        "state": "disconnected",
        "protection": "inactive",
        "snapshot": {},
    }
    instance.observations = Lock()
    return instance


def test_prepare_persists_recoverable_intent_and_retries_without_original_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = runtime(tmp_path)
    private = {
        key: "fixture-value"
        for key in (
            "uuid",
            "reality_private_key",
            "reality_public_key",
            "reality_short_id",
            "reality_server_name",
            "naive_username",
            "naive_password",
            "hysteria_password",
            "obfs_password",
            "certificate_server_name",
        )
    }
    monkeypatch.setattr(instance, "_new_endpoint", lambda: private.copy())
    calls = 0

    def bootstrap(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        persisted = json.loads((tmp_path / "applied.json").read_bytes())
        assert persisted["profiles"]["fixture"]["secrets"]["endpoints"][0]["prepared"] is False
        assert "INITIAL_PASSWORD_CANARY" not in json.dumps(persisted)
        if calls == 1:
            raise PlatformError("synthetic lost acknowledgement")
        return {
            "certificate": "synthetic certificate",
            "public_certificate": False,
            "maintenance_user": "root",
        }

    monkeypatch.setattr(service, "bootstrap", bootstrap)
    payload = {
        "settings": settings(),
        "revision": 1,
        "credentials": {"password": "INITIAL_PASSWORD_CANARY"},
    }
    with pytest.raises(PlatformError):
        instance._prepare("fixture", payload)
    assert instance.state["profiles"]["fixture"]["secrets"]["endpoints"][0]["prepared"] is False
    assert instance.public("fixture")["snapshot"]["prepared"] is False
    assert instance.public("fixture")["snapshot"]["setup_incomplete"] is True
    instance._prepare("fixture", payload)
    assert calls == 2
    recovered = json.loads((tmp_path / "applied.json").read_text())
    assert recovered["profiles"]["fixture"]["secrets"]["endpoints"][0]["prepared"] is True
    assert instance.public("fixture")["snapshot"]["prepared"] is True
    assert instance.public("fixture")["snapshot"]["setup_incomplete"] is False
    assert len(recovered["profiles"]["fixture"]["secrets"]["endpoints"]) == 1
    assert "INITIAL_PASSWORD_CANARY" not in (tmp_path / "applied.json").read_text()


def test_all_endpoints_must_be_prepared_before_reporting_ready(tmp_path: Path) -> None:
    instance = runtime(tmp_path)
    profile: dict[str, Any] = {"settings": settings(), "revision": 1, "secrets": {"endpoints": []}}
    instance.state["profiles"]["fixture"] = profile
    assert instance.public("fixture")["snapshot"]["prepared"] is False
    profile["secrets"]["endpoints"].append({"prepared": True})
    assert instance.public("fixture")["snapshot"]["prepared"] is True
    profile["settings"]["endpoints"].append({"host": "198.51.100.20", "user": "root", "port": 22})
    assert instance.public("fixture")["snapshot"]["prepared"] is False
    profile["secrets"]["endpoints"].append({"prepared": False})
    assert instance.public("fixture")["snapshot"]["prepared"] is False
    profile["secrets"]["endpoints"][1]["prepared"] = True
    assert instance.public("fixture")["snapshot"]["prepared"] is True
    profile["removing"] = True
    snapshot = instance.public("fixture")["snapshot"]
    assert snapshot["prepared"] is False and snapshot["removal_pending"] is True


@pytest.mark.parametrize("host", ["203.0.113.10", "2001:db8::10"])
def test_vps_underlay_requires_standalone_network(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    import harness.tunnel_platform as platform

    device = ["enp3s0"]

    def route(command: list[str]) -> bytes:
        if "link" in command:
            return json.dumps(
                [
                    {
                        "link_type": "none" if device[0] == "amn0" else "ether",
                        "linkinfo": {"info_kind": "tun" if device[0] == "amn0" else ""},
                    }
                ]
            ).encode()
        assert command[-5:] == ["route", "get", host, "uid", "995"]
        return json.dumps([{"dev": device[0]}]).encode()

    monkeypatch.setattr(platform, "run", route)
    assert platform.LinuxGuard(995).underlay_interface(host) == "enp3s0"
    device[0] = "amn0"
    with pytest.raises(platform.VPNConflict):
        platform.LinuxGuard(995).underlay_interface(host)
    device[0] = "harness-tun"
    with pytest.raises(PlatformError, match="маршрут"):
        platform.LinuxGuard(995).underlay_interface(host)


@pytest.mark.parametrize("reachable", [False, True])
def test_foreign_vpn_kill_switch_requires_reachable_output_chain(
    monkeypatch: pytest.MonkeyPatch, reachable: bool
) -> None:
    import harness.tunnel_platform as platform

    data = {
        "nftables": [
            {"chain": {"family": "ip", "table": "filter", "name": "OUTPUT", "hook": "output"}},
            {
                "rule": {
                    "family": "ip",
                    "table": "filter",
                    "chain": "OUTPUT" if reachable else "unused",
                    "expr": [{"jump": {"target": "amnvpn.anchors"}}],
                }
            },
            {
                "rule": {
                    "family": "ip",
                    "table": "filter",
                    "chain": "amnvpn.anchors",
                    "expr": [{"jump": {"target": "amnvpn.blockAll"}}],
                }
            },
            {
                "rule": {
                    "family": "ip",
                    "table": "filter",
                    "chain": "amnvpn.blockAll",
                    "expr": [{"reject": {"type": "icmp"}}],
                }
            },
        ]
    }
    monkeypatch.setattr(platform, "run", lambda command: json.dumps(data).encode())
    if reachable:
        with pytest.raises(platform.VPNConflict):
            platform.LinuxGuard(995).check_conflicts()
    else:
        platform.LinuxGuard(995).check_conflicts()


def test_explicit_disconnect_persists_intent_before_releasing_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = runtime(tmp_path)
    instance.state.update(desired=True, active="fixture", protection="active", state="connected")
    monkeypatch.setattr(instance, "_stop_engines", lambda: None)

    class Guard:
        def disarm(self) -> None:
            persisted = json.loads((tmp_path / "applied.json").read_text())
            assert persisted["desired"] is False
            assert persisted["state"] != "disconnected"

    instance.guard = Guard()
    instance._disconnect()
    assert instance.state["protection"] == "inactive"


def test_boot_guard_does_not_require_engine_and_corrupt_intent_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int | None, list[str]]] = []

    class Guard:
        def __init__(self, uid: int | None) -> None:
            self.uid = uid

        def arm(self, routes: list[str]) -> None:
            calls.append((self.uid, routes))

    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "LinuxGuard", Guard)
    monkeypatch.setattr(service, "_read_json", lambda path: json.loads(path.read_text()))
    (tmp_path / "service.json").write_text('{"runtime_uid":1234}')
    (tmp_path / "applied.json").write_text('{"desired":true}')
    service.restore_linux_guard()
    assert calls == [(1234, [])]
    (tmp_path / "applied.json").write_text('{"damaged":true}')
    with pytest.raises(PlatformError):
        service.restore_linux_guard()
    assert calls[-1] == (None, [])
    assert "skuid" not in nft_rules(None, [])
    units = systemd_units(
        Path("/opt/fixture/python"), Path("/opt/fixture/launcher.py"), Path("/var/lib/fixture")
    )
    assert "Type=oneshot" in units["harness-tunnel-guard.service"]
    assert "restore-guard" in units["harness-tunnel-guard.service"]


def test_archive_members_cannot_escape_source_build(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as target:
        root = tarfile.TarInfo("source")
        root.type = tarfile.DIRTYPE
        target.addfile(root)
        member = tarfile.TarInfo("source/../escape")
        member.size = 1
        target.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ArtifactError):
        extract_archive(archive, tmp_path / "output", "source/")
    assert not (tmp_path / "escape").exists()


def test_shipped_core_excludes_unapproved_native_and_remote_programs_compile() -> None:
    entry = catalog_entry()
    assert entry["audit"]["status"] == "approved"
    assert "naive" not in entry["components"] and not entry["system_packages"]
    assert entry["components"]["sing-box"]["method"] == "source-build"
    compile(REMOTE_BOOTSTRAP, "remote bootstrap", "exec")
    compile(maintenance_script("fixture"), "restricted maintenance", "exec")
    assert validate_credentials(
        {"password": "primary", "backup_password": "backup", "backup_private_key": ""}
    ) == {"password": "primary", "backup_password": "backup"}


def test_socks_https_actual_bulk_upload_and_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent local TLS fixture; never routes or installs a system tunnel."""
    executable = os.environ.get("HARNESS_TEST_ENGINE")
    if not executable:
        pytest.skip("set HARNESS_TEST_ENGINE to the admitted scratch build for local TLS fixture")
    pem = tmp_path / "fixture.pem"
    pem.write_bytes(
        subprocess.check_output(
            [executable, "generate", "tls-keypair", "fixture.invalid", "--months", "1"]
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(pem)
    factory = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda: factory(cafile=str(pem)))
    received: list[bytes] = []
    errors: list[BaseException] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(10)
        proxy_port = listener.getsockname()[1]
        original = health.socks_connect
        monkeypatch.setattr(
            health, "socks_connect", lambda host, port: original(host, port, proxy_port=proxy_port)
        )

        def serve() -> None:
            try:
                for _ in range(2):
                    peer, _ = listener.accept()
                    with peer:
                        peer.settimeout(10)
                        assert health.receive(peer, 3) == b"\x05\x01\x00"
                        peer.sendall(b"\x05\x00")
                        assert health.receive(peer, 4) == b"\x05\x01\x00\x03"
                        length = health.receive(peer, 1)[0]
                        assert health.receive(peer, length) == b"fixture.invalid"
                        health.receive(peer, 2)
                        peer.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb")
                        with server_context.wrap_socket(peer, server_side=True) as secure:
                            file = secure.makefile("rb")
                            first = file.readline()
                            headers = {}
                            while line := file.readline().strip():
                                key, value = line.split(b":", 1)
                                headers[key.lower()] = value.strip()
                            body = file.read(int(headers.get(b"content-length", b"0")))
                            if first.startswith(b"POST"):
                                received.append(body)
                                reply = b"ok"
                            else:
                                reply = b"x" * health.BULK_BYTES
                            secure.sendall(
                                f"HTTP/1.1 200 OK\r\nContent-Length: {len(reply)}\r\nConnection: close\r\n\r\n".encode()
                                + reply
                            )
                            file.close()
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=serve, daemon=True)
        thread.start()
        status, download = health.request("https://fixture.invalid/down")
        assert status == 200 and len(download) == health.BULK_BYTES
        upload = b"u" * health.BULK_BYTES
        assert health.request("https://fixture.invalid/up", body=upload) == (200, b"ok")
        thread.join(timeout=10)
    assert not errors and not thread.is_alive()
    assert received == [upload]


@pytest.mark.parametrize("password_required", [False, True])
def test_sudo_bootstrap_tar_framing_with_and_without_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    password_required: bool,
) -> None:
    """Run the actual fixed shell framing against a disposable sudo stand-in."""
    import sys

    import harness.tunnel_ssh as ssh

    directory = tmp_path / "ssh"
    directory.mkdir()
    (directory / "fixture-maintenance").write_text("synthetic key")
    (directory / "fixture-maintenance.pub").write_text("ssh-ed25519 synthetic")
    engine = tmp_path / "engine"
    engine.write_bytes(b"synthetic non-executable fixture")
    checker = tmp_path / "check.py"
    checker.write_text(
        "import sys,io,tarfile,json\nraw=sys.stdin.buffer.read()\nassert raw[:2]==b'\\x1f\\x8b'\narchive=tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz')\nassert set(archive.getnames())=={'sing-box','settings.json'}\nprint(json.dumps({'ok':True,'certificate':'-----BEGIN CERTIFICATE-----\\nfixture','public_certificate':False,'maintenance_user':'root'}))\n"
    )
    fake = tmp_path / "sudo"
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = -n ] && [ "$2" = true ]; then exit '
        + ("1" if password_required else "0")
        + "; fi\n"
        + 'if [ "$1" = -S ]; then IFS= read -r pass; [ "$pass" = SUDO_CANARY ] || exit 3; fi\n'
        + 'exec "$FIXTURE_PYTHON" "$FIXTURE_CHECK"\n'
    )
    fake.chmod(0o700)
    original = subprocess.run

    def execute(arguments: list[str], **kwargs: Any) -> Any:
        environment = {
            **kwargs["env"],
            "PATH": str(tmp_path) + os.pathsep + "/usr/bin:/bin",
            "FIXTURE_PYTHON": sys.executable,
            "FIXTURE_CHECK": str(checker),
        }
        return original(
            ["/bin/sh", "-c", arguments[-1]],
            input=kwargs["input"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    monkeypatch.setattr(subprocess, "run", execute)
    result = ssh.bootstrap(
        tmp_path,
        "fixture",
        {"host": "203.0.113.10", "user": "ubuntu", "port": 22},
        {"sudo_password": "SUDO_CANARY"},
        {},
        engine,
    )
    assert result["ok"] is True


def test_truncated_doh_response_keeps_monitor_alive_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real trusted TLS/chunk parser -> DNS/probe/_check/monitor, no OS tunnel."""
    import base64
    import copy
    import struct
    from queue import Queue
    from threading import Event, Semaphore
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from harness.tunnel_config import AdaptivePaths

    pem, key = tmp_path / "doh.pem", tmp_path / "doh.key"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(pem),
            "-days",
            "1",
            "-subj",
            "/CN=cloudflare-dns.com",
            "-addext",
            "subjectAltName=DNS:cloudflare-dns.com,DNS:www.cloudflare.com,DNS:api.ipify.org,DNS:speed.cloudflare.com,DNS:one.one.one.one",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(pem, key)
    original_context = ssl.create_default_context
    fail_dns, stop_fixture = Event(), Event()
    fail_dns.set()
    requests: list[str] = []
    uploaded: list[int] = []
    fixture_errors: list[BaseException] = []
    monitor_errors: list[BaseException] = []

    class GatedStop:
        """Only the scheduler clock/wait is controlled, never health results."""

        def __init__(self) -> None:
            self.ready: Queue[None] = Queue()
            self.permit = Semaphore(0)
            self.done = Event()

        def wait(self, timeout: float) -> bool:
            self.ready.put(None)
            if not self.permit.acquire(timeout=5):
                raise TimeoutError("fixture monitor iteration was not scheduled")
            return self.done.is_set()

    gate = GatedStop()
    clock = [1000.0]
    instance = service.ServiceRuntime.__new__(service.ServiceRuntime)
    instance.path_hints = {}
    instance.owner_uid = 1000
    monkeypatch.setattr(service, "system_probe", lambda uid, ip: (True, True))
    clean_settings = settings()
    clean_settings["lan_bypass"] = False
    instance.state = {
        "profiles": {
            "fixture": {
                "settings": clean_settings,
                "revision": 1,
                "secrets": {"endpoints": [{}], "metrics_token": "synthetic"},
            }
        },
        "active": "fixture",
        "desired": True,
        "state": "connected",
        "protection": "active",
        "snapshot": {
            "lan_routes": [],
            "tcp_ok": True,
            "dns_ok": True,
            "bulk_ok": True,
            "udp_ok": True,
            "transport": "reality",
        },
    }
    instance.stop = gate  # type: ignore[assignment]
    instance.operations, instance.observations = Lock(), Lock()
    instance.paths = AdaptivePaths()
    instance.paths.current = (0, "reality")
    instance.processes = [SimpleNamespace(poll=lambda: None)]
    instance.metrics_generation = service.MetricsGeneration(instance.processes[0], "synthetic")
    instance.guard = SimpleNamespace(active=lambda: True, lan_routes=lambda: [])
    instance.naive = None
    instance.last_health = 0.0
    instance.last_metrics = None
    instance.metric_offsets = (0, 0)
    observations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        instance, "persist", lambda: observations.append(copy.deepcopy(instance.state))
    )
    starts: list[tuple[int, str, str]] = []

    def start_path(profile: dict[str, Any], endpoint: int, transport: str, udp: str) -> None:
        starts.append((endpoint, transport, udp))
        instance.state["snapshot"].update(transport=transport, udp_transport=udp)

    monkeypatch.setattr(instance, "_start_path", start_path)
    monkeypatch.setattr(service, "monotonic", lambda: clock[0])
    # UDP/metrics and OS/process startup are explicit external boundaries. The
    # trace/DNS/bulk HTTPS, wire parser, health decisions and monitor are real.
    monkeypatch.setattr(health, "udp_check", lambda: True)
    metric_calls: list[str] = []

    def counters(token: str, port: int) -> dict[str, int]:
        metric_calls.append(token)
        return {"rx_bytes": 100, "tx_bytes": 50}

    monkeypatch.setattr(service, "metrics", counters)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        address = listener.getsockname()

        def serve() -> None:
            try:
                while not stop_fixture.is_set():
                    try:
                        peer, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with peer, tls.wrap_socket(peer, server_side=True) as secure:
                        reader = secure.makefile("rb")
                        path = reader.readline().decode().split()[1]
                        headers = {}
                        while line := reader.readline().strip():
                            name, value = line.split(b":", 1)
                            headers[name.lower()] = value.strip()
                        data = reader.read(int(headers.get(b"content-length", b"0")))
                        requests.append(path)
                        if path.startswith("/dns-query") and fail_dns.is_set():
                            secure.sendall(
                                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n100\r\npartial DNS"
                            )
                            reader.close()
                            continue
                        if path.startswith("/dns-query"):
                            raw = parse_qs(urlsplit(path).query)["dns"][0]
                            query = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                            body = (
                                query[:2]
                                + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
                                + query[12:]
                                + b"\xc0\x0c"
                                + struct.pack("!HHIH", 1, 1, 60, 4)
                                + socket.inet_aton("104.16.132.229")
                            )
                        elif path.startswith("/__down"):
                            body = b"x" * health.BULK_BYTES
                        elif path.startswith("/__up"):
                            uploaded.append(len(data))
                            body = b"ok"
                        else:
                            body = b"ip=198.51.100.99\n"
                        secure.sendall(
                            f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                            + body
                        )
                        reader.close()
            except BaseException as exc:
                fixture_errors.append(exc)

        fixture = Thread(target=serve, daemon=True)
        fixture.start()
        monkeypatch.setattr(
            health, "socks_connect", lambda host, port: socket.create_connection(address, timeout=3)
        )
        monkeypatch.setattr(
            ssl, "create_default_context", lambda: original_context(cafile=str(pem))
        )

        def monitor() -> None:
            try:
                instance.monitor()
            except BaseException as exc:
                monitor_errors.append(exc)

        worker = Thread(target=monitor, daemon=True)
        worker.start()
        gate.ready.get(timeout=5)

        def tick(seconds: float) -> None:
            clock[0] += seconds
            gate.permit.release()
            gate.ready.get(timeout=5)
            assert worker.is_alive() and not monitor_errors

        try:
            tick(16)
            assert instance.state["state"] == "reconnecting"
            assert instance.state["snapshot"]["dns_ok"] is False
            assert instance.paths.failures == 1
            assert not metric_calls
            tick(16)
            assert instance.state["state"] == "blocked"
            assert instance.state["snapshot"]["dns_ok"] is False
            assert (0, "hysteria2", "hysteria2") in starts
            assert instance.guard.active()
            fail_dns.clear()
            tick(121)  # candidate cooldown elapsed; next normal monitor iteration
            assert instance.state["state"] == "connected"
            assert all(
                instance.state["snapshot"][field] for field in ("tcp_ok", "dns_ok", "bulk_ok")
            )
            assert instance.state["snapshot"]["external_ip"] == "198.51.100.99"
            assert metric_calls and uploaded and all(size == health.BULK_BYTES for size in uploaded)
            tick(16)
            assert instance.state["state"] == "connected" and len(metric_calls) == 2
            assert not monitor_errors
            (tmp_path / "monitor-results.json").write_text(
                json.dumps(
                    {
                        "states": [item["state"] for item in observations],
                        "dns_flags": [item["snapshot"]["dns_ok"] for item in observations],
                        "starts": starts,
                        "requests": requests,
                        "uploaded": uploaded,
                        "metrics_iterations": len(metric_calls),
                        "monitor_alive_before_controlled_shutdown": worker.is_alive(),
                    },
                    indent=2,
                )
            )
        finally:
            gate.done.set()
            gate.permit.release()
            worker.join(timeout=3)
            stop_fixture.set()
            fixture.join(timeout=2)
        assert not worker.is_alive() and not fixture_errors and not monitor_errors
