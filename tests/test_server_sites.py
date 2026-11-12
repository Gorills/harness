from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harness.server_import import enrich_selected_card, preview_import
from harness.servers import (
    MAX_RESPONSE_BYTES,
    MAX_SERVER_BYTES,
    ServerConflictError,
    ServerError,
    edit_server,
    enrich_server,
    get_server,
    list_servers,
    load_server_metadata,
    record_event,
    save_server,
    validate_server,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database


def site(identity: str = "front") -> dict[str, object]:
    return {
        "site_id": identity,
        "name": "Frontend",
        "domains": ["site.example.invalid"],
        "stack": [{"name": "Nuxt", "version": ""}],
        "paths": ["/srv/site"],
        "runtime": ["service frontend"],
        "ports": [3000],
        "dependencies": ["PostgreSQL: site_db (reference only)"],
        "source": "operator",
        "checked_at": None,
    }


def profile() -> dict[str, object]:
    return {
        "profile_id": "nuxt-test",
        "name": "Nuxt test",
        "environment": "test",
        "stack": [{"name": "Nuxt", "version": ""}],
        "domain_pattern": "{site}.example.invalid",
        "path_pattern": "/srv/{site}",
        "runtime": ["systemd"],
        "access_policy": "BasicAuth before publication; credential reference only",
        "steps": ["Prepare chosen path", "Verify authenticated access"],
        "source": "operator",
        "checked_at": None,
    }


def test_sites_profiles_cas_search_and_additive_import_preserve_edits_and_deletions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        imported = validate_server(
            {
                "name": "Shared",
                "sites": [site()],
                "platform_profiles": [profile()],
                "facts": [
                    {
                        "text": "Other domains are undocumented",
                        "source": "selected card",
                        "checked_at": None,
                    }
                ],
            }
        )
        save_server(connection, "shared", 0, {"name": "Shared"})
        card = enrich_server(connection, "shared", 1, imported)
        assert card["revision"] == 2
        assert list_servers(connection, "site.example.invalid")["total"] == 1
        assert list_servers(connection, "Frontend")["total"] == 1
        card["metadata"]["name"] = "Operator title"
        card["metadata"]["sites"][0]["stack"][0]["version"] = "4.5.2"
        card["metadata"]["sites"][0]["profile_id"] = "nuxt-test"
        card["metadata"]["platform_profiles"][0]["path_pattern"] = "/operator/{site}"
        save_server(connection, "shared", 2, card["metadata"])
        with pytest.raises(ServerConflictError):
            enrich_server(connection, "shared", 2, imported)
        current = enrich_server(connection, "shared", 3, imported)
        assert current["metadata"]["name"] == "Operator title"
        assert current["metadata"]["sites"][0]["stack"][0]["version"] == "4.5.2"
        assert current["metadata"]["platform_profiles"][0]["path_pattern"] == "/operator/{site}"
        assert any(
            f["text"] == "Other domains are undocumented" for f in current["metadata"]["facts"]
        )
        current["metadata"]["sites"] = []
        current["metadata"]["platform_profiles"] = []
        save_server(connection, "shared", 4, current["metadata"])
        deleted = enrich_server(connection, "shared", 5, imported)
        assert deleted["metadata"]["sites"] == [] and deleted["metadata"]["platform_profiles"] == []
        assert connection.execute("SELECT count(*) FROM server_imported_sites").fetchone()[0] == 1


@pytest.mark.parametrize(
    "bad",
    [
        {
            "sites": [
                {"site_id": "front", "name": "Safe", "source": "operator", "password": "CANARY"}
            ]
        },
        {"sites": [{**site(), "dependencies": ["postgres://user:CANARY@host/db"]}]},
        {"sites": [{**site(), "stack": [{"name": "Nuxt", "version": "token=CANARY"}]}]},
        {"sites": [{**site(), "ports": [True]}]},
        {"sites": [{**site(), "ports": [65536]}]},
        {"sites": [{**site(), "stack": [{"name": "Nuxt", "version": "", "env": "CANARY"}]}]},
        {"sites": [{**site(), "checked_at": "2026-10-05"}]},
        {"sites": [{**site(), "name": "\ud800"}]},
        {"sites": [site(), site()]},
        {"sites": [{**site(), "profile_id": "missing"}]},
        {"platform_profiles": [{**profile(), "steps": ["password=CANARY"]}]},
        {"platform_profiles": [{**profile(), "steps": ["x"] * 13}]},
    ],
)
def test_invalid_nested_context_is_rejected_before_any_durable_write(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        with pytest.raises(ServerError) as error:
            save_server(connection, "host", 0, {"name": "Host", **bad})
        assert "CANARY" not in str(error.value)
        assert connection.execute("SELECT count(*) FROM servers").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM server_events").fetchone()[0] == 0


def large_metadata() -> dict[str, object]:
    return {
        "name": "Многосайтовый сервер",
        "sites": [
            {
                **site(f"app-{n}"),
                "name": "Площадка " + str(n),
                "purpose": "я" * 120,
                "runtime": ["служба " * 16],
            }
            for n in range(16)
        ],
        "platform_profiles": [profile()],
    }


def test_large_storage_has_bounded_progressing_pages_and_rejects_unreadable_child(
    tmp_path: Path,
) -> None:
    metadata = large_metadata()
    metadata["sites"] = [{**site(f"app-{n}"), "name": "Площадка " + str(n)} for n in range(95)]
    size = len(json.dumps(validate_server(metadata), ensure_ascii=True).encode())
    assert 32 * 1024 < size <= MAX_SERVER_BYTES
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        card = save_server(connection, "large", 0, metadata)
        assert len(card["metadata"]["sites"]) == 5
        assert card["sites_page"]["full_total"] == 95
        ids: list[str] = []
        offset = 0
        while True:
            card = get_server(connection, "large", sites_offset=offset, expected_revision=1)
            assert len(json.dumps(card, ensure_ascii=True).encode()) <= MAX_RESPONSE_BYTES
            ids.extend(s["site_id"] for s in card["metadata"]["sites"])
            if card["sites_page"]["next_offset"] is None:
                break
            assert card["sites_page"]["next_offset"] > offset
            offset = card["sites_page"]["next_offset"]
        assert ids == [f"app-{n}" for n in range(95)]
        assert list_servers(connection, "Площадка 94")["total"] == 1
        selected = get_server(connection, "large", site_id="app-94", profiles_limit=0)
        assert selected["metadata"]["sites"][0]["site_id"] == "app-94"
        with pytest.raises(ServerError, match="byte limit"):
            edit_server(
                connection,
                "large",
                1,
                site={**site("app-94"), "dependencies": [str(n) + "я" * 120 for n in range(16)]},
            )
        assert get_server(connection, "large")["revision"] == 1
        assert len(load_server_metadata(connection, "large")["sites"]) == 95


def test_atomic_child_patch_cas_and_read_pin_preserve_neighbors_and_ledgers(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    with connect_database(database) as connection:
        imported = validate_server(
            {
                "name": "Shared",
                "sites": [site(f"app-{n}") for n in range(95)],
                "platform_profiles": [profile()],
            }
        )
        save_server(connection, "shared", 0, {"name": "Shared", "role": "Operator"})
        enrich_server(connection, "shared", 1, imported)
        before = load_server_metadata(connection, "shared")
        changed = {
            **before["sites"][-1],
            "purpose": "Verified late-page edit",
            "profile_id": "nuxt-test",
        }
        edit_server(connection, "shared", 2, site=changed)
        current = load_server_metadata(connection, "shared")
        assert current["sites"][:-1] == before["sites"][:-1]
        assert current["sites"][-1] == changed
        assert current["role"] == "Operator"
        with pytest.raises(ServerConflictError):
            get_server(connection, "shared", sites_offset=5, expected_revision=2)
        event_count = connection.execute("SELECT count(*) FROM server_events").fetchone()[0]
        ledger_count = connection.execute("SELECT count(*) FROM server_imported_sites").fetchone()[
            0
        ]
        for operation in (
            {"site": site("app-0")},
            {"delete_site_id": "app-94"},
            {"metadata": {"role": "stale"}},
        ):
            with pytest.raises(ServerConflictError):
                edit_server(connection, "shared", 2, **operation)
        with pytest.raises(ServerError):
            edit_server(connection, "shared", 3, metadata={"sites": []})
        with pytest.raises(ServerError):
            edit_server(connection, "shared", 3, site=changed, metadata={"name": "mixed"})
        with pytest.raises(ServerError, match="profile must exist"):
            edit_server(connection, "shared", 3, delete_profile_id="nuxt-test")
        assert load_server_metadata(connection, "shared") == current
        assert connection.execute("SELECT count(*) FROM server_events").fetchone()[0] == event_count
        assert (
            connection.execute("SELECT count(*) FROM server_imported_sites").fetchone()[0]
            == ledger_count
        )
        edit_server(connection, "shared", 3, metadata={"role": "New role"})
        after = load_server_metadata(connection, "shared")
        assert (
            after["sites"] == current["sites"]
            and after["platform_profiles"] == current["platform_profiles"]
        )
        assert after["role"] == "New role"
        filtered = get_server(connection, "shared", sites_query="app-94", expected_revision=4)
        assert filtered["sites_page"]["total"] == 1 and filtered["sites_page"]["full_total"] == 95


def test_worst_case_ascii_base_selected_children_and_history_remain_readable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    initialize_database(database)
    metadata = validate_server(
        {
            "name": "Кириллица",
            "facts": [
                {"text": "я" * 190, "source": "я" * 100, "checked_at": None} for _ in range(16)
            ],
        }
    )
    large_site = validate_server(
        {"name": "Host", "sites": [{**site(), "runtime": [str(n) + "я" * 115 for n in range(7)]}]}
    )["sites"][0]
    large_profile = validate_server(
        {
            "name": "Host",
            "platform_profiles": [{**profile(), "steps": [str(n) + "я" * 100 for n in range(8)]}],
        }
    )["platform_profiles"][0]
    metadata["sites"] = [{**large_site, "site_id": f"app-{n}"} for n in range(70)]
    metadata["platform_profiles"] = [
        {**large_profile, "profile_id": f"profile-{n}"} for n in range(8)
    ]
    with connect_database(database) as connection:
        save_server(connection, "host", 0, metadata)
        for revision in range(1, 21):
            record_event(
                connection,
                "host",
                revision,
                {
                    "kind": "audit",
                    "summary": "я" * 300,
                    "source": "operator",
                    "checked_at": "2026-10-05T00:00:00+00:00",
                },
            )
        result = get_server(connection, "host", sites_limit=10, profiles_limit=8, history_limit=20)
        assert len(json.dumps(result, ensure_ascii=True).encode()) <= MAX_RESPONSE_BYTES
        assert (
            result["metadata"]["sites"]
            and result["metadata"]["platform_profiles"]
            and result["history"]
        )
        assert result["sites_page"]["next_offset"] > 0
        assert result["history_truncated"] and result["next_before"] > 0
        result = get_server(
            connection, "host", site_id="app-69", profile_id="profile-7", history_limit=20
        )
        assert len(json.dumps(result, ensure_ascii=True).encode()) <= MAX_RESPONSE_BYTES
        assert result["metadata"]["sites"][0]["site_id"] == "app-69"
        assert result["metadata"]["platform_profiles"][0]["profile_id"] == "profile-7"


def test_selected_manifest_pins_source_and_reports_unknowns_without_reading_other_cards(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    rules = root / ".cursor/rules"
    rules.mkdir(parents=True)
    index = rules / "vps-hosts.mdc"
    index.write_text("| `sample` | Sample | `ssh ops@192.0.2.1` |\n")
    card = rules / "host-sample.mdc"
    card.write_text("## Services\nNuxt and nginx.\npassword=SECRET_CANARY\n## Paths\n`/srv/site`\n")
    # A symlinked unrelated card would fail if accidentally visited.
    (rules / "host-other.mdc").symlink_to("/not/readable")
    manifest = tmp_path / "sample.json"
    data = {
        "source_id": "sample",
        "card_sha256": hashlib.sha256(card.read_bytes()).hexdigest(),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "sites": [{**site(), "source": "server-monitoring/.cursor/rules/host-sample.mdc#L1-L2"}],
        "platform_profiles": [],
        "omissions": ["Database/version is unknown"],
    }
    manifest.write_text(json.dumps(data))
    record = enrich_selected_card(root, preview_import(root)[0], "sample", sites_manifest=manifest)
    assert record["metadata"]["sites"][0]["checked_at"] is None
    assert record["metadata"]["facts"][0]["text"] == "Database/version is unknown"
    assert "CANARY" not in json.dumps(record)
    card.write_text(card.read_text() + "new line\n")
    with pytest.raises(ServerError, match="snapshot"):
        enrich_selected_card(root, preview_import(root)[0], "sample", sites_manifest=manifest)


def test_v26_migration_preserves_exact_card_history_and_refuses_old_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import storage

    database = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 26)
        initialize_database(database)
        with connect_database(database) as connection:
            payload = json.dumps(
                {"name": "Valuable preexisting card", "operator_field": "preserved exact bytes"}
            )
            connection.execute(
                "INSERT INTO servers(id,revision,data,created_at,updated_at) VALUES ('old',7,?,'before','before')",
                (payload,),
            )
            connection.execute(
                "INSERT INTO server_events(server_id,revision,kind,data,recorded_at) VALUES ('old',7,'audit','{\"summary\":\"valuable history\"}','before')"
            )
            connection.commit()
    assert initialize_database(database).schema_version == SCHEMA_VERSION
    with connect_database(database) as connection:
        assert connection.execute("SELECT data,revision FROM servers").fetchone() == (payload, 7)
        assert (
            connection.execute("SELECT data FROM server_events").fetchone()[0]
            == '{"summary":"valuable history"}'
        )
    with monkeypatch.context() as old:
        old.setattr(storage, "SCHEMA_VERSION", 26)
        with pytest.raises(storage.UnsupportedSchemaVersionError):
            connect_database(database)


def test_linked_manifest_reference_is_pinned_bounded_and_selected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    rules = root / ".cursor/rules"
    rules.mkdir(parents=True)
    index = rules / "vps-hosts.mdc"
    index.write_text("| `sample` | Sample | `ssh ops@192.0.2.1` |\n")
    card = rules / "host-sample.mdc"
    card.write_text("[sites](../../sites.md)\n")
    linked = root / "sites.md"
    linked.write_text("Known site\n")
    manifest = tmp_path / "sample.json"
    references = [{"path": "sites.md", "sha256": hashlib.sha256(linked.read_bytes()).hexdigest()}]
    data = {
        "source_id": "sample",
        "card_sha256": hashlib.sha256(card.read_bytes()).hexdigest(),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "references": references,
        "sites": [{**site(), "source": "server-monitoring/sites.md#L1"}],
        "platform_profiles": [],
        "omissions": [],
    }
    manifest.write_text(json.dumps(data))
    assert enrich_selected_card(root, preview_import(root)[0], "sample", sites_manifest=manifest)[
        "metadata"
    ]["sites"]
    linked.write_text("Changed\n")
    with pytest.raises(ServerError, match="snapshot"):
        enrich_selected_card(root, preview_import(root)[0], "sample", sites_manifest=manifest)
    references[0]["sha256"] = hashlib.sha256(linked.read_bytes()).hexdigest()
    for path in ("../outside.md", "missing.md", ".env", "sites.md/../sites.md"):
        references[0]["path"] = path
        manifest.write_text(json.dumps(data))
        with pytest.raises(ServerError):
            enrich_selected_card(root, preview_import(root)[0], "sample", sites_manifest=manifest)
    references[0]["path"] = "sites.md"
    linked.unlink()
    linked.symlink_to(card)
    manifest.write_text(json.dumps(data))
    with pytest.raises(ServerError):
        enrich_selected_card(root, preview_import(root)[0], "sample", sites_manifest=manifest)
