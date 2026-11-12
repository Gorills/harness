# ruff: noqa: RUF001
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness import storage
from harness.dashboard_server_map import mutate_form
from harness.dashboard_servers import render_servers_content
from harness.server_map import (
    collection_rows,
    freshness,
    links_for,
    mutate_map,
    object_detail,
    problems,
    read_map,
)
from harness.servers import ServerConflictError, ServerError, edit_server, get_server, save_server
from harness.storage import connect_database, initialize_database

NOW = datetime.now(UTC).isoformat()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "state.db"
    initialize_database(path)
    c = connect_database(path)
    save_server(c, "host", 0, {"name": "Тестовый сервер", "role": "Fixture"})
    yield c
    c.close()


def put(c: sqlite3.Connection, kind: str, key: str, **data: object) -> None:
    revision = c.execute("SELECT revision FROM servers WHERE id='host'").fetchone()[0]
    mutate_map(
        c,
        "host",
        revision,
        {
            "action": "upsert",
            "kind": kind,
            "object_id": key,
            "data": {"name": key, "source": "fixture", **data},
        },
    )


def relate(
    c: sqlite3.Connection, key: str, a: tuple[str, str], b: tuple[str, str], role: str = "uses"
) -> None:
    revision = c.execute("SELECT revision FROM servers WHERE id='host'").fetchone()[0]
    mutate_map(
        c,
        "host",
        revision,
        {
            "action": "link",
            "link": {
                "link_id": key,
                "from_kind": a[0],
                "from_id": a[1],
                "to_kind": b[0],
                "to_id": b[1],
                "role": role,
                "source": "fixture",
                "checked_at": NOW,
                "basis": "configuration",
                "reason": "Явная конфигурация",
            },
        },
    )


@pytest.mark.parametrize("count", [6, 256, 1000])
def test_full_collection_beyond_old_quota_and_search_linked_domain(
    db: sqlite3.Connection, count: int
) -> None:
    for n in range(count):
        put(db, "application", f"app-{n:04}", name=f"Приложение {n:04}", usage="active")
    put(db, "domain", "last-domain", name="витрина.example.test", hostname="витрина.example.test")
    last = f"app-{count - 1:04}"
    relate(db, "domain-link", ("application", last), ("domain", "last-domain"))
    revision = db.execute("SELECT revision FROM servers WHERE id='host'").fetchone()[0]
    offset = 0
    keys: list[str] = []
    while True:
        page = read_map(
            db,
            "host",
            collection="application",
            limit=100,
            offset=offset,
            expected_revision=revision,
        )
        assert len(json.dumps(page, ensure_ascii=True).encode()) <= 48 * 1024
        keys.extend(row["object_id"] for row in page["rows"])
        if page["complete"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(keys) == len(set(keys)) == count and keys[-1] == last
    assert read_map(db, "host", collection="application", query="витрина")["matched"] == 1
    assert (
        read_map(db, "host", collection="application", query=f"Приложение {count - 1:04}")["rows"][
            0
        ]["object_id"]
        == last
    )
    edit_server(db, "host", revision, metadata={"role": "Изменено"})
    with pytest.raises(ServerConflictError):
        read_map(db, "host", collection="application", offset=100, expected_revision=revision)
    assert get_server(db, "host")["sites_page"]["full_total"] == count


def test_partial_check_staging_idempotency_old_evidence_and_intent(db: sqlite3.Connection) -> None:
    put(
        db,
        "application",
        "shop",
        name="Магазин",
        usage="closed",
        owner="Оператор",
        purpose="Закрытая витрина",
    )
    put(db, "directory", "old", path="/srv/old", usage="unknown")
    revision = db.execute("SELECT revision FROM servers WHERE id='host'").fetchone()[0]
    coverage = [
        {
            "area": "application",
            "state": "partial",
            "source": "public probe",
            "reason": "За входом не проверялось",
        }
    ]
    start = {
        "action": "check_start",
        "check_id": "scan",
        "coverage": coverage,
        "source": "fixture",
        "started_at": NOW,
    }
    revision = mutate_map(db, "host", revision, start)["revision"]
    observation = {
        "kind": "application",
        "object_id": "shop",
        "values": {"availability": "login"},
        "source": "https login probe",
        "checked_at": NOW,
        "basis": "probe",
        "reason": "Страница входа отвечает",
    }
    portion = {
        "action": "check_append",
        "check_id": "scan",
        "batch": 0,
        "observations": [observation],
    }
    revision = mutate_map(db, "host", revision, portion)["revision"]
    mutate_map(db, "host", revision, portion)
    assert object_detail(db, "host", "application", "shop")["availability"] == "unknown"
    with pytest.raises(ServerError):
        mutate_map(
            db,
            "host",
            revision,
            {**portion, "observations": [{**observation, "values": {"availability": "error"}}]},
        )
    with pytest.raises(ServerError):
        mutate_map(
            db,
            "host",
            revision,
            {
                **portion,
                "batch": 1,
                "observations": [{**observation, "values": {"usage": "active"}}],
            },
        )
    publication = {
        "action": "check_publish",
        "check_id": "scan",
        "coverage": coverage,
        "finished_at": NOW,
    }
    result = mutate_map(db, "host", revision, publication)
    current = object_detail(db, "host", "application", "shop")
    assert (
        current["availability"] == "login"
        and current["usage"] == "closed"
        and current["owner"] == "Оператор"
    )
    assert object_detail(db, "host", "directory", "old")["usage"] == "unknown"
    assert mutate_map(db, "host", result["revision"], publication)["already_published"]
    older = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    old_revision = mutate_map(
        db, "host", result["revision"], {**start, "check_id": "old-report", "started_at": older}
    )["revision"]
    mutate_map(
        db,
        "host",
        old_revision,
        {
            **portion,
            "check_id": "old-report",
            "observations": [
                {**observation, "checked_at": older, "values": {"availability": "error"}}
            ],
        },
    )
    mutate_map(
        db,
        "host",
        db.execute("SELECT revision FROM servers WHERE id='host'").fetchone()[0],
        {**publication, "check_id": "old-report", "finished_at": older},
    )
    assert object_detail(db, "host", "application", "shop")["availability"] == "login"


def test_check_conflict_rolls_back_all_objects_and_bad_portion(db: sqlite3.Connection) -> None:
    put(db, "domain", "dom", hostname="example.test")
    revision = get_server(db, "host")["revision"]
    coverage = [{"area": "domain", "state": "complete", "source": "nginx", "reason": "Один файл"}]
    mutate_map(
        db,
        "host",
        revision,
        {
            "action": "check_start",
            "check_id": "scan",
            "coverage": coverage,
            "source": "fixture",
            "started_at": NOW,
        },
    )
    revision = get_server(db, "host")["revision"]
    mutate_map(
        db,
        "host",
        revision,
        {
            "action": "check_append",
            "check_id": "scan",
            "batch": 1,
            "observations": [
                {
                    "kind": "domain",
                    "object_id": "dom",
                    "values": {"availability": "available"},
                    "source": "fixture",
                    "checked_at": NOW,
                    "basis": "probe",
                    "reason": "Expected page",
                }
            ],
        },
    )
    revision = get_server(db, "host")["revision"]
    publish = {
        "action": "check_publish",
        "check_id": "scan",
        "coverage": coverage,
        "finished_at": NOW,
    }
    with pytest.raises(ServerError, match="contiguous"):
        mutate_map(db, "host", revision, publish)
    edit_server(db, "host", revision, metadata={"role": "Operator change"})
    with pytest.raises(ServerConflictError):
        mutate_map(db, "host", revision + 1, publish)
    assert object_detail(db, "host", "domain", "dom")["availability"] == "unknown"
    assert read_map(db, "host", collection="checks")["rows"][0]["state"] == "draft"


def test_directory_classification_has_reasons_and_active_links_block_leftovers(
    db: sqlite3.Connection,
) -> None:
    put(db, "application", "app", usage="active")
    put(db, "directory", "dir", path="/srv/app")
    relate(db, "code", ("application", "app"), ("directory", "dir"), "code")
    with pytest.raises(ServerError):
        put(
            db,
            "directory",
            "dir",
            path="/srv/app",
            usage="leftover",
            basis="operator",
            reason="Old files",
        )
    with pytest.raises(ServerError, match="active application"):
        put(
            db,
            "directory",
            "dir",
            path="/srv/app",
            usage="leftover",
            basis="operator",
            reason="Закрыто",
            closed_application="Old shop",
            decision_owner="Operator",
            decision_at=NOW,
            links_checked_at=NOW,
        )
    with pytest.raises(ServerError):
        put(
            db,
            "directory",
            "backup",
            path="/srv/backup",
            usage="retained",
            basis="operator",
            reason="Backup",
        )
    put(
        db,
        "directory",
        "backup",
        path="/srv/backup",
        usage="retained",
        retention="14 дней",
        basis="operator",
        reason="Резервная копия",
        size_bytes=200,
    )
    put(db, "directory", "small", path="/srv/small", size_bytes=20)
    put(db, "directory", "unknown-size", path="/srv/unknown")
    rows, _ = collection_rows(db, "host", "directory", sort="size_bytes", descending=True)
    assert [row["object_id"] for row in rows[:2]] == ["backup", "small"]
    with pytest.raises(ServerError, match="has links"):
        mutate_map(
            db,
            "host",
            get_server(db, "host")["revision"],
            {"action": "delete", "kind": "application", "object_id": "app"},
        )
    assert links_for(db, "host", "application", "app")


def test_certificate_versions_and_use_distinguish_file_config_observation(
    db: sqlite3.Connection,
) -> None:
    put(db, "certificate", "cert", renewal="automatic", deployment="manual", owner="Оператор")
    assert object_detail(db, "host", "certificate", "cert")["renewal_state"] == "unknown"
    with pytest.raises(ServerError, match="dated probe"):
        put(db, "certificate", "fake", renewal="automatic", renewal_state="verified")
    with pytest.raises(ServerError, match="SNI"):
        put(
            db,
            "certificate_use",
            "bad",
            endpoint="https://example.test",
            observed_fingerprints=["old"],
        )
    put(
        db,
        "certificate_use",
        "use",
        endpoint="https://example.test",
        address="192.0.2.10",
        sni="example.test",
        port=443,
        observed_at=NOW,
        certificate_file="/etc/cert/fullchain.pem",
        key_reference="/etc/cert/privkey.pem",
        configured_fingerprint="new",
        file_fingerprint="new",
        observed_fingerprints=["old"],
        deployment="manual",
    )
    obj = object_detail(db, "host", "certificate_use", "use")
    assert "different_certificate" in problems(obj)
    put(
        db,
        "certificate_version",
        "v",
        fingerprint="old",
        sans=["example.test"],
        not_after=(datetime.now(UTC) + timedelta(days=6)).isoformat(),
    )
    assert "expires_soon" in problems(object_detail(db, "host", "certificate_version", "v"))
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    obj["evidence"] = [
        {
            "field": "observed_fingerprints",
            "source": "probe",
            "checked_at": stale,
            "basis": "probe",
            "reason": "Old endpoint check",
        }
    ]
    obj["checked_at"] = NOW
    assert freshness(obj) == "stale"


@pytest.mark.parametrize(
    "kind,data",
    [
        ("certificate", {"client": "password=CANARY"}),
        ("certificate_use", {"key_reference": "-----BEGIN PRIVATE KEY-----CANARY"}),
        ("service", {"identifier": "postgres://user:CANARY@host/db"}),
        ("domain", {"hostname": "example.test", "raw_config": "CANARY"}),
    ],
)
def test_negative_disclosure(db: sqlite3.Connection, kind: str, data: dict[str, object]) -> None:
    with pytest.raises(ServerError) as exc:
        put(db, kind, "bad", **data)
    assert "CANARY" not in str(exc.value)
    assert db.execute("SELECT count(*) FROM server_map_objects").fetchone()[0] == 0


def test_v30_migration_exact_snapshot_identity_events_and_unknown_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 29)
        initialize_database(path)
        with connect_database(path) as c:
            save_server(
                c,
                "host",
                0,
                {
                    "name": "Old",
                    "sites": [
                        {
                            "site_id": "app",
                            "name": "App",
                            "source": "import",
                            "domains": ["app.example.test"],
                            "paths": ["/srv/app"],
                        }
                    ],
                    "passport": {"hostname": "host"},
                },
            )
            raw = c.execute("SELECT data FROM servers").fetchone()[0]
            history = c.execute("SELECT * FROM server_events").fetchall()
    initialize_database(path)
    with connect_database(path) as c:
        assert c.execute("SELECT data FROM servers").fetchone()[0] == raw
        assert c.execute("SELECT data FROM server_legacy_snapshots").fetchone()[0] == raw
        assert c.execute("SELECT * FROM server_events").fetchall() == history
        assert get_server(c, "host")["metadata"]["sites"][0]["site_id"] == "app"
        assert object_detail(c, "host", "application", "app")["usage"] == "unknown"
        rows = read_map(c, "host", collection="domain")["rows"]
        assert len(rows) == 1 and rows[0]["availability"] == "unknown"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_dashboard_all_compact_rows_selected_editor_and_typed_form(tmp_path: Path) -> None:
    path = tmp_path / "ui.db"
    initialize_database(path)
    with connect_database(path) as c:
        save_server(
            c,
            "host",
            0,
            {
                "name": "Fixture",
                "sites": [
                    {"site_id": f"app-{n:02}", "name": f"Application {n:02}", "source": "fixture"}
                    for n in range(35)
                ],
            },
        )
    html = render_servers_content(path, "host", {"view": "sites"})
    assert "Все 35" in html and "Application 34" in html
    assert 'name="map_kind"' not in html  # no editor per row
    selected = render_servers_content(
        path, "host", {"view": "sites", "collection": "application", "object_id": "app-34"}
    )
    assert 'name="object_key" value="app-34"' in selected
    with connect_database(path) as c:
        current = object_detail(c, "host", "application", "app-34")
        fields = {
            "action": "server_map_save",
            "server_id": "host",
            "expected_revision": "1",
            "map_kind": "application",
            "object_key": "app-34",
            "object_id": "app-34",
        }
        from harness.server_map import COMMON, FIELDS

        for name in COMMON - {"evidence"}:
            fields["map_" + name] = str(current[name] or "")
        for name, t in FIELDS["application"].items():
            value = current[name]
            fields["map_" + name] = (
                "\n".join(map(str, value))
                if t in {"list", "ports"}
                else ""
                if t == "stack"
                else str(value or "")
            )
        fields["map_purpose"] = "Понятная витрина"
        mutate_form(c, "host", 1, fields)
        assert object_detail(c, "host", "application", "app-34")["purpose"] == "Понятная витрина"
        assert get_server(c, "host")["sites_page"]["full_total"] == 35


def test_byte_reduced_pages_and_relation_details_make_real_progress(db: sqlite3.Connection) -> None:
    for n in range(80):
        put(db, "service", f"s-{n:03}", name="С" * 80, purpose="Я" * 500, reason="Я" * 150)
    revision = get_server(db, "host")["revision"]
    first = read_map(db, "host", collection="service", limit=100, expected_revision=revision)
    assert 0 < len(first["rows"]) < 80
    assert first["next_offset"] == len(first["rows"])
    assert len(json.dumps(first, ensure_ascii=True).encode()) <= 48 * 1024
    keys = [row["object_id"] for row in first["rows"]]
    offset = first["next_offset"]
    while offset is not None:
        page = read_map(
            db, "host", collection="service", limit=100, offset=offset, expected_revision=revision
        )
        assert page["rows"]
        keys.extend(row["object_id"] for row in page["rows"])
        offset = page["next_offset"]
    assert len(set(keys)) == 80
    put(db, "application", "a")
    for n in range(80):
        relate(db, f"rel-{n:03}", ("application", "a"), ("service", f"s-{n:03}"))
        raw = db.execute(
            "SELECT data FROM server_map_links WHERE server_id='host' AND link_id=?",
            (f"rel-{n:03}",),
        ).fetchone()[0]
        data = json.loads(raw)
        data["reason"] = "Я" * 150
        db.execute(
            "UPDATE server_map_links SET data=? WHERE server_id='host' AND link_id=?",
            (json.dumps(data), f"rel-{n:03}"),
        )
    revision = get_server(db, "host")["revision"]
    offset = 0
    found: list[str] = []
    while True:
        page = read_map(
            db,
            "host",
            collection="application",
            object_id="a",
            links_offset=offset,
            expected_revision=revision,
        )
        assert page["links_total"] == 80 and page["links"]
        assert len(json.dumps(page, ensure_ascii=True).encode()) <= 48 * 1024
        found.extend(row["link_id"] for row in page["links"])
        if page["links_next_offset"] is None:
            break
        assert page["links_next_offset"] > offset
        offset = page["links_next_offset"]
    assert len(found) == len(set(found)) == 80


def test_custom_freshness_policy_and_draft_revision_pin(db: sqlite3.Connection) -> None:
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    put(db, "service", "svc", state="running", checked_at=stale)
    obj = object_detail(db, "host", "service", "svc")
    assert freshness(obj) == "stale"
    policy = {
        "health_hours": 48,
        "inventory_hours": 168,
        "passport_days": 30,
        "expiry_days": [14, 7],
    }
    assert freshness(obj, policy) == "recent"
    revision = get_server(db, "host")["revision"]
    edit_server(db, "host", revision, metadata={"map_policy": policy})
    revision += 1
    assert read_map(db, "host", collection="service")["policy"] == policy
    result = mutate_map(
        db,
        "host",
        revision,
        {
            "action": "check_start",
            "check_id": "draft",
            "coverage": [
                {"area": "service", "state": "partial", "source": "fixture", "reason": "One unit"}
            ],
            "source": "fixture",
            "started_at": NOW,
        },
    )
    assert result["revision"] == revision + 1
    with pytest.raises(ServerConflictError):
        read_map(db, "host", collection="checks", expected_revision=revision)
