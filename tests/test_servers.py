from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import harness.storage as storage
from harness.registry import create_project
from harness.server_import import enrich_selected_card, preview_import
from harness.servers import (
    MAX_RESPONSE_BYTES,
    ServerConflictError,
    ServerError,
    archive_server,
    dispatch_servers,
    get_server,
    import_servers,
    list_servers,
    record_event,
    safe_text,
    save_server,
)
from harness.storage import SCHEMA_VERSION, connect_database, initialize_database


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "servers.db"
    initialize_database(path)
    return connect_database(path)


def test_independent_servers_cas_history_and_multiple_projects(
    connection: sqlite3.Connection,
) -> None:
    a, b = create_project(connection), create_project(connection)
    card = save_server(
        connection, "shared", 0, {"name": "Shared", "project_ids": [a.project_id, b.project_id]}
    )
    assert card["revision"] == 1 and len(card["metadata"]["project_ids"]) == 2
    assert connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
    with pytest.raises(ServerConflictError):
        save_server(connection, "shared", 0, {"name": "Stale"})
    with pytest.raises(ServerError):
        save_server(connection, "shared", 1, {"name": "Lost", "project_ids": ["missing"]})
    assert get_server(connection, "shared")["revision"] == 1
    card = record_event(
        connection,
        "shared",
        1,
        {
            "kind": "audit",
            "summary": "Healthy",
            "checked_at": "2026-10-05T12:00:00+07:00",
            "source": "read-only host identity check",
        },
    )
    assert card["revision"] == 2 and card["history"][0]["kind"] == "audit"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE server_events SET kind='operation'")
    connection.rollback()
    assert archive_server(connection, "shared", 2)["archived"]
    assert list_servers(connection)["total"] == 0
    assert list_servers(connection, archived=True)["total"] == 1
    assert not archive_server(connection, "shared", 3, False)["archived"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"name": "safe", "password": "CANARY"},
        {"name": "password=CANARY"},
        {"name": "safe", "connection": {"private_key": "CANARY"}},
        {"name": "safe", "rules": ["token: CANARY"]},
        {"name": "safe", "facts": [{"text": "x", "source": "x", "checked_at": "today"}]},
        {"name": "safe", "connection": {"port": True}},
    ],
)
def test_secret_and_invalid_data_is_rejected_without_persistence(
    connection: sqlite3.Connection, metadata: dict[str, object]
) -> None:
    with pytest.raises(ServerError) as error:
        save_server(connection, "safe", 0, metadata)
    assert "CANARY" not in str(error.value)
    assert connection.execute("SELECT count(*) FROM servers").fetchone()[0] == 0


@pytest.mark.parametrize("value", [None, 1, b"text", "\ud800", "\udfff", "x\x00"])
def test_safe_text_rejects_invalid_text_with_bounded_domain_error(value: object) -> None:
    with pytest.raises(ServerError) as error:
        safe_text(value)
    assert len(str(error.value).encode()) < 100
    assert "\\ud" not in str(error.value)


def test_safe_text_utf8_byte_boundaries_and_allowed_whitespace() -> None:
    assert safe_text("я" * 80, 160) == "я" * 80
    assert safe_text("\U0001f600", 4) == "\U0001f600"
    assert safe_text(" \tline\nnext ") == "line\nnext"
    for value, maximum in (("я" * 81, 160), ("\U0001f600", 3)):
        with pytest.raises(ServerError):
            safe_text(value, maximum)


def test_search_ambiguity_and_budgeted_paged_history(connection: sqlite3.Connection) -> None:
    for n in range(24):
        save_server(
            connection,
            f"host-{n}",
            0,
            {
                "name": "Shared",
                "domains": ["test.example.invalid"],
                "aliases": [f"alias-{n}"],
                "environment": "test",
            },
        )
    result = list_servers(connection, "example.invalid", limit=3)
    assert result["ambiguous"] and result["total"] == 24 and len(result["servers"]) == 3
    assert list_servers(connection, "alias-23")["total"] == 1
    assert list_servers(connection, environment="prod")["total"] == 0
    for revision in range(1, 31):
        record_event(
            connection,
            "host-0",
            revision,
            {"kind": "audit", "summary": "x" * 900, "checked_at": "2026-10-05T00:00:00Z"},
        )
    first = get_server(connection, "host-0", history_limit=20)
    assert first["history_truncated"]
    assert len(json.dumps(first, ensure_ascii=True).encode()) <= MAX_RESPONSE_BYTES
    second = get_server(connection, "host-0", before=first["next_before"])
    assert second["history"][0]["sequence"] < first["history"][-1]["sequence"]
    with pytest.raises(ServerError):
        dispatch_servers(connection, "list", {"password": "CANARY"})


def _corpus(root: Path) -> None:
    directory = root / ".cursor/rules"
    directory.mkdir(parents=True)
    (directory / "vps-hosts.mdc").write_text(
        "| `sample` | Sample test | `ssh -p 2222 ops@192.0.2.12` |\n| `sample.example.invalid` | `sample` |\n"
    )
    (directory / "host-sample.mdc").write_text(
        "## Services\nDjango, nginx and redis.\npassword=SECRET_CANARY\n## Paths\n`/srv/sample`\n`/srv/sample/.env`\n## Credentials\n-----BEGIN PRIVATE KEY-----\nKEY_CANARY\n## Backups\n`/srv/dump.sql`\n"
    )
    (directory / "host-other.mdc").write_text("OTHER_CARD_CANARY")


def test_safe_import_selected_card_preserves_existing_edits(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    root = tmp_path / "corpus"
    _corpus(root)
    entries = preview_import(root)
    assert entries[0]["metadata"]["connection"] == {"port": 2222, "user": "ops"}
    entry = enrich_selected_card(root, entries[0], "sample")
    assert entry["metadata"]["services"] == ["django", "nginx", "redis"]
    assert entry["metadata"]["paths"] == ["/srv/sample"]
    assert "CANARY" not in json.dumps(entry)
    assert entry["metadata"]["provenance"]["checked_at"] is None
    assert import_servers(connection, [entry])["created"] == 1
    card = get_server(connection, entry["server_id"])
    save_server(connection, card["server_id"], 1, {**card["metadata"], "name": "Operator edit"})
    assert import_servers(connection, [entry])["preserved"] == 1
    assert get_server(connection, entry["server_id"])["metadata"]["name"] == "Operator edit"
    assert (root / ".cursor/rules/host-other.mdc").read_text() == "OTHER_CARD_CANARY"


def test_v25_migration_preserves_prior_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.db"
    with monkeypatch.context() as previous_schema:
        previous_schema.setattr(storage, "SCHEMA_VERSION", 25)
        initialize_database(path)
        connection = connect_database(path)
        try:
            project = create_project(connection)
        finally:
            connection.close()
    assert initialize_database(path).schema_version == SCHEMA_VERSION
    connection = connect_database(path)
    assert connection.execute("SELECT id FROM projects").fetchone()[0] == project.project_id
    assert list_servers(connection)["total"] == 0
    connection.close()
