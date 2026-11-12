"""Metrics HTTP recovery and per-managed-launch credentials; loopback fixtures only."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Semaphore, Thread
from types import SimpleNamespace
from typing import Any

import pytest

import harness.tunnel_config as compiler
import harness.tunnel_health as health
import harness.tunnel_service as service
from harness.tunnel_config import AdaptivePaths, PathResult, runtime_settings
from harness.tunnels import TunnelError


class Gate:
    def __init__(self) -> None:
        self.ready: Queue[None] = Queue()
        self.permit = Semaphore(0)
        self.done = Event()

    def wait(self, timeout: float) -> bool:
        self.ready.put(None)
        if not self.permit.acquire(timeout=5):
            raise TimeoutError("fixture iteration not scheduled")
        return self.done.is_set()


def monitor_runtime(root: Path) -> service.ServiceRuntime:
    instance = service.ServiceRuntime.__new__(service.ServiceRuntime)
    instance.path_hints = {}
    instance.owner_uid = 1000
    instance.root = root
    settings = runtime_settings(
        {
            "name": "Fixture",
            "endpoints": [{"host": "203.0.113.10"}, {"host": "198.51.100.20"}],
            "lan_bypass": False,
        }
    )
    instance.state = {
        "profiles": {
            "fixture": {"settings": settings, "revision": 1, "secrets": {"endpoints": [{}, {}]}}
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
            "rx_bps": 999,
            "tx_bps": 888,
        },
    }
    instance.operations, instance.observations = Lock(), Lock()
    instance.processes = [SimpleNamespace(poll=lambda: None)]
    instance.metrics_generation = service.MetricsGeneration(
        instance.processes[0], "CURRENT_RUNTIME_CANARY"
    )
    instance.paths = AdaptivePaths()
    instance.paths.current = (0, "reality")
    instance.guard = SimpleNamespace(active=lambda: True, lan_routes=lambda: [])
    instance.naive = None
    instance.metric_offsets = (1000, 2000)
    instance.last_metrics = None
    instance.last_health = 1000.0
    return instance


def test_metrics_truncated_http_keeps_health_failover_and_recovers_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.storage import connect_database, initialize_database
    from harness.tunnel_control import NetworkServiceClient, TunnelController
    from harness.tunnels import get_profile, save_profile

    instance = monitor_runtime(tmp_path)
    monkeypatch.setattr(service, "system_probe", lambda uid, ip: (True, True))
    database = tmp_path / "projection.db"
    initialize_database(database)
    connection = connect_database(database)
    save_profile(connection, "fixture", 0, instance.state["profiles"]["fixture"]["settings"])
    connection.close()

    class PollOnce:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    class LocalStatusClient:
        def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert operation == "status"
            return NetworkServiceClient._result(instance.public(payload["profile_id"]))

    controller = TunnelController(database, service=LocalStatusClient())  # type: ignore[arg-type]
    gate = Gate()
    instance.stop = gate  # type: ignore[assignment]
    clock = [1001.0]
    monkeypatch.setattr(service, "monotonic", lambda: clock[0])
    unhealthy = [False]
    all_bad = [False]
    probes: list[tuple[int, str, bool]] = []
    starts: list[tuple[int, str]] = []

    def probe(endpoint: int, transport: str, *, bulk: bool) -> tuple[PathResult, str]:
        probes.append((endpoint, transport, bulk))
        good = (not unhealthy[0] or endpoint == 1) and not all_bad[0]
        return PathResult(
            endpoint, transport, good, good, good, good
        ), "203.0.113.200" if good else ""

    monkeypatch.setattr(service, "probe", probe)

    def start(profile: dict[str, Any], endpoint: int, transport: str, udp: str) -> None:
        starts.append((endpoint, transport))
        instance.metric_offsets = (
            instance.state["snapshot"].get("rx_bytes", 0),
            instance.state["snapshot"].get("tx_bytes", 0),
        )
        instance.last_metrics = None
        instance.metrics_generation = service.MetricsGeneration(
            instance.processes[0], "NEW_GENERATION_CANARY"
        )
        instance.state["snapshot"].update(transport=transport, udp_transport=udp)

    monkeypatch.setattr(instance, "_start_path", start)
    response_mode = ["good"]
    raw_counters = [(10, 20)]
    requests: list[str] = []
    stop_server = Event()
    errors: list[BaseException] = []
    worker_errors: list[BaseException] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        monkeypatch.setattr(service, "METRICS_PORT", listener.getsockname()[1])

        def serve() -> None:
            try:
                while not stop_server.is_set():
                    try:
                        peer, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with peer:
                        reader = peer.makefile("rb")
                        requests.append(reader.readline().decode().strip())
                        headers = {}
                        while line := reader.readline().strip():
                            name, value = line.split(b":", 1)
                            headers[name.lower()] = value.strip()
                        assert instance.metrics_generation
                        assert (
                            headers[b"authorization"]
                            == ("Bearer " + instance.metrics_generation.token).encode()
                        )
                        if response_mode[0] == "truncated":
                            peer.sendall(
                                b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n100\r\n{"downloadTotal":12'
                            )
                        else:
                            body = json.dumps(
                                {
                                    "downloadTotal": raw_counters[0][0],
                                    "uploadTotal": raw_counters[0][1],
                                    "connections": [{"private": "NEVER_DISCLOSE_BODY_CANARY"}],
                                }
                            ).encode()
                            peer.sendall(
                                f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                                + body
                            )
                        reader.close()
            except BaseException as exc:
                errors.append(exc)

        server = Thread(target=serve, daemon=True)
        server.start()

        def monitor() -> None:
            try:
                instance.monitor()
            except BaseException as exc:
                worker_errors.append(exc)

        worker = Thread(target=monitor, daemon=True)
        worker.start()
        gate.ready.get(timeout=5)
        evidence: list[dict[str, Any]] = []

        def tick(seconds: float) -> None:
            clock[0] += seconds
            gate.permit.release()
            gate.ready.get(timeout=5)
            assert worker.is_alive() and not worker_errors
            assert instance.operations.acquire(blocking=False)
            instance.operations.release()
            public = json.loads(json.dumps(instance.public("fixture")))
            controller._stop = PollOnce()  # type: ignore[assignment]
            controller._poll()  # actual typed result -> poll -> SQLite snapshot replacement
            connection = connect_database(database)
            try:
                durable = get_profile(connection, "fixture")["runtime"]
                assert durable["state"] == public["state"]
                assert durable["snapshot"] == public["snapshot"]
                totals = connection.execute(
                    "SELECT coalesce(sum(rx_bytes),0),coalesce(sum(tx_bytes),0) FROM tunnel_metrics"
                ).fetchone()
                assert list(totals) == [
                    public["snapshot"]["rx_bytes"],
                    public["snapshot"]["tx_bytes"],
                ]
            finally:
                connection.close()
            if public["state"] != "connected":
                assert all(key not in public["snapshot"] for key in ("rx_bps", "tx_bps"))
                journal = json.loads((tmp_path / "applied.json").read_text())
                assert all(key not in journal["snapshot"] for key in ("rx_bps", "tx_bps"))
                assert instance.last_metrics is None
            evidence.append(public)

        try:
            tick(0)
            assert instance.state["snapshot"]["rx_bytes"] == 1010
            assert "rx_bps" not in instance.state["snapshot"]
            response_mode[0] = "truncated"
            tick(1)
            snapshot = instance.state["snapshot"]
            assert instance.state["state"] == "connected" and snapshot["dns_ok"] is True
            assert snapshot["last_error"] == service.METRICS_UNAVAILABLE
            assert "rx_bps" not in snapshot and "tx_bps" not in snapshot
            assert snapshot["rx_bytes"] == 1010 and not probes
            response_mode[0] = "good"
            raw_counters[0] = (30, 40)
            tick(2)
            assert snapshot["rx_bytes"] == 1030 and snapshot["tx_bytes"] == 2040
            assert (
                snapshot["rx_bps"] == 6 and snapshot["tx_bps"] == 6 and snapshot["last_error"] == ""
            )
            assert not probes  # a missing counter sample never demands bulk/health by itself
            unhealthy[0] = True
            tick(16)
            assert instance.state["state"] == "reconnecting"
            raw_counters[0] = (5, 7)
            tick(16)
            assert (
                instance.paths.current == (1, "reality") and instance.state["state"] == "connected"
            )
            assert snapshot["rx_bytes"] == 1035 and snapshot["tx_bytes"] == 2047
            assert "rx_bps" not in snapshot and "tx_bps" not in snapshot
            raw_counters[0] = (25, 37)
            tick(1)
            assert snapshot["rx_bps"] == 20 and snapshot["tx_bps"] == 30
            before = len(starts)
            raw_counters[0] = (57, 85)
            tick(16)
            assert len(starts) == before and probes[-1] == (1, "reality", False)
            assert snapshot["rx_bps"] == 2 and snapshot["tx_bps"] == 3
            last_totals = (snapshot["rx_bytes"], snapshot["tx_bytes"])
            all_bad[0] = True
            tick(16)  # first failed health check; no old rate or interval survives
            assert instance.state["state"] == "reconnecting"
            tick(16)  # exhausted candidates
            assert instance.state["state"] == "blocked"
            requests_before, starts_before = len(requests), len(starts)
            tick(16)  # blocked cooldown: no metric request or extra candidate start
            assert instance.state["state"] == "blocked"
            assert (len(requests), len(starts)) == (requests_before, starts_before)
            assert (snapshot["rx_bytes"], snapshot["tx_bytes"]) == last_totals
            all_bad[0] = False
            unhealthy[0] = False
            raw_counters[0] = (0, 0)
            tick(121)
            assert instance.state["state"] == "connected"
            assert "rx_bps" not in snapshot and "tx_bps" not in snapshot
            assert (snapshot["rx_bytes"], snapshot["tx_bytes"]) == last_totals
            tick(1)  # a valid current-generation zero pair is a real zero rate
            assert snapshot["rx_bps"] == snapshot["tx_bps"] == 0
            assert "NEVER_DISCLOSE_BODY_CANARY" not in json.dumps(evidence)
            assert "CANARY" not in json.dumps(evidence)
            (tmp_path / "metrics-recovery-results.json").write_text(
                json.dumps(
                    {
                        "states": [item["state"] for item in evidence],
                        "snapshots": [item["snapshot"] for item in evidence],
                        "probes": probes,
                        "starts": starts,
                        "actual_metrics_http_requests": len(requests),
                        "worker_alive_before_shutdown": worker.is_alive(),
                        "lock_released_each_iteration": True,
                        "typed_controller_sqlite_exact_each_iteration": True,
                        "journal_cooldown_rates_unavailable": True,
                        "aggregate_offsets_and_minute_deltas_preserved": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            gate.done.set()
            gate.permit.release()
            worker.join(timeout=3)
            stop_server.set()
            server.join(timeout=2)
            controller._executor.shutdown(wait=True, cancel_futures=True)
        assert not worker.is_alive() and not worker_errors and not errors


@pytest.mark.parametrize(
    "body",
    [
        b"not-json-CANARY",
        b"[]",
        b'{"downloadTotal":true,"uploadTotal":2}',
        b'{"downloadTotal":-1,"uploadTotal":2}',
        b'{"downloadTotal":1}',
        b'{"downloadTotal":1,"uploadTotal":2}',
    ],
)
def test_actual_metrics_http_bounds_and_typed_aggregate(body: bytes) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)

        def serve() -> None:
            peer, _ = listener.accept()
            with peer:
                reader = peer.makefile("rb")
                reader.readline()
                while reader.readline().strip():
                    pass
                peer.sendall(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
                )
                reader.close()

        server = Thread(target=serve, daemon=True)
        server.start()
        try:
            if body == b'{"downloadTotal":1,"uploadTotal":2}':
                assert health.metrics("synthetic", listener.getsockname()[1]) == {
                    "rx_bytes": 1,
                    "tx_bytes": 2,
                }
            else:
                with pytest.raises(TunnelError) as failure:
                    health.metrics("synthetic", listener.getsockname()[1])
                assert "CANARY" not in str(failure.value)
        finally:
            server.join(timeout=3)


@pytest.mark.skipif(
    not os.environ.get("HARNESS_TEST_ENGINE"), reason="requires admitted scratch engine"
)
def test_actual_engine_retired_metrics_token_replay_and_generation_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actual _start_path/engine/API; loopback SOCKS replaces TUN/data transports."""
    executable = Path(os.environ["HARNESS_TEST_ENGINE"])

    def port() -> int:
        with socket.socket() as peer:
            peer.bind(("127.0.0.1", 0))
            return int(peer.getsockname()[1])

    api_port, socks_port = port(), port()
    monkeypatch.setattr(service, "METRICS_PORT", api_port)
    monkeypatch.setattr(compiler, "METRICS_PORT", api_port)
    instance = monitor_runtime(tmp_path)
    instance.engine = executable
    instance.naive = None
    instance.runtime_gid = os.getgid()
    instance.configuration = {
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
        "owner_uid": os.getuid(),
    }
    # Cache ownership is checked separately; this fixture isolates API generations.
    monkeypatch.setattr(service, "fakeip_cache_file", lambda *args: tmp_path / "cache.db")
    instance.processes = []
    instance.metrics_generation = None
    instance.stop = Event()
    private = instance._new_endpoint()
    private.update(
        certificate=subprocess.check_output(
            [str(executable), "generate", "tls-keypair", "fixture.invalid"]
        ).decode(),
        public_certificate=False,
        prepared=True,
    )
    profile = instance.state["profiles"]["fixture"]
    profile["settings"]["endpoints"] = [profile["settings"]["endpoints"][0]]
    profile["secrets"] = {"endpoints": [private]}
    instance.state["snapshot"].update(rx_bytes=100, tx_bytes=200)
    instance.guard = SimpleNamespace(
        active=lambda: True,
        lan_routes=lambda: [],
        engine_command=lambda executable, args, gid: [str(executable), *args],
        underlay_interface=lambda host: "lo",
        configure_dns=lambda: None,
    )
    domain = "synthetic-private-destination.invalid"
    original_compiler = compiler.compile_client
    compiled_tokens: list[str] = []

    def config(*args: Any, **kwargs: Any) -> dict[str, Any]:
        full = original_compiler(*args, **kwargs)
        compiled_tokens.append(full["experimental"]["clash_api"]["secret"])
        return {
            "log": {"disabled": True},
            "dns": {
                "servers": [
                    {"type": "hosts", "tag": "local", "predefined": {domain: ["127.0.0.1"]}}
                ],
                "final": "local",
            },
            "inbounds": [
                {
                    "type": "socks",
                    "tag": "synthetic-in",
                    "listen": "127.0.0.1",
                    "listen_port": socks_port,
                }
            ],
            "outbounds": [{"type": "direct", "tag": "direct", "domain_resolver": "local"}],
            "route": {"final": "direct", "default_domain_resolver": "local"},
            "experimental": full["experimental"],
        }

    monkeypatch.setattr(service, "compile_client", config)

    def runtime_file(name: str, value: dict[str, Any]) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    monkeypatch.setattr(instance, "_runtime_file", runtime_file)
    original_metrics = health.metrics

    def api(token: str | None, method: str = "GET") -> tuple[int, Any]:
        peer = http.client.HTTPConnection("127.0.0.1", api_port, timeout=2)
        try:
            peer.request(
                method,
                "/connections",
                headers={"Authorization": "Bearer " + token} if token else {},
            )
            reply = peer.getresponse()
            data = reply.read()
            return reply.status, json.loads(data) if data else None
        finally:
            peer.close()

    def current_generation(runtime: service.ServiceRuntime) -> service.MetricsGeneration:
        generation = runtime.metrics_generation
        assert generation is not None
        return generation

    def launch(runtime: service.ServiceRuntime, transport: str) -> service.MetricsGeneration:
        runtime._start_path(profile, 0, transport, "hysteria2")
        assert runtime.metrics_generation
        for _ in range(100):
            assert runtime.metrics_generation.process.poll() is None
            try:
                if api(runtime.metrics_generation.token)[0] == 200:
                    return runtime.metrics_generation
            except OSError:
                pass
            time.sleep(0.02)
        pytest.fail("own actual engine API did not start")

    captured: list[str] = []
    all_tokens: list[str] = []
    receiver_errors: list[BaseException] = []
    restored: service.ServiceRuntime | None = None
    outcomes: dict[str, Any] = {}
    try:
        first = launch(instance, "reality")
        all_tokens.append(first.token)
        assert "rx_bps" not in instance.state["snapshot"]
        assert "tx_bps" not in instance.state["snapshot"]
        assert original_metrics(first.token, api_port) == {"rx_bytes": 0, "tx_bytes": 0}
        instance._sample_metrics()
        assert "rx_bps" not in instance.state["snapshot"]
        assert "tx_bps" not in instance.state["snapshot"]
        assert (instance.state["snapshot"]["rx_bytes"], instance.state["snapshot"]["tx_bytes"]) == (
            100,
            200,
        )
        instance._sample_metrics()
        assert instance.state["snapshot"]["rx_bps"] == instance.state["snapshot"]["tx_bps"] == 0
        outcomes["new_engine_current_rates"] = {
            "actual_raw_counters": [0, 0],
            "first_sample_unavailable": True,
            "second_valid_zero_pair": [0, 0],
            "cumulative_offsets_preserved": [100, 200],
        }
        before = dict(instance.state["snapshot"])

        def die_between_check_and_connect(token: str, selected_port: int) -> dict[str, Any]:
            first.process.terminate()
            first.process.wait(timeout=5)
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", selected_port))
                listener.listen()
                listener.settimeout(3)

                def receive() -> None:
                    try:
                        peer, _ = listener.accept()
                        with peer:
                            reader = peer.makefile("rb")
                            reader.readline()
                            while line := reader.readline().strip():
                                name, value = line.split(b":", 1)
                                if name.lower() == b"authorization":
                                    captured.append(value.strip().decode().split(" ", 1)[1])
                            body = b'{"downloadTotal":987654,"uploadTotal":987654}'
                            peer.sendall(
                                f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                                + body
                            )
                            reader.close()
                    except BaseException as exc:
                        receiver_errors.append(exc)

                receiver = Thread(target=receive, daemon=True)
                receiver.start()
                try:
                    return original_metrics(token, selected_port)
                finally:
                    receiver.join(timeout=3)

        with monkeypatch.context() as race:
            race.setattr(service, "metrics", die_between_check_and_connect)
            instance._sample_metrics()
        assert captured and not receiver_errors
        assert instance.metrics_generation is None and instance.state["state"] == "reconnecting"
        assert instance.state["snapshot"]["rx_bytes"] == before["rx_bytes"]
        assert instance.state["snapshot"]["tx_bytes"] == before["tx_bytes"]
        assert not instance.state["snapshot"]["tcp_ok"] and instance.last_health == 0
        outcomes["death_between_precheck_and_connect"] = {
            "captured_retired_credential": True,
            "fake_counters_discarded": True,
            "state": instance.state["state"],
        }
        second = launch(instance, "reality")
        all_tokens.append(second.token)
        assert api(None)[0] == 401 and api(captured[0])[0] == 401
        assert api(captured[0], "DELETE")[0] == 401
        assert original_metrics(second.token, api_port)["rx_bytes"] >= 0
        with socket.socket() as destination:
            destination.bind(("127.0.0.1", 0))
            destination.listen()
            destination.settimeout(3)
            client = health.socks_connect(
                domain, destination.getsockname()[1], proxy_port=socks_port
            )
            with client:
                client.sendall(b"SYNTHETIC_PRIVATE_FLOW")
                remote, _ = destination.accept()
                with remote:
                    assert remote.recv(100) == b"SYNTHETIC_PRIVATE_FLOW"
                    status, details = api(second.token)
                    assert status == 200 and any(
                        row["metadata"]["host"] == domain for row in details["connections"]
                    )
                    assert api(captured[0])[0] == 401
        outcomes["next_actual_engine"] = {
            "no_auth_status": 401,
            "captured_prior_get_status": 401,
            "captured_prior_delete_status": 401,
            "current_status": 200,
            "current_metadata_fixture_positive": True,
        }
        second.process.terminate()
        second.process.wait(timeout=5)
        with socket.socket() as substitute:
            substitute.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            substitute.bind(("127.0.0.1", api_port))
            substitute.listen()
            substitute.settimeout(0.2)
            instance._sample_metrics()
            with pytest.raises(TimeoutError):
                substitute.accept()  # precheck sends no credential
        third = launch(instance, "hysteria2")
        all_tokens.append(third.token)
        assert all(api(token)[0] == 401 for token in all_tokens[:-1])

        # A generation change during the call must discard even a valid old reply.
        def replace_generation_during_request(token: str, selected_port: int) -> dict[str, Any]:
            sample = original_metrics(token, selected_port)
            launch(instance, "reality")
            return {**sample, "rx_bytes": 876543, "tx_bytes": 876543}

        with monkeypatch.context() as race:
            race.setattr(service, "metrics", replace_generation_during_request)
            instance._sample_metrics()
        generation = current_generation(instance)
        all_tokens.append(generation.token)
        assert instance.state["snapshot"]["rx_bytes"] != 876543 + instance.metric_offsets[0]
        assert instance.state["state"] == "reconnecting"
        outcomes["generation_changed_during_request"] = {
            "counters_discarded": True,
            "state": "reconnecting",
        }
        observed_retired: list[bool] = []
        current_process = generation.process
        terminate = current_process.terminate

        def stop() -> None:
            observed_retired.append(instance.metrics_generation is None)
            terminate()

        with monkeypatch.context() as retirement:
            retirement.setattr(current_process, "terminate", stop)
            instance._stop_engines()
        assert observed_retired == [True]
        # Restore legacy state through the actual constructor: old profile key is
        # purged, no runtime generation restored, and the next launch rotates.
        profile["secrets"]["metrics_token"] = captured[0]
        (tmp_path / "applied.json").write_text(json.dumps(instance.state))
        (tmp_path / "service.json").write_text(
            json.dumps(
                {"runtime_uid": os.getuid(), "runtime_gid": os.getgid(), "owner_uid": os.getuid()}
            )
        )
        monkeypatch.setattr(service, "require_owned", lambda *args, **kwargs: None)
        monkeypatch.setattr(service, "verify_bundle", lambda *args, **kwargs: {"components": {}})
        monkeypatch.setattr(service, "component_path", lambda *args: executable)
        restored = service.ServiceRuntime(tmp_path, tmp_path)
        assert restored.metrics_generation is None
        assert "metrics_token" not in restored.state["profiles"]["fixture"]["secrets"]
        assert captured[0] not in (tmp_path / "applied.json").read_text()
        restored.guard = instance.guard
        monkeypatch.setattr(restored, "_runtime_file", runtime_file)
        restored_profile = restored.state["profiles"]["fixture"]
        # launch() takes the same endpoint contract; no profile token is consumed.
        profile = restored_profile
        fresh = launch(restored, "reality")
        all_tokens.append(fresh.token)
        assert all(api(token)[0] == 401 for token in all_tokens[:-1])
        assert original_metrics(fresh.token, api_port)["tx_bytes"] >= 0
        assert len(set(compiled_tokens)) == len(compiled_tokens)
        assert fresh.token not in repr(fresh)
        restored.persist()
        public_json = json.dumps(restored.public("fixture"))
        private_json = (tmp_path / "applied.json").read_text()
        assert all(token not in public_json + private_json for token in all_tokens)
        from harness.storage import connect_database, initialize_database
        from harness.tunnels import observations, observe_runtime, save_profile

        database = tmp_path / "shared.db"
        initialize_database(database)
        connection = connect_database(database)
        try:
            save_profile(
                connection,
                "fixture",
                0,
                {"name": "Synthetic", "endpoints": [{"host": "203.0.113.10"}], "exceptions": []},
            )
            public = restored.public("fixture")
            observe_runtime(
                connection, "fixture", public["state"], public["protection"], public["snapshot"]
            )
            shared = json.dumps(observations(connection, "fixture")) + "\n".join(
                connection.iterdump()
            )
            assert all(token not in shared for token in all_tokens)
        finally:
            connection.close()
        outcomes["launch_restore_and_disclosure"] = {
            "launches": len(compiled_tokens),
            "all_launch_credentials_unique": True,
            "legacy_profile_credential_removed": True,
            "generation_retired_before_terminate": True,
            "profile_public_shared_db_payloads_no_credential": True,
        }
        (tmp_path / "metrics-generation-results.json").write_text(json.dumps(outcomes, indent=2))
    finally:
        instance._stop_engines()
        if restored is not None:
            restored._stop_engines()
