from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.storage import connect_database, initialize_database
from harness.tunnel_config import AdaptivePaths, PathResult, connected_lan_routes
from harness.tunnel_service import validate_request
from harness.tunnel_windows import Filter, Guid, Value, policy_rules
from harness.tunnels import (
    TunnelConflict,
    TunnelError,
    begin_operation,
    delete_profile,
    finish_operation,
    get_profile,
    normalize_rules,
    observe_runtime,
    save_profile,
)


def settings() -> dict[str, object]:
    return {
        "name": "Личный VPS",
        "endpoints": [{"host": "203.0.113.10", "user": "root", "port": 22}],
        "exceptions": ["example.org", "192.0.2.42", "10.20.1.99/16"],
        "lan_bypass": True,
    }


@pytest.fixture
def database(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "tunnels.db"
    initialize_database(path)
    connection = connect_database(path)
    yield connection
    connection.close()


def test_tunnels_independent_cas_and_operation_serialization(database: sqlite3.Connection) -> None:
    assert database.execute("SELECT count(*) FROM servers").fetchone()[0] == 0
    first = save_profile(database, "personal", 0, settings())
    assert first["revision"] == 1
    assert first["settings"]["exceptions"][1] == {"kind": "cidr", "value": "192.0.2.42/32"}
    save_profile(database, "backup", 0, {**settings(), "name": "Other"})
    with pytest.raises(TunnelConflict):
        save_profile(database, "personal", 0, settings())
    operation = begin_operation(database, "personal", 1, "connect")
    with pytest.raises(TunnelConflict):
        begin_operation(database, "backup", 1, "connect")
    finish_operation(database, operation["id"], success=False, message="Служба недоступна")
    assert get_profile(database, "personal")["runtime"]["protection"] == "inactive"
    assert database.execute("SELECT count(*) FROM servers").fetchone()[0] == 0
    assert database.execute("SELECT count(*) FROM server_events").fetchone()[0] == 0


@pytest.mark.parametrize("marker", ["prepared", "setup_incomplete", "removal_pending"])
def test_partial_server_state_blocks_delete_and_endpoint_changes(
    database: sqlite3.Connection, marker: str
) -> None:
    profile = save_profile(database, "personal", 0, settings())
    observe_runtime(database, "personal", "disconnected", "inactive", {marker: True})
    with pytest.raises(TunnelConflict):
        delete_profile(database, "personal", profile["revision"])
    changed = {**settings(), "endpoints": [{"host": "198.51.100.20"}]}
    with pytest.raises(TunnelConflict):
        save_profile(database, "personal", profile["revision"], changed)
    saved = save_profile(database, "personal", profile["revision"], {**settings(), "name": "Retry"})
    observe_runtime(database, "personal", "disconnected", "inactive", {"prepared": False})
    delete_profile(database, "personal", saved["revision"])
    assert database.execute("SELECT count(*) FROM tunnel_profiles").fetchone()[0] == 0


def test_connected_requires_guard_and_bulk_proof(database: sqlite3.Connection) -> None:
    save_profile(database, "personal", 0, settings())
    operation = begin_operation(database, "personal", 1, "connect")
    with pytest.raises(TunnelError, match="проверку"):
        finish_operation(
            database,
            operation["id"],
            success=True,
            message="Connected",
            state="connected",
            protection="inactive",
            snapshot={"tcp_ok": True, "dns_ok": True, "bulk_ok": True},
        )
    assert database.execute("SELECT state FROM tunnel_operations").fetchone()[0] == "pending"
    finish_operation(
        database,
        operation["id"],
        success=False,
        message="Трафик заблокирован",
        state="blocked",
        protection="active",
        snapshot={"tcp_ok": False, "dns_ok": False, "bulk_ok": False},
    )
    current = get_profile(database, "personal")
    assert current["runtime"]["desired"]
    assert current["runtime"]["protection"] == "active"
    with pytest.raises(TunnelConflict):
        delete_profile(database, "personal", current["revision"])


def test_only_one_active_profile_and_metric_deltas(database: sqlite3.Connection) -> None:
    ready = {"tcp_ok": True, "dns_ok": True, "bulk_ok": True, "rx_bytes": 100, "tx_bytes": 40}
    for name in ("first", "second"):
        save_profile(database, name, 0, settings())
        operation = begin_operation(database, name, 1, "connect")
        finish_operation(
            database,
            operation["id"],
            success=True,
            message="Проверено",
            state="connected",
            protection="active",
            snapshot=ready,
        )
    assert get_profile(database, "first")["runtime"]["state"] == "disconnected"
    observe_runtime(
        database, "second", "connected", "active", {**ready, "rx_bytes": 140, "tx_bytes": 70}
    )
    observe_runtime(
        database, "second", "connected", "active", {**ready, "rx_bytes": 180, "tx_bytes": 80}
    )
    row = database.execute("SELECT rx_bytes,tx_bytes FROM tunnel_metrics").fetchone()
    assert tuple(row) == (80, 40)


@pytest.mark.parametrize("state", ["reconnecting", "blocked"])
@pytest.mark.parametrize("protection", ["active", "unknown", "inactive", "unavailable"])
def test_connection_events_reflect_observed_protection_without_rewriting_history(
    database: sqlite3.Connection, state: str, protection: str
) -> None:
    saved = save_profile(database, "personal", 0, settings())
    snapshot = {
        "transport": "reality",
        "tcp_ok": True,
        "dns_ok": True,
        "bulk_ok": True,
        "rx_bytes": 100,
        "tx_bytes": 40,
    }
    observe_runtime(database, "personal", "connected", "active", snapshot)
    before = database.execute("SELECT * FROM tunnel_events ORDER BY sequence").fetchall()
    observe_runtime(database, "personal", state, protection, snapshot)
    current = get_profile(database, "personal")
    after = database.execute("SELECT * FROM tunnel_events ORDER BY sequence").fetchall()
    assert after[:-1] == before
    assert current["revision"] == saved["revision"] and current["settings"] == saved["settings"]
    assert current["runtime"]["state"] == state
    assert current["runtime"]["protection"] == protection
    assert current["runtime"]["desired"] is True
    assert current["runtime"]["snapshot"] == snapshot
    expected_protection = {
        "active": "защита сохраняется"
        if state == "reconnecting"
        else "защищённый трафик заблокирован",
        "unknown": "защита не подтверждена",
        "inactive": "защита неактивна",
        "unavailable": "защита недоступна",
    }[protection]
    prefix = (
        "Соединение восстанавливается" if state == "reconnecting" else "Рабочий путь не подтверждён"
    )
    assert after[-1][3] == prefix + "; " + expected_protection
    # Repeated observation neither rewrites/appends history nor counts the same bytes twice.
    observe_runtime(database, "personal", state, protection, snapshot)
    observe_runtime(database, "personal", "connected", "active", snapshot)
    assert (
        database.execute("SELECT * FROM tunnel_events ORDER BY sequence").fetchall()[:-1] == after
    )
    assert tuple(
        database.execute("SELECT sum(rx_bytes),sum(tx_bytes) FROM tunnel_metrics").fetchone()
    ) == (100, 40)


@pytest.mark.parametrize(
    ("protection", "missing_proof"),
    [(value, None) for value in ("unknown", "inactive", "unavailable")]
    + [("active", value) for value in ("tcp_ok", "dns_ok", "bulk_ok")],
)
def test_observed_connected_requires_active_protection_and_all_health_proofs(
    database: sqlite3.Connection, protection: str, missing_proof: str | None
) -> None:
    save_profile(database, "personal", 0, settings())
    snapshot = {"tcp_ok": True, "dns_ok": True, "bulk_ok": True}
    if missing_proof is not None:
        snapshot[missing_proof] = False
    before = list(database.iterdump())
    with pytest.raises(TunnelError, match="проверку соединения"):
        observe_runtime(database, "personal", "connected", protection, snapshot)
    assert list(database.iterdump()) == before


@pytest.mark.parametrize(
    "invalid",
    [
        {**settings(), "password": "CANARY"},
        {**settings(), "endpoints": [{"host": "127.0.0.1"}]},
        {**settings(), "endpoints": [{"host": "203.0.113.10", "user": "root;id"}]},
        {**settings(), "endpoints": [{"host": "203.0.113.10", "port": True}]},
        {**settings(), "exceptions": ["https://example.org/path"]},
        {**settings(), "exceptions": ["0.0.0.0/0"]},
    ],
)
def test_rejected_input_never_persists_or_reflects_secret(
    database: sqlite3.Connection, invalid: object
) -> None:
    with pytest.raises(TunnelError) as error:
        save_profile(database, "personal", 0, invalid)
    assert "CANARY" not in str(error.value)
    assert database.execute("SELECT count(*) FROM tunnel_profiles").fetchone()[0] == 0


def test_domain_normalization_and_no_default_lan_bypass_from_dhcp() -> None:
    assert normalize_rules(["EXAMPLE.org", "*.example.org", "пример.рф"])[0] == {
        "kind": "domain",
        "value": "example.org",
    }
    routes = [
        {"dst": "0.0.0.0/0", "dev": "wlan0", "scope": "link", "protocol": "kernel"},
        {"dst": "0.0.0.0/1", "dev": "wlan0", "scope": "link", "protocol": "kernel"},
        {"dst": "192.168.1.0/24", "dev": "wlan0", "scope": "link", "protocol": "kernel"},
        {"dst": "10.0.0.0/8", "dev": "wlan0", "scope": "link", "protocol": "dhcp"},
        {"dst": "10.2.0.0/16", "dev": "docker0", "scope": "link", "protocol": "kernel"},
    ]
    assert connected_lan_routes(routes) == ["192.168.1.0/24"]


def test_failover_requires_bulk_and_retains_healthy_path() -> None:
    paths = AdaptivePaths()
    good = PathResult(0, "reality", True, True, True, latency_ms=100)
    assert paths.choose([good], 1) == good
    faster = PathResult(0, "naive", True, True, True, latency_ms=1)
    assert paths.choose([good, faster], 2) == good
    frozen = PathResult(0, "reality", True, True, False)
    assert paths.choose([frozen, faster], 3) is None
    assert paths.choose([frozen, faster], 4) == faster
    assert paths.current == (0, "naive")


def test_privileged_protocol_rejects_arbitrary_commands_and_paths() -> None:
    for request in (
        {"version": 1, "operation": "shell", "payload": {}},
        {"version": 1, "operation": "status", "payload": {"profile_id": "safe", "command": "id"}},
        {
            "version": 1,
            "operation": "connect",
            "payload": {
                "profile_id": "safe",
                "revision": 1,
                "settings": {},
                "binary_path": "/tmp/evil",
            },
        },
    ):
        with pytest.raises(TunnelError):
            validate_request(request)


def test_windows_policy_abi_and_provider_scoping() -> None:
    import ctypes

    assert ctypes.sizeof(Guid) == 16
    assert ctypes.sizeof(Value) == 16
    assert ctypes.sizeof(Filter) == 200
    rules = policy_rules([r"C:\Program Files\HarnessTunnel\sing-box.exe"], ["192.168.1.0/24"], 1234)
    assert any(rule["flags"] == 2 and not rule["permit"] for rule in rules)
    assert any(
        rule["flags"] == 1 and rule["family"] == "ale" and not rule["permit"] for rule in rules
    )
    assert not any(
        condition[0] == "app"
        for rule in rules
        if rule["family"] == "transport"
        for condition in rule["conditions"]
    )
    assert all(rule["flags"] in {1, 2} for rule in rules)


def test_no_credential_columns_or_server_links(database: sqlite3.Connection) -> None:
    names = [
        row[0]
        for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tunnel_%'"
        )
    ]
    for name in names:
        columns = [row[1] for row in database.execute(f'PRAGMA table_info("{name}")')]
        assert not {"password", "private_key", "server_id"} & set(columns)
        assert all(
            row[2] != "servers" for row in database.execute(f'PRAGMA foreign_key_list("{name}")')
        )
    assert json.dumps(settings())
