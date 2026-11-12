from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_dashboard_actions import _post

from harness import storage
from harness.dashboard import DashboardServerManager
from harness.dashboard_servers import _render_storage, render_servers_content
from harness.servers import (
    MAX_RESPONSE_BYTES,
    ServerConflictError,
    ServerError,
    edit_server,
    get_server,
    save_server,
    validate_server,
)
from harness.storage import connect_database, initialize_database


def _site() -> dict[str, object]:
    return {"site_id": "front", "name": "Frontend", "source": "fixture"}


def _profile() -> dict[str, object]:
    return {"profile_id": "test", "name": "Test preparation", "source": "fixture"}


def _passport() -> dict[str, object]:
    return {
        "hostname": "fixture-host",
        "os": "Debian GNU/Linux 11",
        "kernel": "5.10.0",
        "cpu_model": "Synthetic CPU",
        "cpu_cores": 8,
        "memory_mib": 24576,
        "swap_mib": 0,
        "storage": ["/dev/vda · 80 GiB · SSD · /"],
        "source": "synthetic read-only inventory",
        "checked_at": "2026-10-05T22:00:00+07:00",
    }


@pytest.mark.parametrize(
    ("entry", "capacity"),
    [
        ("/dev/vda; 483183820800 bytes; virtual disk; mount /", "450 ГиБ"),
        ("/dev/zram0; 4294967296 bytes; swap in RAM", "4 ГиБ"),
        ("/dev/vda2; 104857600 bytes; vfat; mount /boot/efi", "100 МиБ"),
        ("/dev/sda · 80 GiB · SSD · /", "80 GiB"),
        ("/dev/sdb; 0 bytes; unallocated", "0 Б"),
        ("/dev/sdc; 1536 B; fixture", "1,5 КиБ"),
    ],
)
def test_disk_capacity_display_preserves_source(entry: str, capacity: str) -> None:
    html = _render_storage([entry])
    assert capacity in html
    assert entry.split(";", 1)[0].split(" · ", 1)[0] in html


def test_disk_display_keeps_unknown_records_and_escapes_details(tmp_path: Path) -> None:
    entries = [
        "/dev/vda; 483183820800 bytes; том /dev/vda3; mount /; free: unknown <&>",
        'Legacy disk description <img src="x">; size unknown',
    ]
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(connection, "host", 0, {"name": "Host", "passport": {"storage": entries}})
        before = get_server(connection, "host")
    html = render_servers_content(database, "host", {})
    assert html.count('class="server-storage-table"') == 2
    assert html.count("450 ГиБ") == 2
    assert 'title="483183820800 bytes"' in html
    assert "том /dev/vda3 · mount / · free: unknown &lt;&amp;&gt;" in html
    assert "Legacy disk description &lt;img src=&quot;x&quot;&gt;; size unknown" in html
    assert '<img src="x">' not in html
    with connect_database(database) as connection:
        assert get_server(connection, "host") == before


@pytest.mark.parametrize("condition", ["current", "stale", "unknown", "missing", "custom_policy"])
def test_brief_passport_has_same_dated_confidence_and_source_as_full(
    tmp_path: Path, condition: str
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    passport = _passport()
    passport["checked_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    source = 'Источник "<&>" ' + "long-reference" * 14
    passport["source"] = source
    if condition == "unknown":
        passport["checked_at"] = None
    elif condition == "stale":
        passport["checked_at"] = "2000-01-01T00:00:00Z"
    metadata: dict[str, object] = {"name": "Host"}
    if condition != "missing":
        metadata["passport"] = passport
    if condition == "custom_policy":
        metadata["map_policy"] = {
            "health_hours": 24,
            "inventory_hours": 168,
            "passport_days": 1,
            "expiry_days": [7, 14, 30],
        }
    with connect_database(database) as connection:
        save_server(connection, "host", 0, metadata)
    html = render_servers_content(database, "host", {"view": "overview"})
    brief = html.split('class="server-panel server-passport-brief"')[1].split("</section>")[0]
    assert "Дата проверки:" in brief and "Источник:" in brief
    if condition == "missing":
        assert "Паспорт не заполнен" in brief and "Не указан" in brief  # noqa: RUF001
        assert 'href="#server-passport-edit"' in brief
    else:
        note = (
            "Паспорт не проверен"
            if condition == "unknown"
            else "Проверка паспорта устарела; нужна сверка"
            if condition in {"stale", "custom_policy"}
            else "Паспорт проверен в пределах выбранного интервала"
        )
        assert note in brief and html.count(note) == 2
        if passport["checked_at"]:
            assert str(passport["checked_at"]) in brief
        assert "Источник &quot;&lt;&amp;&gt;&quot;" in brief
        assert source not in html and len(brief) < 4000
        assert 'href="#server-passport-source"' in brief
        assert '<details id="server-passport-source">' in html
    assert html.index('class="server-panel server-passport-brief"') < html.index("Что размещено")


def test_passport_cas_patch_preserves_children_legacy_facts_and_history(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        legacy = {
            "text": "Resources: RAM available 17732/24037 MiB",
            "source": "old",
            "checked_at": None,
        }
        save_server(
            connection,
            "host",
            0,
            {
                "name": "Host",
                "facts": [legacy],
                "sites": [_site()],
                "platform_profiles": [_profile()],
            },
        )
        before = get_server(connection, "host")
        updated = edit_server(connection, "host", 1, metadata={"passport": _passport()})
        assert updated["metadata"]["facts"] == [legacy]
        assert updated["metadata"]["sites"] == before["metadata"]["sites"]
        assert updated["metadata"]["platform_profiles"] == before["metadata"]["platform_profiles"]
        assert updated["history"][1:] == before["history"]
        assert updated["metadata"]["passport"]["swap_mib"] == 0
        with pytest.raises(ServerConflictError):
            edit_server(connection, "host", 1, metadata={"passport": {"cpu_cores": 1}})
        renamed = edit_server(connection, "host", 2, metadata={"name": "Renamed"})
        assert renamed["metadata"]["passport"] == updated["metadata"]["passport"]
        assert len(json.dumps(renamed, ensure_ascii=True).encode()) <= MAX_RESPONSE_BYTES
        assert "passport" not in renamed["history"][0]["data"]


@pytest.mark.parametrize(
    "passport",
    [
        {"password": "CANARY"},
        {"cpu_model": "password=CANARY"},
        {"storage": ["token: CANARY"]},
        {"cpu_cores": True},
        {"cpu_cores": 0},
        {"cpu_cores": 65537},
        {"memory_mib": 0},
        {"memory_mib": "24576"},
        {"swap_mib": -1},
        {"swap_mib": 1.5},
        {"storage": "CANARY"},
        {"storage": ["x"] * 17},
        {"cpu_model": "я" * 129},
        {"checked_at": "2026-10-05"},
        {"source": ""},
    ],
)
def test_invalid_passport_rolls_back_without_echoing_values(
    tmp_path: Path, passport: object
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(connection, "host", 0, {"name": "Host"})
        before = get_server(connection, "host")
        with pytest.raises(ServerError) as error:
            edit_server(connection, "host", 1, metadata={"passport": passport})
        assert "CANARY" not in str(error.value)
        assert get_server(connection, "host") == before


def test_unknown_passport_values_and_optional_fact_category_preserve_uncertainty() -> None:
    old = validate_server({"name": "Old"})
    assert "passport" not in old
    new = validate_server(
        {
            "name": "New",
            "passport": {},
            "facts": [
                {
                    "text": "Failed units: fixture.service",
                    "source": "fixture",
                    "checked_at": None,
                    "category": "services",
                }
            ],
        }
    )
    assert all(new["passport"][key] is None for key in ("cpu_cores", "memory_mib", "swap_mib"))
    assert new["passport"]["checked_at"] is None
    assert new["facts"][0]["category"] == "services"
    with pytest.raises(ServerError):
        validate_server(
            {
                "name": "New",
                "facts": [
                    {
                        "text": "x",
                        "source": "x",
                        "checked_at": None,
                        "category": "invalid",
                    }
                ],
            }
        )


def test_v29_barrier_preserves_exact_v28_card_and_event_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 28)
        initialize_database(database)
        with connect_database(database) as connection:
            save_server(
                connection,
                "host",
                0,
                {
                    "name": "Old",
                    "facts": [
                        {
                            "text": "unclassified",
                            "source": "import",
                            "checked_at": None,
                        }
                    ],
                },
            )
            rows = connection.execute("SELECT * FROM servers").fetchall()
            events = connection.execute("SELECT * FROM server_events").fetchall()
    assert initialize_database(database).schema_version == storage.SCHEMA_VERSION
    with connect_database(database) as connection:
        assert connection.execute("SELECT * FROM servers").fetchall() == rows
        assert connection.execute("SELECT * FROM server_events").fetchall() == events
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 28)
        with pytest.raises(storage.UnsupportedSchemaVersionError):
            connect_database(database)


class _Observations(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.unverified: list[dict[str, str | None]] = []
        self.source_text_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "details" and "server-unverified" in (values.get("class") or ""):
            self.unverified.append(values)

    def handle_data(self, data: str) -> None:
        if data == "shared audit source":
            self.source_text_count += 1


def test_renderer_groups_legacy_observations_without_inventing_inventory(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    texts = [
        "Resources: available RAM 17732/24037 MiB",
        "Failed units: broken.service",
        "Effective sshd defaults: port 22",
    ]
    with connect_database(database) as connection:
        save_server(
            connection,
            "host",
            0,
            {
                "name": "Host",
                "facts": [
                    *[
                        {
                            "text": text,
                            "source": "shared audit source",
                            "checked_at": "2026-10-05T15:00:00Z",
                        }
                        for text in texts
                    ],
                    {"text": "Unknown imported context", "source": "import", "checked_at": None},
                    {
                        "text": "Older observation from a different timezone",
                        "source": "earlier audit",
                        "checked_at": "2026-10-05T20:00:00+07:00",
                    },
                ],
            },
        )
        before = connection.execute("SELECT data,revision FROM servers").fetchone()
    html = render_servers_content(database, "host", {})
    parser = _Observations()
    parser.feed(html)
    assert len(parser.unverified) == 1 and "open" not in parser.unverified[0]
    assert parser.source_text_count == 1
    assert 'name="passport_memory_mib" value=""' in html
    assert all(text in html for text in texts)
    assert "Загрузка и использование ресурсов" in html and "Службы и контейнеры" in html
    assert html.index(texts[0]) < html.index("Older observation from a different timezone")
    with connect_database(database) as connection:
        assert connection.execute("SELECT data,revision FROM servers").fetchone() == before


def test_passport_http_form_save_validation_conflict_and_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(connection, "host", 0, {"name": "Host", "sites": [_site()]})
    manager = DashboardServerManager(database)
    try:
        base = manager.get_url()
        parsed = urlsplit(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        detail = base + "servers/host/"
        fields: dict[str, str | int] = {
            "action": "server_passport_save",
            "server_id": "host",
            "expected_revision": 1,
            **{
                "passport_" + key: str(value)
                for key, value in _passport().items()
                if key != "storage"
            },
            "passport_storage": "/dev/vda · 80 GiB · SSD · /",
        }
        status, _, html = _post(detail, fields, origin=origin)
        assert status == 200 and "24.00 ГиБ" in html.decode()
        with connect_database(database) as connection:
            current = get_server(connection, "host")
            assert current["metadata"]["passport"]["swap_mib"] == 0
            assert len(current["metadata"]["sites"]) == 1
        fields.update(expected_revision=2, passport_memory_mib="invalid")
        status, _, html = _post(detail, fields, origin=origin)
        assert (
            status == 400 and b'value="invalid"' in html and b'data-recovered-draft="true"' in html
        )
        fields.update(expected_revision=1, passport_memory_mib="32768")
        status, _, html = _post(detail, fields, origin=origin)
        assert status == 409 and b'name="passport_memory_mib" value="32768"' in html
        with connect_database(database) as connection:
            assert get_server(connection, "host") == current
    finally:
        manager.close()
    with connect_database(database) as connection:
        assert get_server(connection, "host")["metadata"]["passport"]["memory_mib"] == 24576


@pytest.mark.parametrize("checked_at", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:00-14:00"])
def test_accepted_date_limits_do_not_break_card_rendering(tmp_path: Path, checked_at: str) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        save_server(
            connection,
            "host",
            0,
            {
                "name": "Host",
                "passport": {"checked_at": checked_at},
                "facts": [
                    {"text": "unchecked", "source": "fixture", "checked_at": None},
                    {"text": "checked snapshot", "source": "fixture", "checked_at": checked_at},
                ],
            },
        )
    html = render_servers_content(database, "host", {})
    assert checked_at in html
    assert html.index("checked snapshot") < html.index("Непроверенные сведения")
