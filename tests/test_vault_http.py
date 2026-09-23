from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from harness.dashboard import DashboardServerManager
from harness.storage import initialize_database
from harness.vault_process import VaultProcess


def post(
    origin: str, data: dict[str, object], *, token: str = "", foreign: bool = False
) -> dict[str, object]:
    request = Request(
        origin + "/api",
        data=json.dumps(data).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": "https://foreign.invalid" if foreign else origin,
            "Authorization": f"Bearer {token}",
        },
    )
    with urlopen(request, timeout=10) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert "Access-Control-Allow-Origin" not in response.headers
        result = json.loads(response.read())
        assert isinstance(result, dict)
        return result


def test_separate_process_http_origin_auth_and_restart(tmp_path: Path) -> None:
    process = VaultProcess(tmp_path / "harness.db")
    folder = tmp_path / "copies"
    folder.mkdir()
    try:
        origin = process.origin("http://127.0.0.1:1234")
        with urlopen(origin, timeout=5) as response:
            assert (
                "frame-ancestors http://127.0.0.1:1234"
                in response.headers["Content-Security-Policy"]
            )
            assert "Хранилище закрыто" in response.read().decode()
        with pytest.raises(HTTPError) as foreign:
            post(origin, {"action": "create"}, foreign=True)
        assert foreign.value.code == 403
        assert not (process.root / "projects.kdbx").exists()
        created = post(
            origin, {"action": "create", "password": "test-master-password", "folder": str(folder)}
        )
        token = str(created["token"])
        for action in ("device_unlock", "device_remember", "device_forget"):
            with pytest.raises(HTTPError) as foreign_device:
                post(origin, {"action": action}, token=token, foreign=True)
            assert foreign_device.value.code == 403
        for action in ("device_remember", "device_forget"):
            with pytest.raises(HTTPError) as untrusted_device:
                post(origin, {"action": action}, token="harness-mcp-bearer")
            assert untrusted_device.value.code == 401
        with pytest.raises(HTTPError) as locked:
            post(origin, {"action": "state"}, token="mcp-token")
        assert locked.value.code == 401
        assert post(origin, {"action": "state"}, token=token)["records"] == []
        with pytest.raises(HTTPError) as host:
            urlopen(Request(origin, headers={"Host": "foreign.invalid"}), timeout=5)
        assert host.value.code == 404
        with pytest.raises(HTTPError) as get_api:
            urlopen(origin + "/api", timeout=5)
        assert get_api.value.code == 404
        process.close()
        process = VaultProcess(tmp_path / "harness.db")
        restarted = process.origin("http://127.0.0.1:1234")
        with pytest.raises(HTTPError) as stale:
            post(restarted, {"action": "state"}, token=token)
        assert stale.value.code == 401
        assert (
            post(restarted, {"action": "unlock", "password": "test-master-password"})["records"]
            == []
        )
    finally:
        process.close()


def test_empty_dashboard_can_recover_vault_without_registry(tmp_path: Path) -> None:
    database = tmp_path / "harness.db"
    initialize_database(database)
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url()
        with urlopen(url + "vault/all/", timeout=10) as response:
            html = response.read().decode()
            assert "Личное хранилище проекта" in html
            assert "frame-src http://127.0.0.1:" in response.headers["Content-Security-Policy"]
            assert "<script" not in html  # Parent refresh never discards private drafts.
            assert "#project=all" in html
        with urlopen(url, timeout=5) as response:
            assert b"frame-src" not in response.headers["Content-Security-Policy"].encode()
    finally:
        manager.close()


def test_no_password_restart_and_foreign_origin_still_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "copies"
    folder.mkdir()
    process = VaultProcess(tmp_path / "harness.db")
    try:
        origin = process.origin("http://127.0.0.1:1234")
        created = post(
            origin, {"action": "create", "access_mode": "no_password", "folder": str(folder)}
        )
        assert created["access_mode"] == "no_password"
        process.close()
        process = VaultProcess(tmp_path / "harness.db")
        origin = process.origin("http://127.0.0.1:1234")
        assert post(origin, {"action": "status"})["access_mode"] == "no_password"
        with pytest.raises(HTTPError) as foreign:
            post(origin, {"action": "open_without_password"}, foreign=True)
        assert foreign.value.code == 403
        opened = post(origin, {"action": "open_without_password"})
        assert opened["records"] == []
        with pytest.raises(HTTPError) as missing:
            post(origin, {"action": "state"}, token="harness-model-bearer")
        assert missing.value.code == 401
        assert post(origin, {"action": "state"}, token=str(opened["token"]))["records"] == []
    finally:
        process.close()
