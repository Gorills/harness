"""Bounded health I/O and ephemeral verified-path hints; no OS tunnel or VPS."""

from __future__ import annotations

import base64
import copy
import http.server
import importlib.util
import json
import os
import socket
import ssl
import struct
import subprocess
import time
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Lock, Thread
from threading import enumerate as threads
from types import SimpleNamespace
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlsplit

import pytest

import harness.tunnel_health as health
import harness.tunnel_service as service
from harness.tunnel_config import AdaptivePaths, PathResult, runtime_settings
from harness.tunnel_platform import PlatformError


class HealthMeasurement(TypedDict):
    phase: str
    seconds: float
    peak_tls: int


class SerialPool:
    """Same workload without overlap; exact old source can be provided for evidence."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> SerialPool:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def submit(self, function: Any) -> Future[Any]:
        result: Future[Any] = Future()
        try:
            result.set_result(function())
        except Exception as error:
            result.set_exception(error)
        return result


def test_parallel_health_controlled_tls_before_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem, key = tmp_path / "tls.pem", tmp_path / "tls.key"
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
            "/CN=www.cloudflare.com",
            "-addext",
            "subjectAltName=DNS:www.cloudflare.com,DNS:cloudflare-dns.com,DNS:api.ipify.org,DNS:speed.cloudflare.com,DNS:one.one.one.one",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(pem, key)
    original_context = ssl.create_default_context
    monkeypatch.setattr(ssl, "create_default_context", lambda: original_context(cafile=str(pem)))
    lock = Lock()
    active = peak = 0
    delay = 0.15
    uploads: list[int] = []
    downloads: list[int] = []
    worker_ids: set[int] = set()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.exchange()

        def do_POST(self) -> None:
            self.exchange()

        def exchange(self) -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                time.sleep(delay)
                if self.path.startswith("/dns-query"):
                    raw = parse_qs(urlsplit(self.path).query)["dns"][0]
                    query = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                    body = query[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + query[12:]
                    body += (
                        b"\xc0\x0c"
                        + struct.pack("!HHIH", 1, 1, 60, 4)
                        + socket.inet_aton("104.16.132.229")
                    )
                elif self.path.startswith("/__down"):
                    body = b"x" * health.BULK_BYTES
                    downloads.append(len(body))
                elif self.path.startswith("/__up"):
                    uploads.append(len(data))
                    body = b"ok"
                else:
                    body = b"ip=198.51.100.99\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    active -= 1

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = ("127.0.0.1", server.server_port)

    def connect(host: str, port: int) -> socket.socket:
        from threading import get_ident

        worker_ids.add(get_ident())
        return socket.create_connection(address, timeout=3)

    def udp() -> bool:
        time.sleep(delay)
        return True  # explicit UDP boundary; actual STUN/engine routing is tested separately

    baseline_path = os.environ.get("HARNESS_TEST_HEALTH_BASELINE")
    baseline: Any = health
    if baseline_path:
        spec = importlib.util.spec_from_file_location("health_before", baseline_path)
        assert spec and spec.loader
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
    measurements: list[HealthMeasurement] = []
    try:
        for label, module in (("before", baseline), ("after", health)):
            peak = 0
            worker_ids.clear()
            with monkeypatch.context() as scoped:
                scoped.setattr(module, "socks_connect", connect)
                scoped.setattr(module, "udp_check", udp)
                if label == "before" and not baseline_path:
                    scoped.setattr(health, "ThreadPoolExecutor", SerialPool)
                started = time.monotonic()
                result, external = module.probe(0, "reality", bulk=True)
                elapsed = time.monotonic() - started
            assert result.usable and result.udp_ok and external == "198.51.100.99"
            assert active == 0 and len(worker_ids) <= 5
            measurements.append({"phase": label, "seconds": elapsed, "peak_tls": peak})
        assert measurements[0]["peak_tls"] == 1
        assert measurements[1]["peak_tls"] >= 2
        assert measurements[1]["seconds"] < measurements[0]["seconds"] * 0.8
        assert uploads == downloads == [health.BULK_BYTES, health.BULK_BYTES]
        assert not any(thread.name.startswith("harness-health") for thread in threads())
        (tmp_path / "health-timing.json").write_text(
            json.dumps(
                {
                    "measurements": measurements,
                    "exact_old_module": bool(baseline_path),
                    "delay_per_exchange_seconds": delay,
                    "uploads": uploads,
                    "downloads": downloads,
                    "tls_verified": True,
                    "dns_wire_proof": True,
                    "real_vps_benchmark": False,
                },
                indent=2,
            )
        )
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


def test_https_fallback_timer_excludes_failed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]

    def request(url: str, **kwargs: Any) -> tuple[int, bytes]:
        if "cdn-cgi" in url:
            clock[0] += 2
            raise OSError("first identity timed out")
        clock[0] += 0.05
        return 200, b"198.51.100.99"

    monkeypatch.setattr(health, "request", request)
    monkeypatch.setattr(health, "monotonic", lambda: clock[0])
    monkeypatch.setattr(health, "dns_check", lambda: True)
    monkeypatch.setattr(health, "udp_check", lambda: True)
    result, _ = health.probe(0, "reality", bulk=False)
    assert result.usable and result.latency_ms == pytest.approx(50)


@pytest.mark.parametrize("failure", ["identity", "dns", "download", "upload", "udp"])
def test_parallel_proof_negatives_and_dead_identity_byte_budget(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls: list[str] = []

    def request(url: str, *, body: bytes | None = None, **kwargs: Any) -> tuple[int, bytes]:
        calls.append(url)
        if "__down" in url:
            return 200, b"x" * (1 if failure == "download" else health.BULK_BYTES)
        if "__up" in url:
            assert body is not None and len(body) == health.BULK_BYTES
            return (500 if failure == "upload" else 200), b"ok"
        return (500, b"") if failure == "identity" else (200, b"ip=198.51.100.99\n")

    monkeypatch.setattr(health, "request", request)
    monkeypatch.setattr(health, "dns_check", lambda: failure != "dns")
    monkeypatch.setattr(health, "udp_check", lambda: failure != "udp")
    result, _ = health.probe(0, "reality")
    assert result.usable is (failure == "udp")
    assert result.udp_ok is (failure != "udp")
    if failure == "identity":
        assert not any("__up" in url or "__down" in url for url in calls)
    else:
        assert sum("__up" in url for url in calls) == sum("__down" in url for url in calls) == 1


def test_unexpected_worker_error_joins_all_before_return_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_started, release, returned = Event(), Event(), Event()
    fail = [True]
    errors: list[Exception] = []

    def request(url: str, **kwargs: Any) -> tuple[int, bytes]:
        if "__up" in url:
            upload_started.set()
            assert release.wait(3)
            return 200, b"ok"
        if "__down" in url:
            assert upload_started.wait(3)
            if fail[0]:
                raise RuntimeError("programmer error remains visible")
            return 200, b"x" * health.BULK_BYTES
        return 200, b"ip=198.51.100.99\n"

    monkeypatch.setattr(health, "request", request)
    monkeypatch.setattr(health, "dns_check", lambda: True)
    monkeypatch.setattr(health, "udp_check", lambda: True)

    def run() -> None:
        try:
            health.probe(0, "reality")
        except Exception as error:
            errors.append(error)
        finally:
            returned.set()

    worker = Thread(target=run)
    worker.start()
    try:
        assert upload_started.wait(3)
        assert not returned.wait(0.1)
        assert sum(thread.name.startswith("harness-health") for thread in threads()) <= 5
    finally:
        release.set()
        worker.join(timeout=3)
    assert returned.is_set() and isinstance(errors[0], RuntimeError)
    assert not any(thread.name.startswith("harness-health") for thread in threads())
    fail[0] = False
    assert health.probe(0, "reality")[0].usable


def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    service.ServiceRuntime,
    dict[str, Any],
    list[tuple[int, str, str]],
    list[bool],
    list[dict[str, Any]],
    list[float],
    dict[str, bool],
]:
    instance = service.ServiceRuntime.__new__(service.ServiceRuntime)
    instance.owner_uid = 1000
    monkeypatch.setattr(service, "system_probe", lambda uid, ip: (True, True))
    instance.root = tmp_path
    profile = {
        "settings": runtime_settings(
            {
                "name": "Fixture",
                "endpoints": [{"host": "203.0.113.10"}, {"host": "198.51.100.20"}],
                "lan_bypass": False,
            }
        ),
        "revision": 1,
        "secrets": {"endpoints": [{"prepared": True}, {"prepared": True}]},
    }
    instance.state = {
        "profiles": {"fixture": profile},
        "active": None,
        "desired": False,
        "state": "disconnected",
        "protection": "inactive",
        "snapshot": {},
    }
    instance.path_hints = {}
    instance.paths = AdaptivePaths()
    instance.processes = []
    instance.last_metrics = None
    instance.naive = None
    instance.manifest = {"version": "fixture", "system_packages": {}}
    armed = [False]
    calls: list[tuple[int, str, str]] = []
    probes: list[bool] = []
    snapshots: list[dict[str, Any]] = []
    clock = [1000.0]
    mode = {"good_zero": False, "bad_hint": False, "udp_bad": False}

    def arm(routes: list[str]) -> None:
        armed[0] = True

    instance.guard = SimpleNamespace(
        arm=arm,
        check_conflicts=lambda: None,
        underlay_interface=lambda host: "eth0",
        active=lambda: armed[0],
        disarm=lambda: armed.__setitem__(0, False),
        lan_routes=lambda: [],
    )

    def start(profile: Any, index: int, tcp: str, udp: str) -> None:
        assert armed[0] and instance.state["protection"] == "active"
        assert not any(thread.name.startswith("harness-health") for thread in threads())
        calls.append((index, tcp, udp))
        instance.processes = [SimpleNamespace(poll=lambda: None)]
        instance.state["snapshot"].update(
            transport=tcp,
            udp_transport="hysteria2" if udp == "hysteria2" else "reality-udp-over-tcp",
        )

    def probe(index: int, transport: str, *, bulk: bool) -> tuple[PathResult, str]:
        assert armed[0]
        probes.append(bulk)
        good = (index == 1 and not mode["bad_hint"]) or (mode["good_zero"] and index == 0)
        udp = not mode["udp_bad"] and (
            mode["good_zero"] or instance.state["snapshot"]["udp_transport"] != "hysteria2"
        )
        return PathResult(index, transport, good, good, good, udp, 30), "203.0.113.200"

    monkeypatch.setattr(instance, "_start_path", start)
    monkeypatch.setattr(instance, "_stop_engines", lambda: instance._invalidate_rates())
    monkeypatch.setattr(
        instance, "persist", lambda: snapshots.append(copy.deepcopy(instance.state))
    )
    monkeypatch.setattr(service, "probe", probe)
    monkeypatch.setattr(service, "monotonic", lambda: clock[0])
    return instance, profile, calls, probes, snapshots, clock, mode


@pytest.mark.parametrize(
    "enabled,results,bulk,expected_restart,usable",
    [
        (False, [False], True, None, True),
        (False, [True, True], True, True, True),
        (False, [True], False, None, True),
        (True, [False, False], False, False, True),
        (False, [True, False], True, True, False),
    ],
)
def test_ipv6_capability_is_verified_before_promotion_and_on_active_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    results: list[bool],
    bulk: bool,
    expected_restart: bool | None,
    usable: bool,
) -> None:
    instance, profile, *_ = runtime(tmp_path, monkeypatch)
    instance.state["active"] = "fixture"
    instance.ipv6_available = enabled
    instance.state["snapshot"] = {"udp_transport": "hysteria2"}
    responses = iter(results)
    restarted = []

    def probe(index: int, transport: str, *, bulk: bool) -> tuple[PathResult, str]:
        return PathResult(
            index, transport, True, True, True, ipv6_ok=next(responses)
        ), "203.0.113.2"

    def start(candidate: Any, index: int, tcp: str, udp: str, *, ipv6_available: bool) -> None:
        assert candidate is profile and (index, tcp, udp) == (0, "reality", "hysteria2")
        restarted.append(ipv6_available)
        instance.ipv6_available = ipv6_available

    monkeypatch.setattr(service, "probe", probe)
    monkeypatch.setattr(instance, "_start_path", start)
    result, address = instance._probe_path(0, "reality", bulk=bulk)
    assert result.usable is usable
    assert address == ("203.0.113.2" if usable else "")
    assert restarted == ([] if expected_restart is None else [expected_restart])


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b"ip=2001:db8::2\n", True),
        (200, b"ip=203.0.113.2\n", False),
        (403, b"ip=2001:db8::2\n", False),
        (200, b"not a trace", False),
    ],
)
def test_ipv6_probe_requires_valid_tls_http_and_ipv6_egress(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes, expected: bool
) -> None:
    def request(url: str, **kwargs: Any) -> tuple[int, bytes]:
        assert url.startswith("https://[2606:4700:4700::1111]/")
        assert kwargs["server_name"] == "one.one.one.one"
        return status, body

    monkeypatch.setattr(health, "request", request)
    assert health.ipv6_check() is expected


@pytest.mark.parametrize("system_result", [(False, False), (True, False)])
def test_system_path_failure_blocks_even_when_all_proxy_probes_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_result: tuple[bool, bool]
) -> None:
    instance, _, _, _, snapshots, _, mode = runtime(tmp_path, monkeypatch)
    mode["good_zero"] = True
    monkeypatch.setattr(service, "system_probe", lambda uid, ip: system_result)
    instance._connect("fixture")
    assert instance.state["state"] == "blocked"
    assert instance.guard.active() and not instance.path_hints
    assert not any(snapshot["state"] == "connected" for snapshot in snapshots)
    # Once the app path works, the ordinary connect operation can recover.
    monkeypatch.setattr(service, "system_probe", lambda uid, ip: (True, True))
    instance._connect("fixture")
    assert instance.state["state"] == "connected"


def test_foreign_vpn_conflict_preserves_working_network_and_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.tunnel_platform import VPNConflict

    instance, _, calls, _, snapshots, _, _ = runtime(tmp_path, monkeypatch)
    before = copy.deepcopy(instance.state)

    def conflict() -> None:
        raise VPNConflict("fixture foreign VPN")

    instance.guard.check_conflicts = conflict
    with pytest.raises(VPNConflict):
        instance._connect("fixture")
    assert instance.state == before
    assert not instance.guard.active() and not calls and not snapshots


def test_unavailable_backup_route_does_not_reject_usable_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.tunnel_platform import PlatformError

    instance, _, _, _, _, _, mode = runtime(tmp_path, monkeypatch)
    mode["good_zero"] = True

    def underlay(host: str) -> str:
        if host == "198.51.100.20":
            raise PlatformError("fixture unavailable backup route")
        return "eth0"

    instance.guard.underlay_interface = underlay
    instance._connect("fixture")
    assert instance.state["state"] == "connected"


def test_verified_hint_repeat_and_udp_fallback_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, calls, probes, snapshots, clock, _ = runtime(tmp_path, monkeypatch)
    instance._connect("fixture")
    assert calls == [
        (0, "reality", "hysteria2"),
        (0, "hysteria2", "hysteria2"),
        (1, "reality", "hysteria2"),
        (1, "reality", "reality"),
    ]
    assert instance.state["state"] == "connected" and all(probes)
    expiry = instance.path_hints["fixture"].expires
    for snapshot in snapshots:
        if snapshot["state"] == "connected":
            assert all(snapshot["snapshot"][field] for field in ("tcp_ok", "dns_ok", "bulk_ok"))
    instance._disconnect()
    calls.clear()
    instance._connect("fixture")
    assert calls == [(1, "reality", "reality")] and all(probes)
    clock[0] += 15
    instance._check(bulk=False)
    assert instance.path_hints["fixture"].expires == expiry  # no indefinite HY suppression
    instance._disconnect()
    clock[0] = expiry + 1
    calls.clear()
    instance._connect("fixture")
    assert calls[0] == (0, "reality", "hysteria2")
    assert (1, "reality", "hysteria2") in calls
    service.ServiceRuntime.persist(instance)
    durable = json.loads((tmp_path / "applied.json").read_text())
    assert set(durable) == set(instance.state) and "path_hints" not in durable


def test_failed_hint_falls_back_and_guard_remains_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, calls, probes, _, _, mode = runtime(tmp_path, monkeypatch)
    instance._connect("fixture")
    instance._disconnect()
    mode.update(bad_hint=True, good_zero=True)
    calls.clear()
    instance._connect("fixture")
    assert calls == [(1, "reality", "reality"), (0, "reality", "hysteria2")]
    assert instance.path_hints["fixture"].endpoint == 0 and all(probes)
    instance._disconnect()
    monkeypatch.setattr(instance.guard, "active", lambda: False)
    calls.clear()
    with pytest.raises(PlatformError, match="Защита"):
        instance._connect("fixture")
    assert not calls and instance.state["state"] == "disconnected"


def test_udp_degraded_tcp_never_caches_unverified_udp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _, calls, _, _, _, mode = runtime(tmp_path, monkeypatch)
    instance._connect("fixture")
    assert "fixture" in instance.path_hints
    mode["udp_bad"] = True
    instance._check(bulk=False)
    assert instance.state["state"] == "connected"
    assert instance.state["snapshot"]["udp_ok"] is False
    assert "fixture" not in instance.path_hints
    instance._disconnect()
    calls.clear()
    instance._connect("fixture")
    assert instance.state["state"] == "connected"
    assert instance.state["snapshot"]["udp_ok"] is False
    assert (1, "reality", "hysteria2") in calls  # no bad-UDP hint skips a fresh attempt
    assert "fixture" not in instance.path_hints


def test_hint_binding_prepare_remove_invalidation_and_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, profile, _, _, _, clock, _ = runtime(tmp_path, monkeypatch)
    instance._connect("fixture")
    instance._disconnect()
    profile["settings"]["endpoints"][0]["host"] = "203.0.113.55"
    assert instance._candidates("fixture", profile)[0] == (0, "reality", "hysteria2")
    assert "fixture" not in instance.path_hints
    instance._remember_path("fixture", profile, 1, "reality")
    payload = {"settings": profile["settings"], "revision": 2}
    instance._prepare("fixture", payload)  # already prepared; no remote bootstrap needed
    assert "fixture" not in instance.path_hints
    instance._remember_path("fixture", profile, 1, "reality")
    monkeypatch.setattr(service, "maintain", lambda *args: None)
    instance._remove("fixture", {})
    assert "fixture" not in instance.path_hints and "fixture" not in instance.state["profiles"]
    for index in range(35):
        clock[0] += 1
        instance._remember_path(str(index), profile, 1, "reality")
    assert len(instance.path_hints) == service.MAX_PATH_HINTS
