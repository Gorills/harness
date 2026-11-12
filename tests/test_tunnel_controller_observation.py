"""Actual missing Unix IPC -> controller/store/HTTP/SSE, with scoped status recovery."""

from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

import pytest

import harness.dashboard as dashboard
import harness.tunnel_control as control
from harness.storage import connect_database, initialize_database
from harness.tunnel_config import runtime_settings
from harness.tunnels import begin_operation, get_profile, observe_runtime, save_profile

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="local Unix IPC fixture; no Windows execution claim"
)


class PollOnce:
    def __init__(self) -> None:
        self.calls = 0
        self.stopped = False

    def wait(self, timeout: float) -> bool:
        self.calls += 1
        return self.stopped or self.calls > 1

    def set(self) -> None:
        self.stopped = True


def test_missing_ipc_start_polls_failed_command_and_fresh_http_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, endpoint = tmp_path / "shared.db", tmp_path / "svc.sock"
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(tmp_path))
    initialize_database(database)
    settings = runtime_settings(
        {
            "name": "Synthetic availability",
            "endpoints": [{"host": "203.0.113.10"}],
            "lan_bypass": False,
        }
    )
    baseline = {
        "prepared": True,
        "transport": "reality",
        "tcp_ok": True,
        "dns_ok": True,
        "bulk_ok": True,
        "udp_ok": True,
        "rx_bytes": 34247,
        "tx_bytes": 34381,
        "rx_bps": 2000,
        "tx_bps": 2000,
    }
    connection = connect_database(database)
    save_profile(connection, "fixture", 0, settings)
    observe_runtime(connection, "fixture", "connected", "active", baseline)
    connection.close()
    client = control.NetworkServiceClient(endpoint)
    assert not endpoint.exists()
    with pytest.raises(control.ServiceUnavailable):
        client.request("status", {"profile_id": "fixture"})
    controller = control.TunnelController(database, service=client)
    records: list[dict[str, Any]] = []
    manager = None
    stop_server = Event()
    server = None
    errors: list[BaseException] = []

    def seed_positive() -> None:
        connection = connect_database(database)
        try:
            observe_runtime(connection, "fixture", "connected", "active", baseline)
        finally:
            connection.close()

    def inspect(label: str, expected_revision: int, rates: bool) -> dict[str, Any]:
        selected = controller.state("fixture")["selected"]
        connection = connect_database(database)
        try:
            stored = get_profile(connection, "fixture")
            minutes = list(
                connection.execute(
                    "SELECT coalesce(sum(rx_bytes),0),coalesce(sum(tx_bytes),0) FROM tunnel_metrics"
                ).fetchone()
            )
            last_connection = connection.execute(
                "SELECT message FROM tunnel_events WHERE profile_id='fixture' AND kind='connection' "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0]
            dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        runtime = stored["runtime"]
        assert runtime == selected["runtime"]
        assert stored["settings"] == settings and stored["revision"] == expected_revision
        assert runtime["desired"] is True
        assert runtime["snapshot"]["rx_bytes"] == 34247 and runtime["snapshot"]["tx_bytes"] == 34381
        assert minutes == [34247, 34381]
        assert all(
            runtime["snapshot"][key] for key in ("tcp_ok", "dns_ok", "bulk_ok", "udp_ok")
        )  # no fabricated datapath/guard failure
        assert all((key in runtime["snapshot"]) is rates for key in ("rx_bps", "tx_bps"))
        assert str(endpoint) not in dump
        assert (
            next(event for event in selected["events"] if event["kind"] == "connection")["message"]
            == last_connection
        )
        if label.startswith("unavailable-poll-"):
            assert last_connection == "Соединение восстанавливается; защита не подтверждена"
        records.append(
            {
                "label": label,
                "runtime": runtime,
                "revision": stored["revision"],
                "minute_totals": minutes,
                "last_connection_event": last_connection,
            }
        )
        return selected

    def poll() -> None:
        controller._stop = PollOnce()  # type: ignore[assignment]
        controller._poll()

    def http_state(base: str, rates: bool, expected_message: str | None = None) -> None:
        def check_event(payload: dict[str, Any]) -> None:
            if expected_message is not None:
                selected = payload["selected"]
                assert selected["runtime"]["state"] == "reconnecting"
                assert selected["runtime"]["protection"] == "unknown"
                assert (
                    next(event for event in selected["events"] if event["kind"] == "connection")[
                        "message"
                    ]
                    == expected_message
                )

        with urlopen(base + "/tunnels/data?profile=fixture", timeout=5) as response:
            payload = json.load(response)
        check_event(payload)
        assert all(
            (key in payload["selected"]["runtime"]["snapshot"]) is rates
            for key in ("rx_bps", "tx_bps")
        )
        with urlopen(base + "/tunnels/events?profile=fixture", timeout=5) as response:
            assert response.readline().strip() == b"event: snapshot"
            event = response.readline().decode()
            assert event.startswith("data: ")
            payload = json.loads(event[6:])
            check_event(payload)
            assert all(
                (key in payload["selected"]["runtime"]["snapshot"]) is rates
                for key in ("rx_bps", "tx_bps")
            )

    try:
        controller._stop.set()  # only worker scheduling is controlled; actual start reconciliation runs
        controller.start()
        assert controller._monitor is not None
        controller._monitor.join(timeout=2)
        assert not controller._monitor.is_alive()
        initial = inspect("startup", 1, False)
        assert (
            initial["runtime"]["state"] == "reconnecting"
            and initial["runtime"]["protection"] == "unknown"
        )
        monkeypatch.setattr(dashboard, "TunnelController", lambda database: controller)
        manager = dashboard.DashboardServerManager(database)
        base = manager.get_url().rstrip("/")
        http_state(base, False)
        seed_positive()
        for count in range(3):
            poll()
            current = inspect("unavailable-poll-" + str(count + 1), 1, False)
            assert (
                current["runtime"]["state"] == "reconnecting"
                and current["runtime"]["protection"] == "unknown"
            )
        http_state(base, False, "Соединение восстанавливается; защита не подтверждена")
        seed_positive()
        connection = connect_database(database)
        operation = begin_operation(connection, "fixture", 1, "check")
        connection.close()
        credentials: dict[str, str] = {}
        controller._run(operation, credentials)
        failed = inspect("command-failed-no-status", 2, False)
        assert failed["operations"][0]["state"] == "failed"
        assert (
            failed["runtime"]["state"] == "connected"
            and failed["runtime"]["protection"] == "active"
        )  # retain existing intent/last-known state, not infer guard loss
        assert not credentials
        http_state(base, False)
        # Recovery uses the actual client/protocol/result. Only the installed
        # root peer identity is substituted for this known current-UID fixture.
        fresh = {**baseline, "rx_bps": 1234, "tx_bps": 2345}
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(endpoint))
            listener.listen()
            listener.settimeout(0.2)

            def serve() -> None:
                try:
                    while not stop_server.is_set():
                        try:
                            peer, _ = listener.accept()
                        except TimeoutError:
                            continue
                        with peer:
                            size = struct.unpack("!I", control._receive(peer, 4))[0]
                            request = json.loads(control._receive(peer, size))
                            result = (
                                {
                                    "ok": True,
                                    "state": "connected",
                                    "protection": "active",
                                    "snapshot": fresh,
                                }
                                if request["operation"] == "status"
                                else {"ok": False, "code": "health_failed"}
                            )
                            data = json.dumps(result).encode()
                            peer.sendall(struct.pack("!I", len(data)) + data)
                except BaseException as exc:
                    errors.append(exc)

            server = Thread(target=serve, daemon=True)
            server.start()
            original_socket = socket.socket

            class FixturePeerSocket(original_socket):
                def getsockopt(self, level: int, option: int, buflen: int = 0) -> Any:
                    raw = super().getsockopt(level, option, buflen)
                    if level == socket.SOL_SOCKET and option == socket.SO_PEERCRED:
                        assert self.getpeername() == str(endpoint)
                        pid, uid, gid = struct.unpack("3i", raw)
                        assert uid == os.geteuid()
                        return struct.pack("3i", pid, 0, gid)
                    return raw

            scoped_socket = SimpleNamespace(**{**vars(socket), "socket": FixturePeerSocket})
            with monkeypatch.context() as peer_identity:
                peer_identity.setattr(control, "socket", scoped_socket)
                poll()
                recovered = inspect("fresh-status-recovery", 2, True)
                assert (
                    recovered["runtime"]["snapshot"]["rx_bps"] == 1234
                    and recovered["runtime"]["snapshot"]["tx_bps"] == 2345
                )
                assert (
                    recovered["runtime"]["state"] == "connected"
                    and recovered["runtime"]["protection"] == "active"
                )
                http_state(base, True)
                connection = connect_database(database)
                operation = begin_operation(connection, "fixture", 2, "check")
                connection.close()
                controller._run(
                    operation, {}
                )  # rejected command, but a fresh status must remain authoritative
                failed_with_fresh = inspect("failed-command-fresh-status", 3, True)
                assert failed_with_fresh["operations"][0]["state"] == "failed"
                http_state(base, True)
            stop_server.set()
            server.join(timeout=2)
        assert not errors
        (tmp_path / "controller-availability-results.json").write_text(
            json.dumps(
                {
                    "records": records,
                    "actual_absent_client": True,
                    "actual_controller_and_sqlite": True,
                    "http_and_sse_absent_and_recovered": True,
                    "minutes_unchanged": [34247, 34381],
                    "fresh_peer_uid_boundary_fixture_only": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        stop_server.set()
        if server is not None:
            server.join(timeout=2)
        if manager is not None:
            manager.close()
        controller._executor.shutdown(wait=True, cancel_futures=True)


def test_startup_observation_failure_rolls_back_all_profiles_then_retries(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "rollback.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        for identity in ("first", "second"):
            save_profile(
                connection,
                identity,
                0,
                {"name": identity, "endpoints": [{"host": "203.0.113.10"}], "exceptions": []},
            )
            observe_runtime(
                connection,
                identity,
                "connected",
                "active",
                {
                    "tcp_ok": True,
                    "dns_ok": True,
                    "bulk_ok": True,
                    "rx_bytes": 100,
                    "tx_bytes": 200,
                    "rx_bps": 10,
                    "tx_bps": 20,
                },
            )
        before = connection.execute("SELECT * FROM tunnel_runtime ORDER BY profile_id").fetchall()
        connection.execute("CREATE TABLE fixture_attempts(count INTEGER NOT NULL)")
        connection.execute("INSERT INTO fixture_attempts VALUES(0)")
        connection.execute(
            "CREATE TRIGGER fixture_startup_failure BEFORE UPDATE ON tunnel_runtime BEGIN UPDATE fixture_attempts SET count=count+1; SELECT RAISE(ABORT,'injected startup write failure') WHERE (SELECT count FROM fixture_attempts)=2; END"
        )
    finally:
        connection.close()
    controller = control.TunnelController(
        database, service=control.NetworkServiceClient(tmp_path / "absent.sock")
    )
    controller._stop.set()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected startup"):
            controller.start()
        assert not controller._started and controller._monitor is None
        connection = connect_database(database)
        try:
            assert (
                connection.execute("SELECT * FROM tunnel_runtime ORDER BY profile_id").fetchall()
                == before
            )
            assert connection.execute("SELECT count FROM fixture_attempts").fetchone()[0] == 0
            connection.execute("DROP TRIGGER fixture_startup_failure")
        finally:
            connection.close()
        controller.start()
        assert controller._monitor is not None
        controller._monitor.join(timeout=2)
        connection = connect_database(database)
        try:
            for identity in ("first", "second"):
                profile = get_profile(connection, identity)
                assert profile["revision"] == 1
                assert profile["runtime"]["snapshot"]["rx_bytes"] == 100
                assert profile["runtime"]["snapshot"]["tx_bytes"] == 200
                assert all(
                    key not in profile["runtime"]["snapshot"] for key in ("rx_bps", "tx_bps")
                )
            assert get_profile(connection, "first")["runtime"]["desired"] is False
            assert get_profile(connection, "first")["runtime"]["protection"] == "inactive"
            assert get_profile(connection, "second")["runtime"]["desired"] is True
            assert get_profile(connection, "second")["runtime"]["protection"] == "unknown"
        finally:
            connection.close()
    finally:
        controller.close()
