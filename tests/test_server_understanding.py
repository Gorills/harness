# ruff: noqa: RUF001
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest
import test_server_map
from test_dashboard_actions import _post
from test_server_map_regressions import publish, revision, state

from harness import storage
from harness.dashboard import DashboardServerManager
from harness.dashboard_servers import _render_storage, render_servers_content
from harness.server_map import (
    CONSUMER_SOURCES,
    InventoryView,
    collection_rows,
    mutate_map,
    object_detail,
    problems,
    read_map,
    status_of,
    validate_object,
)
from harness.servers import ServerError, save_server

NOW = test_server_map.NOW
db = test_server_map.db
put = test_server_map.put
relate = test_server_map.relate


def test_service_state_and_directory_consumers_do_not_change_operator_intent(
    db: sqlite3.Connection,
) -> None:
    put(db, "application", "app", purpose="Операторское назначение", usage="closed")
    put(db, "service", "worker", state="running", basis="process", checked_at=NOW)
    put(
        db,
        "directory",
        "root",
        path="/home/capital-living",
        reason="Старое описание",
        basis="import",
    )
    relate(db, "runtime", ("application", "app"), ("service", "worker"), role="runtime")
    relate(db, "mount", ("service", "worker"), ("directory", "root"), role="code")
    before = state(db)
    assert read_map(db, "host", collection="service")["rows"][0]["status"] == "running"
    assert read_map(db, "host", collection="service", status="unknown")["matched"] == 0
    detail = read_map(db, "host", collection="directory", object_id="root")
    assert detail["understanding"]["technical_usage"] == "current"
    assert detail["understanding"]["effective_role"] == "code"
    assert "worker" in detail["understanding"]["observed_reason"]
    assert detail["object"]["usage"] == "unknown"
    assert "Размер не измерен." in detail["understanding"]["gaps"]
    assert read_map(db, "host", collection="directory", status="current")["matched"] == 1
    assert read_map(db, "host", collection="directory", status="unknown")["matched"] == 0
    assert read_map(db, "host", collection="application")["rows"][0]["technical_usage"] == "current"
    assert object_detail(db, "host", "application", "app")["purpose"] == "Операторское назначение"
    assert state(db) == before


@pytest.mark.parametrize("basis", ["import", "reference", "unknown", "operator"])
def test_unverified_links_never_certify_current_consumption(
    db: sqlite3.Connection, basis: str
) -> None:
    put(db, "service", "worker", state="running", checked_at=NOW, basis="process")
    put(db, "directory", "old", path="/home/old")
    relation = {
        "link_id": "reference",
        "from_kind": "service",
        "from_id": "worker",
        "to_kind": "directory",
        "to_id": "old",
        "role": "uses",
        "source": "fixture",
        "checked_at": NOW,
        "basis": basis,
        "reason": "Не проверялось",
    }
    mutate_map(db, "host", revision(db), {"action": "link", "link": relation})
    assert read_map(db, "host", collection="directory")["rows"][0]["technical_usage"] == "unknown"


def test_partial_absence_and_stale_runtime_remain_unknown_or_configured(
    db: sqlite3.Connection,
) -> None:
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    put(
        db,
        "directory",
        "old",
        path="/home/tverskoy27",
        context="Docker проверен частично; cron неизвестен",
    )
    put(db, "service", "worker", state="running", checked_at=old, basis="process")
    relate(db, "mount", ("service", "worker"), ("directory", "old"))
    observed = read_map(db, "host", collection="directory", object_id="old")["understanding"]
    assert observed["technical_usage"] == "configured"
    assert "cron" in observed["gaps"][0]
    assert read_map(db, "host", collection="directory", status="candidate")["matched"] == 0
    put(db, "service", "worker", state="stopped", checked_at=NOW, basis="process")
    assert (
        InventoryView(db, "host").describe(object_detail(db, "host", "directory", "old"))[
            "technical_usage"
        ]
        == "configured"
    )


def test_observed_types_coverage_and_legacy_ambiguity_without_rewriting(
    db: sqlite3.Connection,
) -> None:
    put(db, "directory", "socket", path="/var/run/docker.sock")
    publish(
        db,
        "directory",
        "socket",
        {"entry_type": "socket", "consumer_checks": list(CONSUMER_SOURCES)},
        NOW,
    )
    put(
        db,
        "directory",
        "legacy",
        path="/srv/compose.yml",
        checked_at=NOW,
        basis="process",
        context="Файл или отсутствующий путь.",
    )
    assert read_map(db, "host", collection="directory", status="socket")["matched"] == 1
    socket = read_map(db, "host", collection="directory", object_id="socket")
    assert socket["understanding"]["entry_type_effective"] == "socket"
    assert socket["object"]["usage"] == "unknown"
    assert socket["understanding"]["gaps"] == ["Размер не измерен.", "Роль пути не установлена."]
    # A filename is never evidence of its filesystem type.
    assert (
        read_map(db, "host", collection="directory", object_id="legacy")["understanding"][
            "entry_type_effective"
        ]
        == "unknown"
    )
    with pytest.raises(ServerError):
        validate_object("directory", {"name": "bad", "path": "/bad", "consumer_checks": ["all"]})


@pytest.mark.parametrize(
    ("scope", "http", "expected"),
    [
        ("transport", 404, "unconfirmed"),
        ("transport", 403, "unconfirmed"),
        ("transport", 500, "error"),
        ("endpoint", 404, "error"),
        ("functional", 403, "error"),
    ],
)
def test_http_scope_preserves_errors_and_root_uncertainty(
    db: sqlite3.Connection,
    scope: str,
    http: int,
    expected: str,
) -> None:
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        probe_scope=scope,
        probe_url="https://api.example.test/health",
        expected_result="HTTP 200",
        public_result=f"HTTP {http}; TLS=valid",
        https="valid",
        availability="error",
        basis="probe",
        checked_at=NOW,
    )
    obj = object_detail(db, "host", "domain", "api")
    assert status_of(obj) == expected
    assert ("availability" in problems(obj)) == (expected == "error")
    row = read_map(db, "host", collection="domain", status=expected)["rows"][0]
    assert row["effective_availability"] == expected
    assert read_map(db, "host", collection="domain", status="error")["matched"] == (
        expected == "error"
    )
    if expected == "unconfirmed":
        assert "endpoint" in row["observed_reason"]
    assert obj["availability"] == "error"
    assert obj["public_result"] == f"HTTP {http}; TLS=valid"


def test_legacy_api_explicit_root_scope_and_new_expected_endpoint(db: sqlite3.Connection) -> None:
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        availability="error",
        https="valid",
        public_result="HTTP 404; curl=0; TLS=valid",
        checked_at=NOW,
        basis="probe",
        reason="Корень API не является полной проверкой приложения.",
    )
    assert read_map(db, "host", collection="domain")["rows"][0]["status"] == "unconfirmed"
    publish(
        db,
        "domain",
        "api",
        {
            "probe_scope": "functional",
            "probe_url": "https://api.example.test/health",
            "expected_result": "HTTP 200, status ok",
            "public_result": "HTTP 200, status ok",
            "availability": "available",
        },
        NOW,
    )
    assert read_map(db, "host", collection="domain")["rows"][0]["status"] == "available"
    with pytest.raises(ServerError):
        validate_object("route", {"name": "bad", "probe_scope": "functional"})
    with pytest.raises(ServerError):
        validate_object(
            "route",
            {
                "name": "bad",
                "probe_scope": "endpoint",
                "probe_url": "https://u:p@example.test/",
                "expected_result": "200",
            },
        )


def test_leftover_rejected_for_current_consumer_and_read_conflict_keeps_operator_data(
    db: sqlite3.Connection,
) -> None:
    put(db, "directory", "root", path="/root")
    put(db, "service", "worker", state="running", basis="process", checked_at=NOW)
    relate(db, "mount", ("service", "worker"), ("directory", "root"))
    before = state(db)
    with pytest.raises(ServerError):
        put(
            db,
            "directory",
            "root",
            path="/root",
            usage="leftover",
            basis="operator",
            reason="Закрыт",
            closed_application="Старый сайт",
            decision_owner="Оператор",
            decision_at=NOW,
            links_checked_at=NOW,
        )
    assert state(db) == before


def test_derived_pagination_sort_budget_and_no_other_server_exposure(
    db: sqlite3.Connection,
) -> None:
    save_server(db, "neighbor", 0, {"name": "UNRELATED_PRIVATE_NEIGHBOR"})
    for n in range(110):
        put(
            db,
            "service",
            f"svc-{n:03}",
            state="running" if n == 109 else "unknown",
            checked_at=NOW,
            basis="process",
        )
    page = read_map(db, "host", collection="service", status="running", limit=25, sort="status")
    assert page["matched"] == 1 and page["rows"][0]["object_id"] == "svc-109"
    all_rows, _ = collection_rows(db, "host", "service", sort="status", descending=True)
    assert all_rows[0]["object_id"] == "svc-109"
    offset = 0
    found: list[str] = []
    while True:
        page = read_map(db, "host", collection="service", offset=offset, limit=100)
        raw = json.dumps(page, ensure_ascii=True)
        assert len(raw.encode()) <= 48 * 1024 and "UNRELATED_PRIVATE_NEIGHBOR" not in raw
        found.extend(row["object_id"] for row in page["rows"])
        if page["complete"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(found) == len(set(found)) == 110


def test_v31_barrier_preserves_exact_rows_and_old_runtime_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v30.db"
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 30)
        storage.initialize_database(path)
        c = storage.connect_database(path)
        save_server(c, "host", 0, {"name": "Preserved"})
    before = (
        c.execute("SELECT * FROM servers").fetchall(),
        c.execute("SELECT * FROM server_events").fetchall(),
    )
    c.close()
    assert storage.initialize_database(path).schema_version == 31
    with storage.connect_database(path) as current:
        assert (
            current.execute("SELECT * FROM servers").fetchall(),
            current.execute("SELECT * FROM server_events").fetchall(),
        ) == before
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 30)
        with pytest.raises(storage.UnsupportedSchemaVersionError):
            storage.connect_database(path)


def test_screenshot_disk_format_has_columns_and_exact_source_title() -> None:
    entry = "/dev/vda: 64424509440 bytes (60.000 GiB); disk; тип ФС не задан; mount=нет"
    html = _render_storage([entry])
    assert 'colspan="3"' not in html
    assert ">/dev/vda</strong>" in html
    assert "64424509440 bytes (60.000 GiB)" in html
    assert "тип ФС не задан · mount=нет" in html


def test_http_understanding_fields_and_rejected_scope_keep_draft(db: sqlite3.Connection) -> None:
    put(db, "domain", "api", hostname="api.example.test")
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    manager = DashboardServerManager(path)
    try:
        base = manager.get_url()
        url = base + "servers/host/?view=domains&collection=domain&object_id=api"
        html = urlopen(url).read().decode()
        assert "Что известно сейчас" in html and 'name="map_probe_scope"' in html
        parsed = urlsplit(base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        fields: dict[str, str | int] = {
            "action": "server_map_save",
            "server_id": "host",
            "expected_revision": revision(db),
            "map_kind": "domain",
            "object_key": "api",
            "object_id": "api",
            "map_probe_scope": "functional",
            "map_probe_url": "https://api.example.test/health",
            "map_expected_result": "",
        }
        before = state(db)
        status, _, failed = _post(url, fields, origin=origin)
        assert status == 400 and b'data-recovered-draft="true"' in failed
        assert state(db) == before
        fields["map_expected_result"] = "HTTP 200"
        assert _post(url, fields, origin=origin)[0] == 200
        assert object_detail(db, "host", "domain", "api")["probe_scope"] == "functional"
        assert "https://api.example.test/health" in render_servers_content(
            path, "host", {"view": "domains", "object_id": "api"}
        )
    finally:
        manager.close()


@pytest.mark.parametrize("failure", ["tls", "dns", "curl", "mixed"])
def test_root_scope_never_suppresses_transport_or_node_failures(
    db: sqlite3.Connection, failure: str
) -> None:
    values: dict[str, object] = {
        "hostname": "api.example.test",
        "availability": "error",
        "probe_scope": "transport",
        "public_result": "HTTP 404; curl=0",
        "basis": "probe",
        "checked_at": NOW,
    }
    if failure == "tls":
        values["https"] = "error"
    elif failure == "dns":
        values["destination"] = "missing"
    elif failure == "curl":
        values["public_result"] = "HTTP 404; curl=7"
    else:
        values["public_result"] = "HTTP 404 on one node; HTTP 503 on another node"
    put(db, "domain", "api", **values)
    assert read_map(db, "host", collection="domain")["rows"][0]["status"] == "error"


def test_old_probe_scope_does_not_change_new_failure(db: sqlite3.Connection) -> None:
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        availability="error",
        probe_scope="transport",
        public_result="HTTP 404; curl=0",
        basis="probe",
        checked_at=old,
    )
    publish(
        db,
        "domain",
        "api",
        {"availability": "error", "public_result": "HTTP 404; endpoint changed"},
        NOW,
    )
    assert read_map(db, "host", collection="domain")["rows"][0]["status"] == "error"


def test_v31_failed_migration_rolls_back_exact_map_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 30)
        storage.initialize_database(path)
        c = storage.connect_database(path)
        save_server(c, "host", 0, {"name": "Preserved", "passport": {"storage": ["unchanged"]}})
        put(c, "directory", "old", path="/home/old", role="code")
        before = state(c)
        c.close()
    apply = storage._apply_migration

    def fail(c: sqlite3.Connection, target: int) -> None:
        apply(c, target)
        if target == 31:
            c.execute("UPDATE server_map_objects SET data='{}'")
            raise RuntimeError("fixture migration failure")

    with monkeypatch.context() as broken:
        broken.setattr(storage, "_apply_migration", fail)
        with pytest.raises(RuntimeError, match="fixture migration failure"):
            storage.initialize_database(path)
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 30)
        with storage.connect_database(path) as c:
            assert state(c) == before
    storage.initialize_database(path)
    with storage.connect_database(path) as c:
        assert state(c) == before


def test_legal_unicode_detail_summary_keeps_totals_and_progressing_all_links(
    db: sqlite3.Connection,
) -> None:
    put(
        db,
        "route",
        "selected",
        public_result="Ж" * 700,
        origin_result="Ж" * 300,
        target="X" * 256,
        config_reference="X" * 256,
    )
    for kind in ("application", "domain", "route", "service", "directory"):
        for n in range(6):
            key = f"{kind}-{n}"
            fields: dict[str, object] = {"name": "Ж" * (80 if kind == "application" else 128)}
            if kind == "domain":
                fields["hostname"] = f"r{n}.example.test"
            elif kind == "directory":
                fields["path"] = "/srv/" + key
            put(db, kind, key, **fields)
            relate(db, key, ("route", "selected"), (kind, key), role="Ж" * 128)
    exact = object_detail(db, "host", "route", "selected")
    before = state(db)
    offset = 0
    seen: list[str] = []
    while True:
        page = read_map(db, "host", collection="route", object_id="selected", links_offset=offset)
        assert page["object"] == exact
        assert len(json.dumps(page, ensure_ascii=True).encode()) <= 48 * 1024
        assert len(json.dumps(page["understanding"]["related"], ensure_ascii=True).encode()) <= 8192
        for group in page["understanding"]["related"].values():
            assert group["total"] == 6
            assert group["omitted"] == 6 - len(group["items"])
        assert page["links_total"] == 30 and page["links"]
        seen.extend(link["link_id"] for link in page["links"])
        if page["links_next_offset"] is None:
            break
        assert page["links_next_offset"] > offset
        offset = page["links_next_offset"]
    assert len(seen) == len(set(seen)) == 30
    assert state(db) == before


def test_new_consumer_and_scan_refresh_preserve_old_decision_with_cas_and_history(
    db: sqlite3.Connection,
) -> None:
    decision = dict(
        path="/srv/old",
        usage="leftover",
        basis="operator",
        reason="Closed site",
        closed_application="Former site",
        decision_owner="Operator",
        decision_at=NOW,
        links_checked_at=NOW,
    )
    put(db, "directory", "old", **decision)
    put(db, "service", "worker", state="running", basis="process", checked_at=NOW)
    before_revision = revision(db)
    before_events = len(state(db)[-1])
    relate(db, "current-consumer", ("service", "worker"), ("directory", "old"))
    assert revision(db) == before_revision + 1
    assert len(state(db)[-1]) == before_events + 1
    coverage = [{"area": "directory", "state": "partial", "source": "fixture", "reason": "partial"}]
    mutate_map(
        db,
        "host",
        revision(db),
        {
            "action": "check_start",
            "check_id": "refresh",
            "coverage": coverage,
            "source": "fixture",
            "started_at": NOW,
        },
    )
    mutate_map(
        db,
        "host",
        revision(db),
        {
            "action": "check_append",
            "check_id": "refresh",
            "batch": 0,
            "observations": [
                {
                    "kind": "directory",
                    "object_id": "old",
                    "source": "fixture",
                    "checked_at": NOW,
                    "basis": "process",
                    "reason": "Measured path",
                    "values": {"size_bytes": 4242, "entry_type": "directory"},
                }
            ],
        },
    )
    publish_op = {
        "action": "check_publish",
        "check_id": "refresh",
        "coverage": coverage,
        "finished_at": NOW,
    }
    before = state(db)
    with pytest.raises(ServerError, match="revision"):
        mutate_map(db, "host", revision(db) - 1, publish_op)
    assert state(db) == before
    before_revision, before_events = revision(db), len(state(db)[-1])
    mutate_map(db, "host", revision(db), publish_op)
    assert revision(db) == before_revision + 1 and len(state(db)[-1]) == before_events + 1
    obj = object_detail(db, "host", "directory", "old")
    assert obj["size_bytes"] == 4242 and obj["entry_type"] == "directory"
    assert all(
        obj[field] == decision[field]
        for field in (
            "usage",
            "closed_application",
            "decision_owner",
            "decision_at",
            "links_checked_at",
        )
    )
    assert (
        db.execute("SELECT state FROM server_map_checks WHERE check_id='refresh'").fetchone()[0]
        == "published"
    )
    understanding = read_map(db, "host", collection="directory", object_id="old")["understanding"]
    assert understanding["technical_usage"] == "current"
    assert any("противоречит" in gap for gap in understanding["gaps"])
    before = state(db)
    with pytest.raises(ServerError, match="active application"):
        put(db, "directory", "old", **decision)
    assert state(db) == before


def test_runtime_application_filters_before_paging_and_relevant_choices(
    db: sqlite3.Connection,
) -> None:
    for n in range(30):
        put(db, "application", f"app-{n:02}", usage="unknown")
    put(db, "service", "worker", state="running", basis="process", checked_at=NOW)
    relate(db, "runtime", ("application", "app-29"), ("service", "worker"), role="runtime")
    for status in ("current", "running"):
        page = read_map(db, "host", collection="application", status=status, limit=25)
        assert page["matched"] == 1 and page["total"] == 30
        assert page["rows"][0]["object_id"] == "app-29"
    assert read_map(db, "host", collection="application", status="active")["matched"] == 0
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    html = render_servers_content(path, "host", {"view": "sites"})
    assert '<option value="current"' in html and '<option value="running"' in html
    html = render_servers_content(path, "host", {"view": "services"})
    assert '<option value="current"' not in html and '<option value="running"' in html
    put(db, "service", "worker", state="stopped", basis="process", checked_at=NOW)
    assert read_map(db, "host", collection="application", status="configured")["matched"] == 1
    assert read_map(db, "host", collection="application", status="current")["matched"] == 0


def _capital_root_fixture(db: sqlite3.Connection) -> None:
    # Same field provenance shape as the deployed Capital backend inventory:
    # route/domain probes one second apart, inherited application verdict22s later,
    # then an unrelated package observation updates the application's common date.
    start = datetime.now(UTC) - timedelta(minutes=30)
    source = "SSH fixture; Docker/systemctl; DNS/HTTPS public+origin"
    domain_reason = "Проверены DNS, публичный GET / и локальный TLS. Корень API не является полной проверкой приложения."
    route_reason = "Проверен GET /; результаты разных IP и точек доступа сохранены отдельно."
    app_reason = "Маршрут и runtime проверены. Назначение, usage и политика доступа сохранены. Проверен только GET /."
    for kind, key, seconds, basis, reason, values in (
        (
            "domain",
            "api",
            1,
            "probe",
            domain_reason,
            {
                "hostname": "api.example.test",
                "https": "valid",
                "destination": "this_server",
                "public_result": "HTTP 404; curl=0; TLS=valid",
            },
        ),
        (
            "route",
            "public-root",
            0,
            "probe",
            route_reason,
            {
                "hostname": "api.example.test",
                "path": "/",
                "https": "valid",
                "public_result": "HTTP 404; curl=0; TLS=valid",
            },
        ),
        (
            "application",
            "backend",
            22,
            "process",
            app_reason,
            {"usage": "unknown", "domains": ["api.example.test"]},
        ),
    ):
        checked = (start + timedelta(seconds=seconds)).isoformat()
        evidence = [
            {
                "field": field,
                "source": source,
                "checked_at": checked,
                "basis": basis,
                "reason": reason,
            }
            for field in ("availability", *values)
        ]
        put(
            db,
            kind,
            key,
            availability="error",
            source="legacy import",
            basis="import",
            checked_at=(start + timedelta(minutes=20)).isoformat(),
            evidence=evidence,
            **values,
        )
    put(db, "service", "worker", state="running", basis="process", checked_at=NOW)
    relate(db, "domain-route", ("domain", "api"), ("route", "public-root"), role="endpoint")
    relate(db, "app-domain", ("application", "backend"), ("domain", "api"), role="route")
    relate(db, "app-route", ("application", "backend"), ("route", "public-root"), role="endpoint")
    relate(db, "runtime", ("application", "backend"), ("service", "worker"), role="runtime")


def test_capital_shape_root_probe_agrees_across_domain_route_app_and_overview(
    db: sqlite3.Connection,
) -> None:
    from harness.dashboard_server_map import render_summary
    from harness.servers import get_server

    _capital_root_fixture(db)
    before = state(db)
    inventory = InventoryView(db, "host")
    for kind, key in (("domain", "api"), ("route", "public-root"), ("application", "backend")):
        detail = read_map(db, "host", collection=kind, object_id=key)
        assert detail["object"]["availability"] == "error"
        assert detail["understanding"]["effective_availability"] == "unconfirmed"
        assert detail["understanding"]["observed_status"] == "unconfirmed"
        assert read_map(db, "host", collection=kind, status="error")["matched"] == 0
        enriched = inventory.enrich(detail["object"])
        assert "availability" not in problems(enriched)
        assert "endpoint" in enriched["observed_reason"]
    assert read_map(db, "host", collection="application", status="current")["matched"] == 1
    html = render_summary(db, get_server(db, "host"))
    assert "Публичная проверка обнаружила ошибку" not in html
    assert "Работа приложения не проверена" in html
    assert state(db) == before


@pytest.mark.parametrize(
    "failure",
    [
        "endpoint",
        "tls",
        "dns",
        "curl",
        "5xx",
        "source",
        "basis",
        "newer",
        "old-probe",
        "unverified",
        "stale-link",
    ],
)
def test_linked_root_scope_never_erases_genuine_or_unrelated_failure(
    db: sqlite3.Connection, failure: str
) -> None:
    from harness.server_map import links_for

    _capital_root_fixture(db)
    if failure in {"unverified", "stale-link"}:
        for link in links_for(db, "host", "application", "backend"):
            if link["to_kind"] not in {"domain", "route"}:
                continue
            if failure == "unverified":
                link["basis"] = "reference"
            else:
                link["checked_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
            mutate_map(db, "host", revision(db), {"action": "link", "link": link})
    else:
        obj = object_detail(db, "host", "route", "public-root")
        values = {k: v for k, v in obj.items() if k not in {"kind", "object_id"}}
        if failure == "endpoint":
            values.update(
                probe_scope="functional",
                probe_url="https://api.example.test/health",
                expected_result="HTTP 200",
            )
        elif failure == "tls":
            values["https"] = "error"
        elif failure == "dns":
            domain = object_detail(db, "host", "domain", "api")
            put(
                db,
                "domain",
                "api",
                **{
                    k: v for k, v in domain.items() if k not in {"kind", "object_id", "destination"}
                },
                destination="missing",
            )
        elif failure == "old-probe":
            domain = object_detail(db, "host", "domain", "api")
            for item in domain["evidence"]:
                item["checked_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
            put(
                db,
                "domain",
                "api",
                **{k: v for k, v in domain.items() if k not in {"kind", "object_id"}},
            )
        elif failure == "curl":
            values["public_result"] = "HTTP 404; curl=7"
        elif failure == "5xx":
            values["public_result"] = "HTTP 503; curl=0"
        for evidence in values["evidence"]:
            if evidence["field"] not in {"availability", "public_result"}:
                continue
            if failure == "source":
                evidence["source"] = "Unrelated probe"
            elif failure == "basis" and evidence["field"] == "public_result":
                evidence["basis"] = "process"
            elif failure == "newer":
                evidence["checked_at"] = NOW
                evidence["reason"] = "Expected endpoint failed"
            elif failure == "old-probe":
                evidence["checked_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        put(db, "route", "public-root", **values)
    # Every inherited application error remains explicit until its cause is supported.
    assert read_map(db, "host", collection="application")["rows"][0]["status"] == "error"
    if failure not in {"unverified", "stale-link"}:
        assert read_map(db, "host", collection="route")["rows"][0]["status"] == "error"


def test_new_scope_alone_and_nonmatching_field_date_cannot_reinterpret_error(
    db: sqlite3.Connection,
) -> None:
    _capital_root_fixture(db)
    publish(
        db,
        "route",
        "public-root",
        {
            "probe_scope": "transport",
            "probe_url": "https://api.example.test/",
            "expected_result": "Transport response",
        },
        NOW,
    )
    assert read_map(db, "host", collection="route")["rows"][0]["status"] == "error"
    assert read_map(db, "host", collection="application")["rows"][0]["status"] == "error"


@pytest.mark.parametrize(
    "failure",
    [
        "HTTP503; curl=0",
        "HTTP 500; curl=0",
        "HTTP 401; curl=0",
        "HTTP 404; curl=7",
        "HTTP 404; curl = 28",
        "HTTP 404; TLS=error",
        "HTTP 404; TLS=expired",
        "HTTP 404; TLS=untrusted",
        "HTTP 404; TLS=mismatch",
        "HTTP 404; DNS=NXDOMAIN",
    ],
)
def test_own_application_failure_precedes_neighbor_root_uncertainty(
    db: sqlite3.Connection, failure: str
) -> None:
    _capital_root_fixture(db)
    obj = object_detail(db, "host", "application", "backend")
    for evidence in obj["evidence"]:
        if evidence["field"] == "availability":
            evidence["reason"] = "Проверен GET /; " + failure + "."
    put(
        db,
        "application",
        "backend",
        **{k: v for k, v in obj.items() if k not in {"kind", "object_id"}},
    )
    before = state(db)
    detail = read_map(db, "host", collection="application", object_id="backend")
    assert detail["object"] == obj
    assert detail["understanding"]["technical_usage"] == "current"
    assert detail["understanding"]["effective_availability"] == "error"
    assert detail["understanding"]["observed_status"] == "error"
    assert failure in detail["understanding"]["observed_reason"]
    for status in ("error", "attention"):
        assert read_map(db, "host", collection="application", status=status)["matched"] == 1
    assert state(db) == before


@pytest.mark.parametrize(
    ("scope", "mismatch"),
    [
        (scope, mismatch)
        for scope in ("transport", "unknown")
        for mismatch in ("public-source", "public-date", "public-basis", "availability-basis")
    ]
    + [("transport", "scope-source"), ("transport", "scope-date"), ("transport", "scope-basis")],
)
def test_direct_root_uncertainty_requires_compatible_field_provenance(
    db: sqlite3.Connection, scope: str, mismatch: str
) -> None:
    reason = "Корень API не является полной проверкой приложения."
    evidence = [
        {
            "field": field,
            "source": "Root probe",
            "checked_at": NOW,
            "basis": "probe",
            "reason": reason,
        }
        for field in ("availability", "public_result", "probe_scope")
    ]
    target, change = mismatch.split("-")
    field = {"public": "public_result", "scope": "probe_scope", "availability": "availability"}[
        target
    ]
    for item in evidence:
        if item["field"] == field:
            if change == "source":
                item["source"] = "Unrelated endpoint probe"
            elif change == "date":
                item["checked_at"] = (
                    datetime.fromisoformat(NOW) - timedelta(minutes=2)
                ).isoformat()
            else:
                item["basis"] = "process"
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        availability="error",
        probe_scope=scope,
        public_result="HTTP 404; curl=0; TLS=valid",
        evidence=evidence,
    )
    obj = object_detail(db, "host", "domain", "api")
    before = state(db)
    detail = read_map(db, "host", collection="domain", object_id="api")
    assert detail["object"] == obj
    assert detail["understanding"]["effective_availability"] == "error"
    for status in ("error", "attention"):
        assert read_map(db, "host", collection="domain", status=status)["matched"] == 1
    assert state(db) == before


@pytest.mark.parametrize("scope", ["transport", "unknown"])
@pytest.mark.parametrize("http", [403, 404])
def test_matching_root_provenance_remains_unconfirmed(
    db: sqlite3.Connection, scope: str, http: int
) -> None:
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        availability="error",
        probe_scope=scope,
        public_result=f"HTTP {http}; curl=0; TLS=valid",
        basis="probe",
        reason="Корень API не является полной проверкой приложения.",
        checked_at=NOW,
    )
    before = state(db)
    assert read_map(db, "host", collection="domain")["rows"][0]["status"] == "unconfirmed"
    for status in ("error", "attention"):
        assert read_map(db, "host", collection="domain", status=status)["matched"] == 0
    assert state(db) == before


@pytest.mark.parametrize(
    ("kind", "key", "view"),
    [
        ("domain", "api", "domains"),
        ("route", "public-root", "domains"),
        ("application", "backend", "sites"),
    ],
)
def test_detail_distinguishes_saved_assessment_from_current_conclusion(
    db: sqlite3.Connection, kind: str, key: str, view: str
) -> None:
    from harness.dashboard_server_map import display_date

    _capital_root_fixture(db)
    obj = object_detail(db, "host", kind, key)
    assessment = next(item for item in obj["evidence"] if item["field"] == "availability")
    before = state(db)
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    html = render_servers_content(
        path, "host", {"view": view, "collection": kind, "object_id": key}
    )
    assert "<dt>Текущий вывод о доступности</dt><dd>Работа приложения не проверена</dd>" in html
    assert "<dt>Сохранённая оценка доступности</dt><dd>Ошибка</dd>" in html
    assert "<dt>Источник сохранённой оценки</dt><dd>" + assessment["source"] + "</dd>" in html
    assert (
        "<dt>Дата сохранённой оценки</dt><dd>" + display_date(assessment["checked_at"]) + "</dd>"
        in html
    )
    assert "<dt>Доступность</dt><dd>Ошибка</dd>" not in html
    assert state(db) == before


def test_detail_keeps_genuine_error_as_current_availability(db: sqlite3.Connection) -> None:
    _capital_root_fixture(db)
    obj = object_detail(db, "host", "application", "backend")
    for evidence in obj["evidence"]:
        if evidence["field"] == "availability":
            evidence["reason"] = "Проверен GET /; HTTP503; curl=0."
    put(
        db,
        "application",
        "backend",
        **{k: v for k, v in obj.items() if k not in {"kind", "object_id"}},
    )
    before = state(db)
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    html = render_servers_content(path, "host", {"view": "sites", "object_id": "backend"})
    assert "<dt>Доступность</dt><dd>Ошибка</dd>" in html
    assert "Сохранённая оценка доступности" not in html
    assert "Ошибка проверки доступности; точка и причина указаны в основании" in html
    assert state(db) == before


@pytest.mark.parametrize("kind", ["domain", "route"])
@pytest.mark.parametrize(
    "origin",
    [
        "HTTP 503; curl=0; TLS=valid",
        "HTTP 500; curl=0; TLS=valid",
        "HTTP 404; curl=7",
        "HTTP 404; curl=28",
        "HTTP 404; TLS=untrusted",
        "HTTP 404; TLS=mismatch",
        "HTTP 404; TLS=expired",
        "HTTP 404; TLS=error",
        "HTTP 404; DNS=NXDOMAIN",
    ],
)
def test_own_origin_failure_precedes_root_uncertainty(
    db: sqlite3.Connection, kind: str, origin: str
) -> None:
    values = {"path": "/"} if kind == "route" else {}
    put(
        db,
        kind,
        "api",
        **values,
        hostname="api.example.test",
        availability="error",
        probe_scope="transport",
        public_result="HTTP 404; curl=0; TLS=valid",
        origin_result=origin,
        basis="probe",
        checked_at=NOW,
        reason="Корень API не является полной проверкой приложения.",
    )
    exact = object_detail(db, "host", kind, "api")
    before = state(db)
    detail = read_map(db, "host", collection=kind, object_id="api")
    assert detail["object"] == exact
    observed = detail["understanding"]
    assert observed["effective_availability"] == observed["observed_status"] == "error"
    assert "Внутренняя проверка origin: " + origin in observed["observed_reason"]
    assert (
        "Собственная публичная проверка: HTTP 404; curl=0; TLS=valid" in observed["observed_reason"]
    )
    assert "дата: " + NOW in observed["observed_reason"]
    assert "источник: fixture" in observed["observed_reason"]
    assert "endpoint не проверена" in observed["observed_reason"]
    for status in ("error", "attention"):
        # Filtering must establish the complete matched set before slicing.
        page = read_map(db, "host", collection=kind, status=status, offset=1, limit=1)
        assert page["matched"] == 1 and page["rows"] == []
    assert state(db) == before


@pytest.mark.parametrize(
    "guard",
    [
        "independent",
        "stale",
        "future",
        "reference",
        "import",
        "unknown",
        "process",
        "missing-date",
        "common-only",
    ],
)
def test_origin_failure_requires_its_own_current_probe_provenance(
    db: sqlite3.Connection, guard: str
) -> None:
    evidence = [
        {
            "field": field,
            "source": "Public root probe",
            "checked_at": NOW,
            "basis": "probe",
            "reason": "Корень API не является полной проверкой приложения.",
        }
        for field in ("availability", "probe_scope", "public_result")
    ]
    origin_date = (datetime.fromisoformat(NOW) - timedelta(minutes=5)).isoformat()
    origin = {
        "field": "origin_result",
        "source": "Independent origin TLS probe",
        "checked_at": origin_date,
        "basis": "probe",
        "reason": "Origin result recorded separately.",
    }
    if guard == "stale":
        origin["checked_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    elif guard == "future":
        origin["checked_at"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    elif guard in {"reference", "import", "unknown", "process"}:
        origin["basis"] = guard
    elif guard == "missing-date":
        origin["checked_at"] = None  # type: ignore[assignment]
    if guard != "common-only":
        evidence.append(origin)
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        availability="error",
        probe_scope="transport",
        public_result="HTTP 404; curl=0; TLS=valid",
        origin_result="HTTP 503; curl=0; TLS=valid",
        # A fresh generic timestamp cannot verify an origin field when other
        # fields already have independent provenance.
        source="Generic later metadata",
        basis="probe",
        checked_at=NOW,
        evidence=evidence,
    )
    exact = object_detail(db, "host", "domain", "api")
    before = state(db)
    detail = read_map(db, "host", collection="domain", object_id="api")
    expected = "error" if guard == "independent" else "unconfirmed"
    assert detail["understanding"]["effective_availability"] == expected
    reason = detail["understanding"]["observed_reason"]
    if guard == "independent":
        assert "Independent origin TLS probe" in reason and origin_date in reason
        assert "Public root probe" in reason and NOW in reason
    else:
        assert "Внутренняя проверка origin" not in reason
    assert detail["object"] == exact
    for status in ("error", "attention"):
        assert read_map(db, "host", collection="domain", status=status)["matched"] == (
            expected == "error"
        )
    assert state(db) == before


@pytest.mark.parametrize("point", ["domain", "route"])
@pytest.mark.parametrize(
    "origin_result",
    ["HTTP 503; curl=0; TLS=valid", "HTTP 404; curl=0; TLS=untrusted; чтение HTTP с --insecure"],
)
def test_legacy_linked_origin_failure_keeps_point_and_functional_gap(
    db: sqlite3.Connection, point: str, origin_result: str
) -> None:
    _capital_root_fixture(db)
    key = "api" if point == "domain" else "public-root"
    obj = object_detail(db, "host", point, key)
    proof = next(e for e in obj["evidence"] if e["field"] == "availability")
    obj["origin_result"] = origin_result
    obj["evidence"].append({**proof, "field": "origin_result"})
    put(db, point, key, **{k: v for k, v in obj.items() if k not in {"kind", "object_id"}})
    before = state(db)
    for kind, target in ((point, key), ("application", "backend"), ("route", "public-root")):
        detail = read_map(db, "host", collection=kind, object_id=target)
        observed = detail["understanding"]
        assert observed["effective_availability"] == "error"
        assert origin_result in observed["observed_reason"]
        assert proof["source"] in observed["observed_reason"]
        assert proof["checked_at"] in observed["observed_reason"]
        assert "endpoint не проверена" in observed["observed_reason"]
        if kind == "application" or (kind == "route" and point == "domain"):
            assert "Сбой связанной внутренней проверки origin" in observed["observed_reason"]
        if kind == "route" and point == "domain":
            assert detail["object"]["origin_result"] == ""
            assert (
                "Собственная публичная проверка: HTTP 404; curl=0; TLS=valid"
                in observed["observed_reason"]
            )
        for status in ("error", "attention"):
            assert read_map(db, "host", collection=kind, status=status)["matched"] == 1
    assert read_map(db, "host", collection="application", status="current")["matched"] == 1
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    html = render_servers_content(
        path, "host", {"view": "domains", "collection": "route", "object_id": "public-root"}
    )
    assert "Публичная проверка обнаружила ошибку" not in html
    assert origin_result in html
    assert "HTTP 404; curl=0; TLS=valid" in html
    assert state(db) == before


@pytest.mark.parametrize("public_http", [403, 404])
@pytest.mark.parametrize("origin_http", [200, 403, 404])
def test_successful_origin_does_not_certify_api_root_function(
    db: sqlite3.Connection, public_http: int, origin_http: int
) -> None:
    put(
        db,
        "domain",
        "api",
        hostname="api.example.test",
        availability="error",
        probe_scope="transport",
        public_result=f"HTTP {public_http}; curl=0; TLS=valid",
        origin_result=f"HTTP {origin_http}; curl=0; TLS=valid",
        basis="probe",
        checked_at=NOW,
    )
    before = state(db)
    detail = read_map(db, "host", collection="domain", object_id="api")
    assert detail["understanding"]["effective_availability"] == "unconfirmed"
    assert read_map(db, "host", collection="domain", status="attention")["matched"] == 0
    assert state(db) == before


def test_origin_explanation_stays_bounded_with_unicode_and_full_link_cursor(
    db: sqlite3.Connection,
) -> None:
    put(
        db,
        "route",
        "selected",
        availability="error",
        probe_scope="transport",
        public_result="HTTP 404; curl=0; TLS=valid; " + "Ж" * 600,
        origin_result="HTTP 404; " + "Ж" * 600 + "; TLS=untrusted",
        source="Ж" * 128,
        basis="probe",
        checked_at=NOW,
        target="X" * 256,
        config_reference="X" * 256,
    )
    for kind in ("application", "domain", "route", "service", "directory"):
        for n in range(6):
            key = f"{kind}-{n}"
            values: dict[str, object] = {"name": "Ж" * (80 if kind == "application" else 128)}
            if kind == "domain":
                values["hostname"] = f"r{n}.example.test"
            elif kind == "directory":
                values["path"] = "/srv/" + key
            put(db, kind, key, **values)
            relate(db, key, ("route", "selected"), (kind, key), role="Ж" * 128)
    exact = object_detail(db, "host", "route", "selected")
    before = state(db)
    offset = 0
    seen: list[str] = []
    while True:
        detail = read_map(db, "host", collection="route", object_id="selected", links_offset=offset)
        assert detail["object"] == exact
        assert len(json.dumps(detail, ensure_ascii=True).encode()) <= 48 * 1024
        assert (
            len(json.dumps(detail["understanding"]["related"], ensure_ascii=True).encode()) <= 8192
        )
        reason = detail["understanding"]["observed_reason"]
        assert "TLS=untrusted" in reason and NOW in reason
        assert "Собственная публичная проверка: HTTP 404; curl=0; TLS=valid" in reason
        assert "Ж" * 600 not in reason
        assert detail["links_total"] == 30 and detail["links"]
        seen.extend(link["link_id"] for link in detail["links"])
        if detail["links_next_offset"] is None:
            break
        assert detail["links_next_offset"] > offset
        offset = detail["links_next_offset"]
    assert len(seen) == len(set(seen)) == 30
    assert state(db) == before
