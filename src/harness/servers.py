"""Daemon-owned, local-user shared infrastructure metadata. Never executes SSH."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

MAX_SERVER_BYTES = 512 * 1024
MAX_BASE_BYTES = 32 * 1024
MAX_CHILD_BYTES = 6 * 1024
MAX_EVENT_BYTES = 2400
MAX_RESPONSE_BYTES = 48 * 1024
MAX_SITES = 10000
PASSPORT_TEXT_FIELDS = (
    "hostname",
    "provider",
    "location",
    "virtualization",
    "os",
    "kernel",
    "cpu_model",
)
PASSPORT_NUMBER_FIELDS = ("cpu_cores", "memory_mib", "swap_mib")
FACT_CATEGORIES = frozenset(
    {"system", "hardware", "resources", "services", "access", "network", "limitations", "notes"}
)
SITE_FIELDS = frozenset(
    {
        "site_id",
        "profile_id",
        "name",
        "purpose",
        "access_policy",
        "domains",
        "environment",
        "stack",
        "runtime",
        "ports",
        "paths",
        "dependencies",
        "source",
        "checked_at",
    }
)
PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "name",
        "environment",
        "stack",
        "domain_pattern",
        "path_pattern",
        "runtime",
        "access_policy",
        "steps",
        "dependencies",
        "source",
        "checked_at",
    }
)
SERVER_FIELDS = frozenset(
    {
        "name",
        "aliases",
        "role",
        "environment",
        "addresses",
        "domains",
        "services",
        "paths",
        "connection",
        "rules",
        "facts",
        "passport",
        "map_policy",
        "provenance",
        "project_ids",
        "sites",
        "platform_profiles",
    }
)
_LIST_FIELDS = frozenset(
    {"aliases", "addresses", "domains", "services", "paths", "rules", "project_ids"}
)
_SECRET = re.compile(
    r"-----BEGIN .*(?:PRIVATE KEY|OPENSSH)|(?:password|passwd|secret|token|api[_-]?key|пароль|токен)\s*[:=]\s*\S+|[a-z]+://[^\s/:]+:[^\s/@]+@",
    re.I,
)
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_HISTORY_DELETE_GUARD = (
    "CREATE TRIGGER server_events_no_delete BEFORE DELETE ON server_events "
    "BEGIN SELECT RAISE(ABORT, 'server history is append-only'); END"
)


class ServerError(ValueError):
    """Bounded metadata validation error; never includes rejected values."""


class ServerConflictError(ServerError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def safe_text(value: object, maximum: int = 512) -> str:
    message = "Invalid metadata or suspected secret; use a non-secret reference"
    if not isinstance(value, str):
        raise ServerError(message)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ServerError(message) from exc
    if (
        len(encoded) > maximum
        or any(ord(c) < 32 and c not in "\n\t" for c in value)
        or _SECRET.search(value)
    ):
        raise ServerError(message)
    return value.strip()


def server_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ServerError("Invalid server identity")
    return value


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    text = safe_text(value, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise ServerError("checked_at must include timezone") from exc
    return text


def validate_passport(data: object) -> dict[str, Any]:
    """Machine inventory, separate from transient resource/health observations."""
    if not isinstance(data, dict) or set(data) - {
        *PASSPORT_TEXT_FIELDS,
        *PASSPORT_NUMBER_FIELDS,
        "storage",
        "source",
        "checked_at",
    }:
        raise ServerError("Unknown server passport fields")
    result: dict[str, Any] = {
        key: safe_text(data.get(key, ""), 256) for key in PASSPORT_TEXT_FIELDS
    }
    for key in PASSPORT_NUMBER_FIELDS:
        value = data.get(key)
        minimum = 0 if key == "swap_mib" else 1
        maximum = 65536 if key == "cpu_cores" else 2**40
        if value is not None and (type(value) is not int or not minimum <= value <= maximum):
            raise ServerError("Invalid passport capacity; use integer counts/MiB or null")
        result[key] = value
    storage = data.get("storage", [])
    if not isinstance(storage, list) or len(storage) > 16:
        raise ServerError("Passport storage exceeds item limit")
    result["storage"] = list(dict.fromkeys(safe_text(item, 256) for item in storage if item != ""))
    result["source"] = safe_text(data.get("source", "operator"), 256)
    if not result["source"]:
        raise ServerError("Passport source is required")
    result["checked_at"] = _timestamp(data.get("checked_at"))
    return result


def validate_server(data: object) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - SERVER_FIELDS:
        raise ServerError("Unknown server fields; use the published metadata schema")
    result: dict[str, Any] = {}
    for key in ("name", "role", "environment"):
        result[key] = safe_text(data.get(key, ""), 160)
    if not result["name"]:
        raise ServerError("Server name is required")
    if "passport" in data:
        result["passport"] = validate_passport(data["passport"])
    if "map_policy" in data:
        from harness.server_map import validate_policy

        result["map_policy"] = validate_policy(data["map_policy"])
    sites = data.get("sites", [])
    if not isinstance(sites, list) or len(sites) > MAX_SITES:
        raise ServerError("Sites exceed item limit")
    result["sites"] = [validate_site(site) for site in sites]
    if len({site["site_id"] for site in result["sites"]}) != len(sites):
        raise ServerError("Site identities must be unique within the server")
    profiles = data.get("platform_profiles", [])
    if not isinstance(profiles, list) or len(profiles) > 32:
        raise ServerError("Preparation profiles exceed item limit")
    result["platform_profiles"] = [validate_profile(profile) for profile in profiles]
    if len({profile["profile_id"] for profile in result["platform_profiles"]}) != len(profiles):
        raise ServerError("Profile identities must be unique within the server")
    profile_ids = {profile["profile_id"] for profile in result["platform_profiles"]}
    if any(
        site["profile_id"] and site["profile_id"] not in profile_ids for site in result["sites"]
    ):
        raise ServerError("Site preparation profile must exist within the same server card")
    for key in _LIST_FIELDS:
        values = data.get(key, [])
        if not isinstance(values, list) or len(values) > 24:
            raise ServerError("Metadata list exceeds limit")
        result[key] = list(
            dict.fromkeys(
                safe_text(item, 256 if key != "rules" else 512) for item in values if item != ""
            )
        )
    connection = data.get("connection", {})
    if not isinstance(connection, dict) or set(connection) - {
        "ssh_alias",
        "user",
        "port",
        "key_reference",
    }:
        raise ServerError("Connection accepts only ssh_alias, user, port and key_reference")
    result["connection"] = {
        key: safe_text(value, 160) for key, value in connection.items() if key != "port"
    }
    if "port" in connection:
        port = connection["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ServerError("Invalid SSH port")
        result["connection"]["port"] = port
    facts = data.get("facts", [])
    if not isinstance(facts, list) or len(facts) > 16:
        raise ServerError("Facts exceed limit")
    result["facts"] = []
    for fact in facts:
        if (
            not isinstance(fact, dict)
            or not {"text", "source", "checked_at"} <= set(fact)
            or set(fact) - {"text", "source", "checked_at", "category"}
        ):
            raise ServerError("Facts require text, source, checked_at and optional category")
        result["facts"].append(
            {
                "text": safe_text(fact["text"], 512),
                "source": safe_text(fact["source"], 256),
                "checked_at": _timestamp(fact["checked_at"]),
            }
        )
        if "category" in fact:
            if not isinstance(fact["category"], str) or fact["category"] not in FACT_CATEGORIES:
                raise ServerError("Invalid fact category")
            result["facts"][-1]["category"] = fact["category"]
    provenance = data.get("provenance", {})
    if not isinstance(provenance, dict) or set(provenance) - {
        "source",
        "imported_at",
        "checked_at",
    }:
        raise ServerError("Invalid provenance")
    result["provenance"] = {
        "source": safe_text(provenance.get("source", "operator"), 256),
        "checked_at": _timestamp(provenance.get("checked_at")),
    }
    if "imported_at" in provenance:
        result["provenance"]["imported_at"] = _timestamp(provenance["imported_at"])
    base = {
        key: value for key, value in result.items() if key not in {"sites", "platform_profiles"}
    }
    if len(json.dumps(base, ensure_ascii=True).encode()) > MAX_BASE_BYTES:
        raise ServerError("Server base metadata exceeds byte limit")
    if (
        len(
            json.dumps(
                {k: v for k, v in result.items() if k != "sites"}, ensure_ascii=True
            ).encode()
        )
        > MAX_SERVER_BYTES
    ):
        raise ServerError("Server metadata exceeds byte limit")
    return result


def validate_site(data: object) -> dict[str, Any]:
    """Non-secret deployment context; empty versions/checked_at mean unknown, never inferred."""
    if not isinstance(data, dict) or set(data) - SITE_FIELDS:
        raise ServerError("Unknown site fields; use the published site schema")
    result: dict[str, Any] = {"site_id": server_id(data.get("site_id"))}
    profile_id = data.get("profile_id", "")
    result["profile_id"] = server_id(profile_id) if profile_id != "" else ""
    for key in ("name", "purpose", "environment", "source", "access_policy"):
        result[key] = safe_text(
            data.get(key, ""),
            512 if key == "access_policy" else 256 if key in {"source", "purpose"} else 160,
        )
    if not result["name"] or not result["source"]:
        raise ServerError("Site name and source are required")
    for key in ("domains", "runtime", "paths", "dependencies"):
        values = data.get(key, [])
        if not isinstance(values, list) or len(values) > 16:
            raise ServerError("Site list exceeds item limit")
        result[key] = list(dict.fromkeys(safe_text(value, 256) for value in values if value != ""))
    ports = data.get("ports", [])
    if (
        not isinstance(ports, list)
        or len(ports) > 16
        or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
    ):
        raise ServerError("Site ports must be bounded integer ports")
    result["ports"] = list(dict.fromkeys(ports))
    stack = data.get("stack", [])
    if not isinstance(stack, list) or len(stack) > 16:
        raise ServerError("Site stack exceeds item limit")
    result["stack"] = []
    for component in stack:
        if not isinstance(component, dict) or set(component) != {"name", "version"}:
            raise ServerError("Stack components require name and version (empty if unknown)")
        name, version = safe_text(component["name"], 80), safe_text(component["version"], 80)
        if not name:
            raise ServerError("Stack component name is required")
        result["stack"].append({"name": name, "version": version})
    result["checked_at"] = _timestamp(data.get("checked_at"))
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_CHILD_BYTES:
        raise ServerError("Site metadata exceeds byte limit")
    return result


def validate_profile(data: object) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - PROFILE_FIELDS:
        raise ServerError("Unknown preparation profile fields")
    # Reuse nested stack/evidence validation; a profile is guidance, not an actual deployment.
    site = validate_site(
        {
            "site_id": data.get("profile_id"),
            "name": data.get("name"),
            "environment": data.get("environment", ""),
            "stack": data.get("stack", []),
            "runtime": data.get("runtime", []),
            "dependencies": data.get("dependencies", []),
            "source": data.get("source"),
            "checked_at": data.get("checked_at"),
        }
    )
    result = {
        key: site[key]
        for key in (
            "name",
            "environment",
            "stack",
            "runtime",
            "dependencies",
            "source",
            "checked_at",
        )
    }
    result["profile_id"] = site["site_id"]
    for key in ("domain_pattern", "path_pattern", "access_policy"):
        result[key] = safe_text(data.get(key, ""), 512)
    steps = data.get("steps", [])
    if not isinstance(steps, list) or len(steps) > 12:
        raise ServerError("Preparation steps exceed item limit")
    result["steps"] = [safe_text(step, 512) for step in steps]
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_CHILD_BYTES:
        raise ServerError("Preparation profile exceeds byte limit")
    return result


def _has_map(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='server_map_objects'"
        ).fetchone()
        is not None
    )


def load_server_metadata(connection: sqlite3.Connection, identity: str) -> dict[str, Any]:
    """Daemon-only complete state for transactional mutation, never a disclosure API."""
    identity = server_id(identity)
    row = connection.execute("SELECT data FROM servers WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ServerError("Server not found")
    metadata: dict[str, Any] = json.loads(row[0])
    from harness.server_map import legacy_applications

    metadata["sites"] = (
        legacy_applications(connection, identity)
        if _has_map(connection)
        else metadata.get("sites", [])
    )
    metadata.setdefault("platform_profiles", [])
    metadata["project_ids"] = [
        row[0]
        for row in connection.execute(
            "SELECT project_id FROM server_projects WHERE server_id=? ORDER BY project_id",
            (identity,),
        )
    ]
    return metadata


def _get_server_snapshot(
    connection: sqlite3.Connection,
    identity: str,
    *,
    history_limit: int = 5,
    before: int | None = None,
    sites_query: str = "",
    sites_offset: int = 0,
    sites_limit: int = 5,
    site_id: str | None = None,
    profiles_offset: int = 0,
    profiles_limit: int = 4,
    profile_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    identity = server_id(identity)
    row = connection.execute(
        "SELECT revision,archived,data,created_at,updated_at FROM servers WHERE id=?", (identity,)
    ).fetchone()
    if row is None:
        raise ServerError("Server not found")
    if expected_revision is not None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ServerError("Invalid read revision")
        if row[0] != expected_revision:
            raise ServerConflictError("Server revision changed; restart page traversal")
    query = safe_text(sites_query, 256).casefold()
    for offset, limit, maximum in (
        (sites_offset, sites_limit, 10),
        (profiles_offset, profiles_limit, 8),
    ):
        if (
            type(offset) is not int
            or not 0 <= offset <= 100000
            or type(limit) is not int
            or not 0 <= limit <= maximum
        ):
            raise ServerError("Invalid child page")
    metadata = json.loads(row[2])
    from harness.server_map import legacy_applications

    metadata["sites"] = (
        legacy_applications(connection, identity)
        if _has_map(connection)
        else metadata.get("sites", [])
    )
    metadata.setdefault("platform_profiles", [])
    metadata["project_ids"] = [
        row[0]
        for row in connection.execute(
            "SELECT project_id FROM server_projects WHERE server_id=? ORDER BY project_id",
            (identity,),
        )
    ]
    all_sites = metadata["sites"]
    all_profiles = metadata["platform_profiles"]
    sites = [
        site
        for site in all_sites
        if not query
        or any(
            query in value.casefold()
            for value in (site["site_id"], site["name"], site["purpose"], *site["domains"])
        )
    ]
    profiles = all_profiles
    if site_id is not None:
        selected_id = server_id(site_id)
        sites = [site for site in all_sites if site["site_id"] == selected_id]
        if not sites:
            raise ServerError("Site not found")
        sites_offset, sites_limit = 0, 1
    if profile_id is not None:
        selected_id = server_id(profile_id)
        profiles = [profile for profile in all_profiles if profile["profile_id"] == selected_id]
        if not profiles:
            raise ServerError("Preparation profile not found")
        profiles_offset, profiles_limit = 0, 1
    metadata["sites"] = sites[sites_offset : sites_offset + sites_limit]
    metadata["platform_profiles"] = profiles[profiles_offset : profiles_offset + profiles_limit]

    def page(total: int, offset: int, selected: list[Any], full_total: int) -> dict[str, Any]:
        truncated = offset + len(selected) < total
        return {
            "total": total,
            "full_total": full_total,
            "offset": offset,
            "truncated": truncated,
            "next_offset": offset + len(selected) if truncated and selected else None,
        }

    if (
        type(history_limit) is not int
        or not 0 <= history_limit <= 20
        or (before is not None and (type(before) is not int or before <= 0))
    ):
        raise ServerError("Invalid history page")
    events = connection.execute(
        "SELECT sequence,revision,kind,data,recorded_at FROM server_events WHERE server_id=? AND revision<=? AND (? IS NULL OR sequence<?) ORDER BY sequence DESC LIMIT ?",
        (identity, row[0], before, before, history_limit + 1),
    ).fetchall()
    history = [
        {
            "sequence": e[0],
            "revision": e[1],
            "kind": e[2],
            "data": json.loads(e[3]),
            "recorded_at": e[4],
        }
        for e in events[:history_limit]
    ]
    result = {
        "scope": "local_user",
        "server_id": identity,
        "revision": row[0],
        "archived": bool(row[1]),
        "metadata": metadata,
        "sites_page": {
            **page(len(sites), sites_offset, metadata["sites"], len(all_sites)),
            "query": query,
        },
        "profiles_page": page(
            len(profiles), profiles_offset, metadata["platform_profiles"], len(all_profiles)
        ),
        "created_at": row[3],
        "updated_at": row[4],
        "history": history,
        "history_truncated": len(events) > history_limit,
        "next_before": history[-1]["sequence"] if len(events) > history_limit and history else None,
    }
    while len(json.dumps(result, ensure_ascii=True).encode()) > MAX_RESPONSE_BYTES:
        # Keep at least one requested record/event so every advertised cursor makes progress.
        if len(history) > 1:
            history.pop()
            result["history_truncated"] = True
            result["next_before"] = history[-1]["sequence"]
        elif len(metadata["sites"]) > 1 and site_id is None:
            metadata["sites"].pop()
            result["sites_page"].update(
                page(len(sites), sites_offset, metadata["sites"], len(all_sites))
            )
        elif len(metadata["platform_profiles"]) > 1 and profile_id is None:
            metadata["platform_profiles"].pop()
            result["profiles_page"] = page(
                len(profiles), profiles_offset, metadata["platform_profiles"], len(all_profiles)
            )
        else:
            raise ServerError("Response exceeds byte limit; request one child and smaller history")
    return result


def get_server(
    connection: sqlite3.Connection,
    identity: str,
    *,
    history_limit: int = 5,
    before: int | None = None,
    sites_query: str = "",
    sites_offset: int = 0,
    sites_limit: int = 5,
    site_id: str | None = None,
    profiles_offset: int = 0,
    profiles_limit: int = 4,
    profile_id: str | None = None,
    expected_revision: int | None = None,
    collection: str | None = None,
    object_id: str | None = None,
    query: str = "",
    status: str = "",
    application_id: str = "",
    sort: str = "name",
    descending: bool = False,
    offset: int = 0,
    limit: int = 50,
    links_offset: int = 0,
) -> dict[str, Any]:
    if collection is not None:
        from harness.server_map import read_map

        return read_map(
            connection,
            identity,
            collection=collection,
            object_id=object_id,
            query=query,
            status=status,
            application_id=application_id,
            sort=sort,
            descending=descending,
            offset=offset,
            limit=limit,
            expected_revision=expected_revision,
            links_offset=links_offset,
        )
    if (
        object_id is not None
        or query
        or status
        or application_id
        or sort != "name"
        or descending
        or offset
        or limit != 50
        or links_offset
    ):
        raise ServerError("Map filters require an explicit collection")
    # One SQLite read snapshot binds base, revision, child pages, links and history together.
    connection.execute("SAVEPOINT server_read")
    try:
        return _get_server_snapshot(
            connection,
            identity,
            history_limit=history_limit,
            before=before,
            sites_query=sites_query,
            sites_offset=sites_offset,
            sites_limit=sites_limit,
            site_id=site_id,
            profiles_offset=profiles_offset,
            profiles_limit=profiles_limit,
            profile_id=profile_id,
            expected_revision=expected_revision,
        )
    finally:
        connection.execute("RELEASE SAVEPOINT server_read")


def list_servers(
    connection: sqlite3.Connection,
    query: str = "",
    *,
    archived: bool = False,
    environment: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    if type(archived) is not bool:
        raise ServerError("archived must be boolean")
    query = safe_text(query, 256).casefold()
    environment = safe_text(environment, 160)
    if (
        type(limit) is not int
        or not 1 <= limit <= 50
        or type(offset) is not int
        or not 0 <= offset <= 100000
    ):
        raise ServerError("Invalid server page")
    matches = []
    for identity, revision, data in connection.execute(
        "SELECT id,revision,data FROM servers WHERE archived=? ORDER BY updated_at DESC,id",
        (int(archived),),
    ):
        metadata = json.loads(data)
        searchable = [
            identity,
            metadata["name"],
            metadata["role"],
            *metadata["aliases"],
            *metadata["addresses"],
            *metadata["domains"],
            *[
                value
                for site in load_server_metadata(connection, identity)["sites"]
                for value in (site["name"], site["purpose"], *site["domains"])
            ],
        ]
        if query and not any(query in item.casefold() for item in searchable):
            continue
        if environment and metadata["environment"] != environment:
            continue
        matches.append(
            {
                "server_id": identity,
                "revision": revision,
                **{
                    key: metadata[key]
                    for key in ("name", "environment", "role", "addresses", "domains")
                },
            }
        )
    selected = matches[offset : offset + limit]
    result = {
        "scope": "local_user",
        "servers": selected,
        "total": len(matches),
        "ambiguous": len(matches) > 1,
        "truncated": len(matches) > offset + len(selected),
        "next_offset": offset + len(selected),
    }
    while len(json.dumps(result, ensure_ascii=True).encode()) > MAX_RESPONSE_BYTES and selected:
        selected.pop()
        result["truncated"] = True
        result["next_offset"] = offset + len(selected)
    return result


def _write(
    connection: sqlite3.Connection,
    identity: str,
    expected_revision: int,
    *,
    metadata: dict[str, Any] | None = None,
    archived: bool | None = None,
    kind: str = "updated",
    event: dict[str, Any] | None = None,
    source_key: str | None = None,
    imported_site_ids: tuple[str, ...] = (),
    imported_profile_ids: tuple[str, ...] = (),
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    identity = server_id(identity)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ServerError("Explicit nonnegative expected_revision is required")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT revision,archived FROM servers WHERE id=?", (identity,)
        ).fetchone()
        revision = 0 if row is None else row[0]
        if revision != expected_revision:
            raise ServerConflictError(
                "Server revision changed; fetch the card and reconcile before retry"
            )
        if row is None and metadata is None:
            raise ServerError("Server not found")
        previous = load_server_metadata(connection, identity) if row is not None else None
        if transform is not None:
            metadata = validate_server(transform(load_server_metadata(connection, identity)))
        from harness.server_map import (
            check_map_growth,
            map_counts,
            seed_references,
            sync_legacy_applications,
        )

        before = map_counts(connection, identity) if _has_map(connection) else (0, 0)

        stored = (
            None
            if metadata is None
            else {**metadata, "sites": []}
            if _has_map(connection)
            else metadata
        )
        now = _now()
        if row is None:
            connection.execute(
                "INSERT INTO servers(id,revision,data,source_key,created_at,updated_at) VALUES (?,1,?,?,?,?)",
                (identity, json.dumps(stored, ensure_ascii=False), source_key, now, now),
            )
            kind = "created"
        else:
            connection.execute(
                "UPDATE servers SET revision=revision+1,updated_at=?,data=COALESCE(?,data),archived=COALESCE(?,archived) WHERE id=? AND revision=?",
                (
                    now,
                    None if stored is None else json.dumps(stored, ensure_ascii=False),
                    None if archived is None else int(archived),
                    identity,
                    revision,
                ),
            )
        if metadata is not None:
            if _has_map(connection):
                sync_legacy_applications(connection, identity, metadata["sites"])
                seed_references(connection, identity, metadata, previous=previous)
                check_map_growth(connection, identity, before)
            for project in metadata["project_ids"]:
                if (
                    connection.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone()
                    is None
                ):
                    raise ServerError("Linked Project not found")
            connection.execute("DELETE FROM server_projects WHERE server_id=?", (identity,))
            connection.executemany(
                "INSERT INTO server_projects VALUES (?,?)",
                [(identity, project) for project in metadata["project_ids"]],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO server_imported_sites(server_id,site_id) VALUES (?,?)",
                [(identity, site_id) for site_id in imported_site_ids],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO server_imported_profiles(server_id,profile_id) VALUES (?,?)",
                [(identity, profile_id) for profile_id in imported_profile_ids],
            )
        connection.execute(
            "INSERT INTO server_events(server_id,revision,kind,data,recorded_at) VALUES (?,?,?,?,?)",
            (
                identity,
                revision + 1,
                kind,
                json.dumps(event or {"summary": "Metadata saved"}, ensure_ascii=False),
                now,
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return get_server(connection, identity)


def edit_server(
    connection: sqlite3.Connection,
    identity: str,
    expected_revision: int,
    *,
    metadata: object = None,
    site: object = None,
    profile: object = None,
    delete_site_id: str | None = None,
    delete_profile_id: str | None = None,
    map_operation: object = None,
) -> dict[str, Any]:
    """One base patch or one child mutation, merging under the server CAS transaction."""
    if (
        sum(
            value is not None
            for value in (metadata, site, profile, delete_site_id, delete_profile_id, map_operation)
        )
        != 1
    ):
        raise ServerError("Choose exactly one base metadata patch or child operation")
    if map_operation is not None:
        from harness.server_map import mutate_map

        return mutate_map(connection, identity, expected_revision, map_operation)
    if expected_revision == 0:
        if metadata is None:
            raise ServerError("Creation requires server metadata")
        return save_server(connection, identity, expected_revision, metadata)
    if metadata is not None and (
        not isinstance(metadata, dict)
        or set(metadata) - (SERVER_FIELDS - {"sites", "platform_profiles"})
    ):
        raise ServerError("Base patch cannot replace sites/profiles; use one child operation")
    child = (
        validate_site(site)
        if site is not None
        else validate_profile(profile)
        if profile is not None
        else None
    )
    deleted = (
        server_id(delete_site_id or delete_profile_id)
        if delete_site_id is not None or delete_profile_id is not None
        else None
    )

    def merge(current: dict[str, Any]) -> dict[str, Any]:
        if metadata is not None:
            current.update(metadata)
        else:
            collection, key = (
                ("sites", "site_id")
                if site is not None or delete_site_id is not None
                else ("platform_profiles", "profile_id")
            )
            children = current[collection]
            if deleted is not None:
                if not any(item[key] == deleted for item in children):
                    raise ServerError("Child no longer exists; fetch and reconcile")
                current[collection] = [item for item in children if item[key] != deleted]
            elif child is not None:
                current[collection] = [
                    child if item[key] == child[key] else item for item in children
                ]
                if not any(item[key] == child[key] for item in children):
                    current[collection].append(child)
        return current

    return _write(connection, identity, expected_revision, transform=merge)


def save_server(
    connection: sqlite3.Connection,
    identity: str,
    expected_revision: int,
    metadata: object,
    *,
    source_key: str | None = None,
) -> dict[str, Any]:
    return _write(
        connection,
        identity,
        expected_revision,
        metadata=validate_server(metadata),
        source_key=source_key,
        imported_site_ids=tuple(site["site_id"] for site in validate_server(metadata)["sites"])
        if source_key
        else (),
        imported_profile_ids=tuple(
            profile["profile_id"] for profile in validate_server(metadata)["platform_profiles"]
        )
        if source_key
        else (),
    )


def archive_server(
    connection: sqlite3.Connection, identity: str, expected_revision: int, archived: bool = True
) -> dict[str, Any]:
    if type(archived) is not bool:
        raise ServerError("archived must be boolean")
    return _write(
        connection,
        identity,
        expected_revision,
        archived=archived,
        kind="archived" if archived else "restored",
    )


def record_event(
    connection: sqlite3.Connection, identity: str, expected_revision: int, event: object
) -> dict[str, Any]:
    if (
        not isinstance(event, dict)
        or set(event) - {"kind", "summary", "findings", "checked_at", "source"}
        or not isinstance(event.get("kind"), str)
        or event.get("kind") not in {"audit", "operation"}
    ):
        raise ServerError("Event requires audit/operation kind and bounded evidence")
    findings = event.get("findings", [])
    if not isinstance(findings, list) or len(findings) > 12:
        raise ServerError("Findings exceed limit")
    normalized = {
        "summary": safe_text(event.get("summary"), 1000),
        "findings": [safe_text(f, 512) for f in findings],
        "checked_at": _timestamp(event.get("checked_at")),
        "source": safe_text(event.get("source", "agent"), 256),
    }
    if (
        not normalized["summary"]
        or not normalized["checked_at"]
        or len(json.dumps(normalized, ensure_ascii=True).encode()) > MAX_EVENT_BYTES
    ):
        raise ServerError("Recorded evidence requires summary, checked_at and bounded size")
    return _write(connection, identity, expected_revision, kind=event["kind"], event=normalized)


def clear_server_history(
    connection: sqlite3.Connection,
    identity: str,
    expected_revision: int,
    confirm: bool,
) -> dict[str, Any]:
    """Explicit operator exception; ordinary event writes remain append-only."""
    identity = server_id(identity)
    if type(expected_revision) is not int or expected_revision < 1:
        raise ServerError("Explicit positive expected_revision is required")
    if confirm is not True:
        raise ServerError("Explicit operator confirmation is required to clear history")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT revision FROM servers WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ServerError("Server not found")
        if row[0] != expected_revision:
            raise ServerConflictError(
                "Server revision changed; fetch the card and reconcile before retry"
            )
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='server_events_no_delete'"
        ).fetchone()
        if (
            trigger is None
            or " ".join(trigger[0].split()).rstrip(";").casefold()
            != _HISTORY_DELETE_GUARD.casefold()
        ):
            raise ServerError("Server history protection is unavailable")
        # SQLite DDL is transactional. The writer lock prevents any concurrent writer from
        # observing a committed state without this guard; rollback restores it and all rows.
        connection.execute("DROP TRIGGER server_events_no_delete")
        deleted = connection.execute(
            "DELETE FROM server_events WHERE server_id=?", (identity,)
        ).rowcount
        connection.execute(trigger[0])
        now = _now()
        connection.execute(
            "UPDATE servers SET revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (now, identity, expected_revision),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    # An empty history stays empty. This operator acknowledgement is not a server event.
    return {
        "server_id": identity,
        "revision": expected_revision + 1,
        "deleted_events": deleted,
        "updated_at": now,
    }


def dispatch_servers(
    connection: sqlite3.Connection, operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    allowed = {
        "list": {"query", "archived", "environment", "limit", "offset"},
        "get": {
            "server_id",
            "history_limit",
            "before",
            "sites_query",
            "sites_offset",
            "sites_limit",
            "site_id",
            "profiles_offset",
            "profiles_limit",
            "profile_id",
            "expected_revision",
            "collection",
            "object_id",
            "query",
            "status",
            "application_id",
            "sort",
            "descending",
            "offset",
            "limit",
            "links_offset",
        },
        "save": {
            "server_id",
            "expected_revision",
            "metadata",
            "site",
            "profile",
            "delete_site_id",
            "delete_profile_id",
            "map_operation",
        },
        "archive": {"server_id", "expected_revision", "archived"},
        "record": {"server_id", "expected_revision", "event"},
        "enrich": {"server_id", "expected_revision", "metadata"},
        "clear_history": {"server_id", "expected_revision", "confirm"},
    }
    if operation not in allowed or set(arguments) - allowed[operation]:
        raise ServerError("Invalid server operation arguments")
    try:
        if operation == "list":
            return list_servers(connection, **arguments)
        if operation == "get":
            return get_server(
                connection,
                arguments["server_id"],
                **{key: value for key, value in arguments.items() if key != "server_id"},
            )
        if operation == "save":
            return edit_server(
                connection,
                arguments["server_id"],
                arguments["expected_revision"],
                **{
                    key: value
                    for key, value in arguments.items()
                    if key not in {"server_id", "expected_revision"}
                },
            )
        if operation == "enrich":
            return enrich_server(
                connection,
                arguments["server_id"],
                arguments["expected_revision"],
                arguments["metadata"],
            )
        if operation == "archive":
            return archive_server(
                connection,
                arguments["server_id"],
                arguments["expected_revision"],
                arguments.get("archived", True),
            )
        if operation == "clear_history":
            return clear_server_history(
                connection,
                arguments["server_id"],
                arguments["expected_revision"],
                arguments["confirm"],
            )
        return record_event(
            connection, arguments["server_id"], arguments["expected_revision"], arguments["event"]
        )
    except KeyError as exc:
        raise ServerError("Required server arguments are missing") from exc


def enrich_server(
    connection: sqlite3.Connection, identity: str, expected_revision: int, imported: object
) -> dict[str, Any]:
    """CAS additive import; imported site IDs remain recorded after an operator deletes a site."""
    incoming = validate_server(imported)
    current = load_server_metadata(connection, identity)
    for key in ("services", "paths", "rules"):
        current[key] = list(dict.fromkeys([*current[key], *incoming[key]]))
    seen = {
        row[0]
        for row in connection.execute(
            "SELECT site_id FROM server_imported_sites WHERE server_id=?", (identity,)
        )
    }
    seen.update(site["site_id"] for site in current["sites"])
    current["sites"].extend(site for site in incoming["sites"] if site["site_id"] not in seen)
    seen_profiles = {
        row[0]
        for row in connection.execute(
            "SELECT profile_id FROM server_imported_profiles WHERE server_id=?", (identity,)
        )
    }
    seen_profiles.update(profile["profile_id"] for profile in current["platform_profiles"])
    current["platform_profiles"].extend(
        profile
        for profile in incoming["platform_profiles"]
        if profile["profile_id"] not in seen_profiles
    )
    source = incoming["provenance"]["source"]
    fact = {
        "text": "Imported service, path and site references remain unverified",
        "source": source,
        "checked_at": None,
    }
    if fact not in current["facts"]:
        current["facts"].append(fact)
    for imported_fact in incoming["facts"]:
        if imported_fact["checked_at"] is not None:
            raise ServerError("Imported facts must remain unverified")
        if imported_fact not in current["facts"]:
            current["facts"].append(imported_fact)
    return _write(
        connection,
        identity,
        expected_revision,
        metadata=validate_server(current),
        imported_site_ids=tuple(site["site_id"] for site in incoming["sites"]),
        imported_profile_ids=tuple(
            profile["profile_id"] for profile in incoming["platform_profiles"]
        ),
    )


def import_servers(connection: sqlite3.Connection, entries: object) -> dict[str, Any]:
    """Daemon-only import: create safe records; never overwrite edited/imported rows on rerun."""
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ServerError("Invalid import batch")
    validated = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"server_id", "source_key", "metadata"}:
            raise ServerError("Invalid import record")
        validated.append(
            (
                server_id(entry["server_id"]),
                safe_text(entry["source_key"], 256),
                validate_server(entry["metadata"]),
            )
        )
    created = skipped = 0
    for identity, source_key, metadata in validated:
        if connection.execute(
            "SELECT 1 FROM servers WHERE id=? OR source_key=?", (identity, source_key)
        ).fetchone():
            skipped += 1
            continue
        try:
            save_server(connection, identity, 0, metadata, source_key=source_key)
            created += 1
        except (ServerConflictError, sqlite3.IntegrityError):
            skipped += 1
    return {"scope": "local_user", "created": created, "preserved": skipped}
