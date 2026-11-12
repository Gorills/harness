from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import test_server_map
from test_dashboard_actions import _post

from harness.dashboard import DashboardServerManager
from harness.dashboard_server_map import mutate_form
from harness.server_map import (
    FIELDS,
    MAX_LINKS,
    MAX_OBJECTS,
    freshness,
    links_for,
    mutate_map,
    object_detail,
    read_map,
    validate_object,
)
from harness.servers import ServerError, edit_server, get_server, save_server

NOW = test_server_map.NOW
db = test_server_map.db
put = test_server_map.put
relate = test_server_map.relate


def revision(c: sqlite3.Connection) -> int:
    value = c.execute("SELECT revision FROM servers WHERE id='host'").fetchone()[0]
    assert isinstance(value, int)
    return value


def state(c: sqlite3.Connection) -> list[list[tuple[object, ...]]]:
    return [
        c.execute(f"SELECT * FROM {table}").fetchall()
        for table in (
            "servers",
            "server_map_objects",
            "server_map_links",
            "server_map_checks",
            "server_map_batches",
            "server_events",
        )
    ]


def publish(
    c: sqlite3.Connection, kind: str, key: str, values: dict[str, object], checked: str
) -> None:
    coverage = [{"area": kind, "state": "partial", "source": "fixture", "reason": "partial"}]
    check = f"scan-{revision(c)}"
    for op in (
        {
            "action": "check_start",
            "check_id": check,
            "coverage": coverage,
            "source": "fixture",
            "started_at": NOW,
        },
        {
            "action": "check_append",
            "check_id": check,
            "batch": 0,
            "observations": [
                {
                    "kind": kind,
                    "object_id": key,
                    "values": values,
                    "source": "new probe",
                    "checked_at": checked,
                    "basis": "probe",
                    "reason": "observed",
                }
            ],
        },
        {"action": "check_publish", "check_id": check, "coverage": coverage, "finished_at": NOW},
    ):
        mutate_map(c, "host", revision(c), op)


@pytest.mark.parametrize("reverse", [False, True])
def test_positive_links_and_application_reactivation_preserve_old_leftover(
    db: sqlite3.Connection, reverse: bool
) -> None:
    put(db, "application", "app", usage="active")
    put(
        db,
        "directory",
        "dir",
        path="/srv/old",
        usage="leftover",
        basis="operator",
        reason="closed",
        closed_application="former",
        decision_owner="operator",
        decision_at=NOW,
        links_checked_at=NOW,
    )
    endpoints = [("application", "app"), ("directory", "dir")]
    if reverse:
        endpoints.reverse()
    relate(db, "use", endpoints[0], endpoints[1])
    assert object_detail(db, "host", "directory", "dir")["usage"] == "leftover"
    put(db, "application", "app", usage="closed")
    put(db, "application", "app", usage="active")
    assert object_detail(db, "host", "application", "app")["usage"] == "active"
    before = state(db)
    # Explicitly reconfirming the old decision still requires resolving the conflict.
    with pytest.raises(ServerError, match="active application"):
        put(
            db,
            "directory",
            "dir",
            path="/srv/old",
            usage="leftover",
            basis="operator",
            reason="closed",
            closed_application="former",
            decision_owner="operator",
            decision_at=NOW,
            links_checked_at=NOW,
        )
    assert state(db) == before


def test_common_provenance_blocks_old_report_and_preserves_unchecked_fields(
    db: sqlite3.Connection,
) -> None:
    put(db, "application", "app", availability="available", checked_at=NOW, basis="probe")
    publish(
        db,
        "application",
        "app",
        {"availability": "error"},
        (datetime.now(UTC) - timedelta(days=30)).isoformat(),
    )
    app = object_detail(db, "host", "application", "app")
    assert app["availability"] == "available" and app["checked_at"] == NOW
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    put(
        db,
        "domain",
        "domain",
        hostname="old.example.test",
        availability="available",
        https="valid",
        checked_at=old,
        basis="probe",
    )
    publish(db, "domain", "domain", {"dns": ["192.0.2.1"]}, NOW)
    domain = object_detail(db, "host", "domain", "domain")
    assert freshness(domain) == "stale"
    evidence = {e["field"]: e for e in domain["evidence"]}
    assert evidence["availability"]["checked_at"] == evidence["https"]["checked_at"] == old
    assert evidence["dns"]["checked_at"] == NOW


@pytest.mark.parametrize(
    "kind,values,change",
    [
        (
            "application",
            {"availability": "available", "purpose": "old purpose", "owner": "operator"},
            "purpose",
        ),
        ("service", {"state": "running", "identifier": "unit"}, "purpose"),
        ("certificate", {"renewal_state": "configured", "procedure": "manual"}, "procedure"),
        (
            "certificate_use",
            {
                "https": "valid",
                "observed_fingerprints": ["TEST"],
                "address": "192.0.2.1",
                "sni": "example.test",
                "port": 443,
                "observed_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
            },
            "procedure",
        ),
    ],
)
def test_partial_form_edit_retains_effective_field_dates_and_omitted_values(
    db: sqlite3.Connection, kind: str, values: dict[str, object], change: str
) -> None:
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    put(db, kind, "obj", **values, checked_at=old, basis="probe", reason="old probe")
    before = object_detail(db, "host", kind, "obj")
    mutate_form(
        db,
        "host",
        revision(db),
        {
            "action": "server_map_save",
            "server_id": "host",
            "expected_revision": str(revision(db)),
            "map_kind": kind,
            "object_key": "obj",
            "object_id": "obj",
            "map_" + change: "changed",
            "map_checked_at": NOW,
            "map_source": "operator",
            "map_basis": "operator",
        },
    )
    after = object_detail(db, "host", kind, "obj")
    assert freshness(after) == "stale"
    for field in FIELDS[kind]:
        if field != change:
            assert after[field] == before[field]
    for item in after["evidence"]:
        if item["field"] != change:
            assert item["checked_at"] == (
                before.get("observed_at") if item["field"] == "observed_fingerprints" else old
            )


def test_unobserved_health_does_not_adopt_new_description_date(db: sqlite3.Connection) -> None:
    put(db, "application", "app", availability="available")
    mutate_form(
        db,
        "host",
        revision(db),
        {
            "action": "server_map_save",
            "server_id": "host",
            "expected_revision": str(revision(db)),
            "map_kind": "application",
            "object_key": "app",
            "object_id": "app",
            "map_purpose": "new",
            "map_checked_at": NOW,
        },
    )
    assert freshness(object_detail(db, "host", "application", "app")) == "unknown"


def test_normalized_delete_unlink_survive_unrelated_legacy_writes(db: sqlite3.Connection) -> None:
    edit_server(db, "host", revision(db), metadata={"domains": ["old.example.test"]})
    domain = read_map(db, "host", collection="domain")["rows"][0]["object_id"]
    mutate_map(
        db, "host", revision(db), {"action": "delete", "kind": "domain", "object_id": domain}
    )
    edit_server(db, "host", revision(db), metadata={"role": "edited"})
    assert read_map(db, "host", collection="domain")["total"] == 0
    site = {
        "site_id": "legacy",
        "name": "legacy",
        "source": "fixture",
        "domains": ["legacy.example.test"],
    }
    edit_server(db, "host", revision(db), site=site)
    old_link = links_for(db, "host", "application", "legacy")[0]["link_id"]
    mutate_map(db, "host", revision(db), {"action": "unlink", "link_id": old_link})
    edit_server(db, "host", revision(db), site={**site, "purpose": "new purpose"})
    edit_server(
        db, "host", revision(db), profile={"profile_id": "p", "name": "p", "source": "fixture"}
    )
    assert not links_for(db, "host", "application", "legacy")
    edit_server(
        db,
        "host",
        revision(db),
        site={**site, "domains": ["legacy.example.test", "new.example.test"]},
    )
    assert len(links_for(db, "host", "application", "legacy")) == 1
    assert read_map(db, "host", collection="domain")["total"] == 2


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
@pytest.mark.parametrize("location", ["common", "evidence", "link", "start", "finish"])
def test_out_of_utc_range_dates_rejected_atomically(
    db: sqlite3.Connection, stamp: str, location: str
) -> None:
    put(db, "service", "svc", state="running")
    put(db, "application", "app")
    coverage = [{"area": "service", "state": "partial", "source": "fixture", "reason": "partial"}]
    op: dict[str, object]
    if location in {"common", "evidence"}:
        data = (
            {"name": "svc", "source": "fixture", "state": "running", "checked_at": stamp}
            if location == "common"
            else {
                "name": "svc",
                "source": "fixture",
                "evidence": [
                    {
                        "field": "state",
                        "source": "fixture",
                        "basis": "probe",
                        "reason": "observed",
                        "checked_at": stamp,
                    }
                ],
            }
        )
        op = {"action": "upsert", "kind": "service", "object_id": "svc", "data": data}
    elif location == "link":
        op = {
            "action": "link",
            "link": {
                "link_id": "link",
                "from_kind": "application",
                "from_id": "app",
                "to_kind": "service",
                "to_id": "svc",
                "role": "uses",
                "source": "fixture",
                "basis": "probe",
                "reason": "observed",
                "checked_at": stamp,
            },
        }
    else:
        op = {
            "action": "check_start",
            "check_id": "check",
            "coverage": coverage,
            "source": "fixture",
            "started_at": stamp,
        }
        if location == "finish":
            mutate_map(db, "host", revision(db), {**op, "started_at": NOW})
            op = {
                "action": "check_publish",
                "check_id": "check",
                "coverage": coverage,
                "finished_at": stamp,
            }
    before = state(db)
    with pytest.raises(ServerError, match="UTC range"):
        mutate_map(db, "host", revision(db), op)
    assert state(db) == before
    assert read_map(db, "host", collection="service")["total"] == 1


@pytest.mark.parametrize("quota", ["objects", "links"])
def test_legacy_growth_quota_whole_transaction_and_non_growing_overquota(
    db: sqlite3.Connection, quota: str
) -> None:
    if quota == "objects":
        data = json.dumps(validate_object("service", {"name": "service", "source": "fixture"}))
        db.executemany(
            "INSERT INTO server_map_objects VALUES ('host','service',?,?)",
            [(f"svc-{n}", data) for n in range(MAX_OBJECTS)],
        )
        site: dict[str, object] = {"site_id": "app", "name": "app", "source": "fixture"}
    else:
        put(db, "application", "app")
        put(db, "domain", "domain", hostname="fixture.example.test")
        data = json.dumps(
            {
                "role": "uses",
                "source": "fixture",
                "basis": "reference",
                "checked_at": None,
                "reason": "old",
            }
        )
        db.executemany(
            "INSERT INTO server_map_links VALUES ('host',?,'application','app','domain','domain',?)",
            [(f"link-{n}", data) for n in range(MAX_LINKS)],
        )
        site = {
            "site_id": "app",
            "name": "app",
            "source": "fixture",
            "domains": ["new.example.test"],
        }
    before = state(db)
    with pytest.raises(ServerError, match="quota"):
        edit_server(db, "host", revision(db), site=site)
    assert state(db) == before
    if quota == "objects":
        db.execute(
            "INSERT INTO server_map_objects VALUES ('host','service','grandfather',?)", (data,)
        )
    else:
        db.execute(
            "INSERT INTO server_map_links VALUES ('host','grandfather','application','app','domain','domain',?)",
            (data,),
        )
    edit_server(db, "host", revision(db), metadata={"role": "non-growing edit"})
    assert get_server(db, "host")["metadata"]["role"] == "non-growing edit"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_utc_offset_dates_use_instant_order_for_prior_evidence(db: sqlite3.Connection) -> None:
    put(
        db, "service", "svc", state="running", checked_at="2026-10-06T01:00:00+07:00", basis="probe"
    )
    publish(db, "service", "svc", {"state": "stopped"}, "2026-10-05T17:59:00Z")
    assert object_detail(db, "host", "service", "svc")["state"] == "running"
    publish(db, "service", "svc", {"state": "stopped"}, "2026-10-05T18:00:00Z")
    assert object_detail(db, "host", "service", "svc")["state"] == "stopped"


def test_map_link_error_keeps_its_view_and_draft_without_echo_into_new_link(
    db: sqlite3.Connection,
) -> None:
    from harness.dashboard_servers import render_servers_content

    put(db, "application", "app")
    put(db, "domain", "domain", hostname="fixture.example.test")
    relate(db, "rel", ("application", "app"), ("domain", "domain"))
    draft = {
        "action": "server_map_link",
        "server_id": "host",
        "expected_revision": str(revision(db)),
        "link_key": "rel",
        "from_kind": "application",
        "from_id": "app",
        "to_object": "domain:domain",
        "link_role": "TEST link draft",
        "link_source": "fixture",
        "link_basis": "configuration",
        "link_checked_at": "invalid",
        "link_reason": "TEST link reason",
    }
    html = render_servers_content(
        Path(db.execute("PRAGMA database_list").fetchone()[2]),
        "host",
        {"view": "sites", "collection": "application", "object_id": "app"},
        draft=draft,
        error="checked_at must include timezone",
    )
    assert 'data-server-initial-view="sites"' in html
    assert html.count('value="TEST link draft"') == 1
    assert "data-server-error-summary" in html
    assert "data-server-cancel" in html


def test_public_collection_search_uses_human_values_before_paging(db: sqlite3.Connection) -> None:
    for number in range(35):
        put(db, "application", f"app-{number:02}", purpose='Группа "витрина"', usage="active")
    last = "app-34"
    name = 'Приложение "&<> \\Кириллица'
    put(
        db,
        "application",
        last,
        name=name,
        purpose='Группа "витрина"',
        usage="active",
        dependencies=['Соседнее "значение\\каталог'],
        evidence=[
            {
                "field": "availability",
                "source": 'Проверка "браузер\\точка',
                "checked_at": NOW,
                "basis": "probe",
                "reason": "Тестовое наблюдение",
            }
        ],
    )
    put(db, "domain", "domain", name='Домен "витрины"', hostname="fixture.example.test")
    put(db, "directory", "dir", path='/srv/Код "витрина"\\релиз')
    put(db, "service", "svc", identifier='Служба "витрина"\\unit')
    for kind, key in [("domain", "domain"), ("directory", "dir"), ("service", "svc")]:
        relate(db, f"related-{kind}", ("application", last), (kind, key))
    for query in (
        name,
        name.upper(),
        'Соседнее "значение\\каталог',
        'Проверка "браузер\\точка',
        'Домен "витрины"',
        "fixture.example.test",
        '/srv/Код "витрина"\\релиз',
        'Служба "витрина"\\unit',
    ):
        page = get_server(db, "host", collection="application", query=query, limit=5)
        assert (page["total"], page["matched"]) == (35, 1)
        assert [row["object_id"] for row in page["rows"]] == [last]
    # Neighboring fields are separate values, not serialized punctuation or a joined search span.
    assert (
        get_server(db, "host", collection="application", query='витрина", "usage')["matched"] == 0
    )
    pinned = revision(db)
    pages = [
        get_server(
            db,
            "host",
            collection="application",
            query='Группа "витрина"',
            status="active",
            sort="name",
            limit=25,
            offset=offset,
            expected_revision=pinned,
        )
        for offset in (0, 25)
    ]
    assert [page["matched"] for page in pages] == [35, 35]
    assert pages[0]["next_offset"] == 25 and pages[1]["complete"]
    assert len({row["object_id"] for page in pages for row in page["rows"]}) == 35
    assert all(len(json.dumps(page, ensure_ascii=True).encode()) <= 48 * 1024 for page in pages)


@pytest.mark.parametrize("incoming", [False, True])
@pytest.mark.parametrize("failure", ["date", "revision", "unlink"])
def test_http_rejected_link_retains_selected_endpoint_and_one_form(
    db: sqlite3.Connection, incoming: bool, failure: str
) -> None:
    from test_dashboard_servers import _ServerControls

    from harness.dashboard_servers import render_servers_content

    put(db, "application", "app")
    put(db, "service", "svc", state="running")
    relate(db, "rel", ("application", "app"), ("service", "svc"))
    database = Path(db.execute("PRAGMA database_list").fetchone()[2])
    kind, key, view = (
        ("service", "svc", "services") if incoming else ("application", "app", "sites")
    )
    filters = {
        "view": view,
        "collection": kind,
        "object_id": key,
        "map_q": "kept filter",
        "map_sort": "name",
        "map_size": "25",
    }
    current = revision(db)
    fields: dict[str, str | int] = {
        "action": "server_map_link",
        "server_id": "host",
        "expected_revision": str(current),
        "link_key": "rel",
        "from_kind": "application",
        "from_id": "app",
        "to_object": "service:svc",
        "link_role": "TEST draft",
        "link_source": "fixture",
        "link_basis": "configuration",
        "link_checked_at": "2026-10-06T13:00:00",
        "link_reason": "TEST reason",
    }
    if failure != "date":
        fields.update(expected_revision=str(current - 1), link_checked_at=NOW)
    if failure == "unlink":
        fields = {
            key: fields[key] for key in ("action", "server_id", "expected_revision", "link_key")
        }
        fields["action"] = "server_map_unlink"
    before = state(db)
    manager = DashboardServerManager(database)
    try:
        url = manager.get_url() + "servers/host/?" + urlencode(filters)
        origin = "http://" + urlsplit(url).netloc
        status, _, body = _post(url, fields, origin=origin)
        html = body.decode()
        assert status == (400 if failure == "date" else 409)
        assert state(db) == before
        assert f'data-server-initial-view="{view}"' in html
        assert 'role="alert" tabindex="-1" data-server-error-summary' in html
        controls = _ServerControls()
        controls.feed(html)
        assert parse_qs(urlsplit(controls.page_url).query) == {k: [v] for k, v in filters.items()}
        if failure != "unlink":
            assert html.count('value="TEST draft"') == 1
            assert "<details open><summary>Изменить связь" in html
        else:
            assert f'name="expected_revision" value="{current - 1}"' in html
        if failure == "date":
            assert "часов" in html or "пояс" in html
        corrected = {**fields, "expected_revision": str(current)}
        if failure != "unlink":
            corrected["link_checked_at"] = NOW
        status, _, _ = _post(url, corrected, origin=origin)
        assert status == 200 and revision(db) == current + 1
        assert len(links_for(db, "host", kind, key)) == (0 if failure == "unlink" else 1)
        # The native response and subsequent GET use the same selected endpoint.
        assert f'id="map-{kind}-{key}"' in render_servers_content(database, "host", filters.copy())
    finally:
        manager.close()


def test_link_error_context_rejects_other_server_or_unrelated_selection(
    db: sqlite3.Connection,
) -> None:
    from harness.dashboard_servers import render_servers_content

    put(db, "application", "app")
    put(db, "service", "svc")
    put(db, "service", "unrelated")
    relate(db, "rel", ("application", "app"), ("service", "svc"))
    save_server(db, "other", 0, {"name": "Other"})
    mutate_map(
        db,
        "other",
        1,
        {
            "action": "upsert",
            "kind": "service",
            "object_id": "other-only",
            "data": {"name": "OTHER CANARY", "source": "fixture"},
        },
    )
    database = Path(db.execute("PRAGMA database_list").fetchone()[2])
    for key in ("unrelated", "other-only"):
        filters = {"view": "services", "collection": "service", "object_id": key}
        html = render_servers_content(
            database,
            "host",
            filters,
            draft={"action": "server_map_unlink", "link_key": "rel", "expected_revision": "1"},
            error="Server revision conflict",
        )
        assert filters["collection"] == "application" and filters["object_id"] == "app"
        assert "OTHER CANARY" not in html
