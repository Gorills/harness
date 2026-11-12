from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from harness.tunnel_phone import MAX_PHONE_BYTES, phone_profile, phone_qr
from harness.tunnel_service import ServiceRuntime, validate_request
from harness.tunnels import TunnelError


def applied() -> dict[str, Any]:
    return {
        "settings": {
            "name": "Phone fixture",
            "endpoints": [{"host": "203.0.113.8", "user": "root", "port": 22}],
        },
        "secrets": {
            "endpoints": [
                {
                    "prepared": True,
                    "uuid": "7b3ff1d0-bf22-4ffa-8ad9-ac4745b35648",
                    "reality_public_key": "A" * 43,
                    "reality_private_key": "SERVER_PRIVATE_CANARY",
                    "reality_server_name": "example.org",
                    "reality_short_id": "0123",
                    "hysteria_password": "client-password",
                    "obfs_password": "client-obfs",
                    "certificate_server_name": "fixture.invalid",
                    "certificate": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n",
                    "maintenance_user": "ADMIN_CANARY",
                    "naive_password": "UNUSED_SECRET_CANARY",
                }
            ],
            "maintenance_key": "SSH_SECRET_CANARY",
        },
    }


def test_export_allowlist_does_not_change_running_state_or_leak_admin(tmp_path: Path) -> None:
    runtime = ServiceRuntime.__new__(ServiceRuntime)
    runtime.operations = Lock()
    runtime.state = {
        "profiles": {"fixture": applied()},
        "desired": True,
        "active": "fixture",
        "state": "connected",
        "protection": "active",
    }
    original = copy.deepcopy(runtime.state)
    result = runtime.execute("phone_export", {"profile_id": "fixture"})
    assert result["ok"] is True
    assert runtime.state == original
    code = result["phone"]
    raw = base64.urlsafe_b64decode(code[4:] + "=" * (-len(code[4:]) % 4))
    value = json.loads(raw)
    assert set(value) == {"v", "name", "host", "r", "h"}
    assert value["h"]["p"] == "client-password"
    assert value["r"]["k"] == "A" * 43
    for canary in (
        "SERVER_PRIVATE_CANARY",
        "SSH_SECRET_CANARY",
        "ADMIN_CANARY",
        "UNUSED_SECRET_CANARY",
    ):
        assert canary not in raw.decode()
    assert not list(tmp_path.iterdir())
    matrix = phone_qr(code)
    assert len(matrix) <= 185 and all(len(row) == len(matrix) for row in matrix)
    assert all(cell is False for row in matrix[:4] for cell in row)


@pytest.mark.parametrize(
    "mode", ["busy", "unprepared", "removing", "missing", "corrupt", "oversize"]
)
def test_export_failures_never_enter_network_recovery(mode: str) -> None:
    runtime = ServiceRuntime.__new__(ServiceRuntime)
    runtime.operations = Lock()
    profile = applied()
    runtime.state = {
        "profiles": {"fixture": profile},
        "desired": True,
        "active": "fixture",
        "state": "connected",
        "protection": "active",
    }
    if mode == "busy":
        runtime.operations.acquire()
    if mode == "unprepared":
        profile["secrets"]["endpoints"][0]["prepared"] = False
    if mode == "removing":
        profile["removing"] = True
    if mode == "missing":
        runtime.state["profiles"] = {}
    if mode == "corrupt":
        del profile["secrets"]["endpoints"][0]["certificate"]
    if mode == "oversize":
        profile["secrets"]["endpoints"][0]["certificate"] = "x" * 5000
    original = copy.deepcopy(runtime.state)
    # No guard, disk paths, processes, SSH helper or persistence methods exist on
    # this fixture: entering those paths would fail rather than silently pass.
    assert runtime.execute("phone_export", {"profile_id": "fixture"})["ok"] is False
    assert runtime.state == original


@pytest.mark.parametrize(
    "extra", [{"credentials": {"password": "canary"}}, {"settings": {}}, {"revision": 1}]
)
def test_export_accepts_only_a_profile_identifier(extra: dict[str, Any]) -> None:
    with pytest.raises(TunnelError):
        validate_request(
            {
                "version": 1,
                "operation": "phone_export",
                "payload": {"profile_id": "fixture", **extra},
            }
        )
    assert validate_request(
        {"version": 1, "operation": "phone_export", "payload": {"profile_id": "fixture"}}
    ) == ("phone_export", {"profile_id": "fixture"})


def test_unprepared_and_oversized_qr_are_rejected() -> None:
    profile = applied()
    profile["secrets"]["endpoints"][0]["prepared"] = False
    with pytest.raises(TunnelError):
        phone_profile(profile)
    with pytest.raises(TunnelError):
        phone_qr("HT1:" + "x" * MAX_PHONE_BYTES)


def test_http_export_is_explicit_csrf_checked_and_never_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    import harness.tunnel_control as control
    from harness.dashboard import DashboardServerManager
    from harness.storage import connect_database, initialize_database
    from harness.tunnels import observe_runtime, save_profile

    code = phone_profile(applied())
    calls: list[str] = []

    class FakeService:
        def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(operation)
            if operation == "phone_export":
                return {"ok": True, "phone": code}
            return {
                "ok": True,
                "state": "connected",
                "protection": "active",
                "snapshot": {"prepared": True, "tcp_ok": True, "dns_ok": True, "bulk_ok": True},
            }

    monkeypatch.setattr(control, "NetworkServiceClient", FakeService)
    monkeypatch.setenv("HARNESS_DEV_ROOT", str(tmp_path))
    database = tmp_path / "state.db"
    initialize_database(database)
    connection = connect_database(database)
    save_profile(
        connection, "fixture", 0, {**applied()["settings"], "exceptions": [], "lan_bypass": True}
    )
    connection.close()
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url().rstrip("/")
        connection = connect_database(database)
        observe_runtime(
            connection,
            "fixture",
            "connected",
            "active",
            {"prepared": True, "tcp_ok": True, "dns_ok": True, "bulk_ok": True},
        )
        connection.execute("UPDATE tunnel_runtime SET desired=1 WHERE profile_id='fixture'")
        connection.close()
        for origin, revision, expected in (
            ("https://foreign.invalid", 1, 403),
            (url, 999, 409),
            (url, 1, 200),
        ):
            request = Request(
                url + "/tunnels/action",
                data=json.dumps(
                    {
                        "action": "phone_export",
                        "profile_id": "fixture",
                        "expected_revision": revision,
                    }
                ).encode(),
                headers={"Content-Type": "application/json", "Origin": origin},
                method="POST",
            )
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                assert response.status == expected
                body = json.loads(response.read())
                assert response.headers["Cache-Control"] == "no-store"
                if expected == 200:
                    assert body["phone"]["shared"] is True
                    assert body["phone"]["qr"] == phone_qr(code)
        assert calls.count("phone_export") == 1
        connection = connect_database(database)
        try:
            assert connection.execute("SELECT count(*) FROM tunnel_operations").fetchone()[0] == 0
            assert connection.execute("SELECT desired FROM tunnel_runtime").fetchone()[0] == 1
            assert code not in "\n".join(connection.iterdump())
            assert "client-password" not in "\n".join(connection.iterdump())
        finally:
            connection.close()
        with urlopen(url + "/tunnels/data?profile=fixture", timeout=5) as response:
            assert "phone" not in json.loads(response.read())
    finally:
        manager.close()
