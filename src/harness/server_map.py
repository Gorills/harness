# ruff: noqa: RUF001
"""Typed, linked server inventory. Stores evidence; never executes a stored procedure."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from harness.servers import ServerConflictError, ServerError, _now, _timestamp, safe_text, server_id

KINDS = (
    "application",
    "domain",
    "route",
    "directory",
    "service",
    "certificate",
    "certificate_version",
    "certificate_use",
)
MAX_OBJECTS = 10000  # per server, explicit quota rather than a silently truncated UI
MAX_LINKS = 20000
MAX_OBJECT_BYTES = 12 * 1024
MAX_RESPONSE_BYTES = 48 * 1024
COMMON = {"name", "source", "checked_at", "reason", "basis", "evidence"}
FIELDS: dict[str, dict[str, str]] = {
    "application": {
        "purpose": "text",
        "environment": "text",
        "owner": "text",
        "description": "long",
        "usage": "usage",
        "availability": "availability",
        "probe_scope": "probe_scope",
        "probe_url": "text",
        "expected_result": "text",
        "access_policy": "long",
        "profile_id": "text",
        "domains": "list",
        "paths": "list",
        "runtime": "list",
        "dependencies": "list",
        "ports": "ports",
        "stack": "stack",
    },
    "domain": {
        "hostname": "text",
        "pattern": "bool",
        "discovery_sources": "list",
        "dns": "list",
        "destination": "destination",
        "availability": "availability",
        "probe_scope": "probe_scope",
        "probe_url": "text",
        "expected_result": "text",
        "https": "tls",
        "redirect": "text",
        "public_result": "long",
        "origin_result": "long",
    },
    "route": {
        "hostname": "text",
        "address": "text",
        "observed_at": "date",
        "protocol": "protocol",
        "port": "port",
        "path": "text",
        "config_reference": "text",
        "target": "text",
        "public_result": "long",
        "origin_result": "long",
        "availability": "availability",
        "probe_scope": "probe_scope",
        "probe_url": "text",
        "expected_result": "text",
        "https": "tls",
    },
    "directory": {
        "path": "text",
        "context": "text",
        "entry_type": "entry_type",
        "consumer_checks": "consumer_checks",
        "role": "directory_role",
        "usage": "directory_usage",
        "size_bytes": "number",
        "size_method": "text",
        "exists": "exists",
        "target_path": "text",
        "retention": "long",
        "closed_application": "text",
        "decision_owner": "text",
        "decision_at": "date",
        "links_checked_at": "date",
    },
    "service": {
        "manager": "manager",
        "identifier": "text",
        "role": "text",
        "purpose": "long",
        "state": "service_state",
        "usage": "usage",
        "launch": "text",
        "config_reference": "text",
    },
    "certificate": {
        "issuer": "text",
        "client": "text",
        "issuance": "mode",
        "challenge": "challenge",
        "renewal": "mode",
        "renewal_state": "renewal_state",
        "deployment": "mode",
        "deployment_state": "renewal_state",
        "owner": "text",
        "issued_at": "date",
        "last_attempt": "date",
        "last_success": "date",
        "next_check": "date",
        "automation_reference": "text",
        "issuance_location": "text",
        "procedure": "long",
        "failure": "long",
    },
    "certificate_version": {
        "fingerprint": "text",
        "issuer": "text",
        "sans": "list",
        "not_before": "date",
        "not_after": "date",
        "key_type": "text",
        "certificate_reference": "text",
        "trust": "tls",
    },
    "certificate_use": {
        "endpoint": "text",
        "address": "text",
        "sni": "text",
        "port": "port",
        "observed_at": "date",
        "trust_context": "text",
        "termination": "termination",
        "certificate_file": "text",
        "key_reference": "text",
        "config_reference": "text",
        "configured_fingerprint": "text",
        "file_fingerprint": "text",
        "observed_fingerprints": "list",
        "deployment": "mode",
        "deployment_state": "renewal_state",
        "reload": "text",
        "owner": "text",
        "procedure": "long",
        "https": "tls",
    },
}
CONSUMER_SOURCES = ("docker", "systemd", "cron", "proxy", "symlinks")
ENUMS = {
    "probe_scope": ("unknown", "transport", "endpoint", "functional"),
    "entry_type": ("unknown", "directory", "file", "socket", "symlink", "other"),
    "usage": ("unknown", "active", "closed", "replaced"),
    "availability": ("unknown", "available", "login", "redirect", "error"),
    "destination": ("unknown", "this_server", "external_proxy", "other_server", "missing"),
    "tls": ("unknown", "valid", "expired", "untrusted", "mismatch", "error", "internal"),
    "protocol": ("https", "http", "tcp"),
    "directory_role": (
        "unknown",
        "code",
        "uploads",
        "data",
        "release",
        "backup",
        "cache",
        "shared",
    ),
    "directory_usage": ("unknown", "active", "retained", "candidate", "leftover"),
    "exists": ("unknown", "present", "missing"),
    "manager": ("unknown", "systemd", "docker", "process", "cron", "external"),
    "service_state": ("unknown", "running", "stopped", "failed", "scheduled"),
    "mode": ("unknown", "automatic", "manual", "mixed", "external"),
    "renewal_state": ("unknown", "configured", "verified", "failed", "pending"),
    "challenge": ("unknown", "http-01", "dns-01", "tls-alpn-01", "supplier", "manual"),
    "termination": ("unknown", "server", "proxy", "cdn", "application", "external"),
}
BASES = (
    "unknown",
    "operator",
    "configuration",
    "probe",
    "process",
    "certificate",
    "import",
    "reference",
    "complete_scan",
)
# Observations may not replace business intent, ownership or the manual runbook.
OPERATOR_FIELDS = {
    "owner",
    "usage",
    "purpose",
    "description",
    "retention",
    "closed_application",
    "decision_owner",
    "decision_at",
    "links_checked_at",
    "procedure",
    "access_policy",
    "profile_id",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _map_timestamp(value: object) -> str | None:
    result = _timestamp(value)
    if result:
        _date(result)
    return result


def effective_evidence(kind: str, obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Materialize field provenance before replacing its common fallback."""
    evidence = {item["field"]: item for item in obj.get("evidence", [])}
    for field in FIELDS[kind]:
        if field not in evidence and obj.get(field) not in (None, "", "unknown", []):
            evidence[field] = {
                "field": field,
                "checked_at": obj.get("observed_at")
                if field == "observed_fingerprints"
                else obj.get("checked_at"),
                "source": obj.get("source", "operator"),
                "basis": obj.get("basis", "unknown"),
                "reason": obj.get("reason", ""),
            }
    return evidence


def map_counts(connection: sqlite3.Connection, identity: str) -> tuple[int, int]:
    counts = [
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE server_id=?", (identity,)
        ).fetchone()[0]
        for table in ("server_map_objects", "server_map_links")
    ]
    return counts[0], counts[1]


def check_map_growth(
    connection: sqlite3.Connection, identity: str, before: tuple[int, int]
) -> None:
    after = map_counts(connection, identity)
    for old, new, quota in zip(before, after, (MAX_OBJECTS, MAX_LINKS), strict=True):
        if new > quota and new > old:
            raise ServerError("Server inventory quota exceeded; legacy write was not truncated")


def validate_object(kind: str, value: object) -> dict[str, Any]:
    if (
        kind not in KINDS
        or not isinstance(value, dict)
        or set(value) - (COMMON | set(FIELDS[kind]))
    ):
        raise ServerError("Unknown map type or fields; use the typed inventory contract")
    result: dict[str, Any] = {}
    for key in ("name", "source", "reason", "basis"):
        result[key] = safe_text(
            value.get(key, "operator" if key == "source" else "unknown" if key == "basis" else ""),
            512 if key == "reason" else 256,
        )
    if not result["name"] or not result["source"] or result["basis"] not in BASES:
        raise ServerError("Name, non-secret source and known evidence basis are required")
    result["checked_at"] = _map_timestamp(value.get("checked_at"))
    for key, kind_type in FIELDS[kind].items():
        raw = value.get(key)
        if kind_type in ENUMS:
            default = "https" if kind_type == "protocol" else "unknown"
            if raw is not None and raw not in ENUMS[kind_type]:
                raise ServerError("Invalid inventory state")
            result[key] = raw or default
        elif kind_type in {"text", "long"}:
            result[key] = safe_text(raw or "", 2048 if kind_type == "long" else 512)
        elif kind_type == "date":
            result[key] = _map_timestamp(raw)
        elif kind_type in {"number", "port"}:
            maximum = 65535 if kind_type == "port" else 2**63 - 1
            minimum = 1 if kind_type == "port" else 0
            if raw is not None and (type(raw) is not int or not minimum <= raw <= maximum):
                raise ServerError(f"Inventory numeric field {key} requires {minimum}..{maximum}")
            result[key] = raw
        elif kind_type == "bool":
            if raw is not None and type(raw) is not bool:
                raise ServerError("Use a boolean for pattern")
            result[key] = bool(raw)
        elif kind_type in {"list", "ports", "stack", "consumer_checks"}:
            raw = [] if raw is None else raw
            if not isinstance(raw, list) or len(raw) > 64:
                raise ServerError("Inventory field exceeds 64 items; use independent records/links")
            if kind_type == "consumer_checks":
                if any(item not in CONSUMER_SOURCES for item in raw):
                    raise ServerError("Consumer checks accept docker/systemd/cron/proxy/symlinks")
                result[key] = list(dict.fromkeys(raw))
            elif kind_type == "list":
                result[key] = list(dict.fromkeys(safe_text(item, 256) for item in raw))
            elif kind_type == "ports":
                if any(type(item) is not int or not 1 <= item <= 65535 for item in raw):
                    raise ServerError("Invalid port list")
                result[key] = list(dict.fromkeys(raw))
            else:
                result[key] = []
                for item in raw:
                    if not isinstance(item, dict) or set(item) != {"name", "version"}:
                        raise ServerError("Stack accepts name/version records")
                    result[key].append({k: safe_text(item[k], 80) for k in ("name", "version")})
    evidence = value.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 32:
        raise ServerError("Evidence exceeds limit")
    result["evidence"] = []
    for item in evidence:
        if (
            not isinstance(item, dict)
            or set(item) != {"field", "source", "checked_at", "reason", "basis"}
            or item["field"] not in FIELDS[kind]
        ):
            raise ServerError(
                "Evidence requires a typed field, source, checked_at, reason and basis"
            )
        normalized: dict[str, Any] = {
            k: safe_text(item[k], 512 if k == "reason" else 256)
            for k in ("field", "source", "reason", "basis")
        }
        normalized["checked_at"] = _map_timestamp(item["checked_at"])
        if normalized["basis"] not in BASES or not normalized["source"]:
            raise ServerError("Evidence basis and source are required")
        result["evidence"].append(normalized)
    if kind in {"application", "domain", "route"} and result["probe_scope"] in {
        "endpoint",
        "functional",
    }:
        if not result["probe_url"] or not result["expected_result"]:
            raise ServerError("Expected endpoint checks require probe_url and expected_result")
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(result["probe_url"])
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            valid = False
        if not valid:
            raise ServerError("Probe URL requires a non-secret HTTP/HTTPS address")
    if kind == "domain":
        hostname = result["hostname"] or result["name"]
        pattern = hostname.startswith("*.")
        try:
            normalized_name = (
                (hostname[2:] if pattern else hostname)
                .rstrip(".")
                .encode("idna")
                .decode("ascii")
                .lower()
            )
        except UnicodeError as exc:
            raise ServerError("Invalid DNS name") from exc
        import re

        if len(normalized_name) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in normalized_name.split(".")
        ):
            raise ServerError("Invalid DNS name")
        result["hostname"] = ("*." if pattern else "") + normalized_name
        result["pattern"] = pattern
    if kind == "directory":
        if not result["path"].startswith("/"):
            raise ServerError("Directory requires an absolute path and context")
        usage = result["usage"]
        if usage in {"candidate", "leftover", "retained"} and (
            not result["reason"] or result["basis"] == "unknown"
        ):
            raise ServerError("Directory classification requires an explicit reason and basis")
        if usage == "leftover" and (
            result["basis"] != "operator"
            or not result["closed_application"]
            or not result["decision_owner"]
            or not result["decision_at"]
            or not result["links_checked_at"]
        ):
            raise ServerError(
                "Confirmed leftovers require former application, operator name, decision date and date of checked links"
            )
        if usage == "retained" and not result["retention"]:
            raise ServerError("Retained directories require a storage purpose")
    if (
        kind == "certificate_use"
        and result["observed_fingerprints"]
        and (
            not result["address"]
            or not result["sni"]
            or result["port"] is None
            or not result["observed_at"]
        )
    ):
        raise ServerError("Observed TLS versions require address, SNI, port and observation time")
    for key, field_type in FIELDS[kind].items():
        if field_type == "date" and result[key]:
            _date(result[key])
    if kind == "certificate_version" and result["not_before"] and result["not_after"]:
        if _date(result["not_before"]) >= _date(result["not_after"]):
            raise ServerError("Certificate validity dates must increase")
    if kind in {"certificate", "certificate_use"}:
        for key in ("renewal_state", "deployment_state"):
            if result.get(key) == "verified" and not any(
                e["field"] == key and e["checked_at"] and e["basis"] == "probe"
                for e in result["evidence"]
            ):
                raise ServerError(
                    "Verified automation/deployment requires dated probe evidence for that field"
                )
    if len(_json(result).encode()) > MAX_OBJECT_BYTES:
        raise ServerError("Inventory object exceeds 12 KiB; separate objects and links")
    return result


def _date(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ServerError("Inventory date is outside the supported UTC range") from exc


def migrate_map(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE server_legacy_snapshots(server_id TEXT PRIMARY KEY REFERENCES servers(id), data TEXT NOT NULL CHECK(json_valid(data)))"
    )
    connection.execute(
        "CREATE TABLE server_map_objects(server_id TEXT NOT NULL REFERENCES servers(id), kind TEXT NOT NULL, object_id TEXT NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(server_id,kind,object_id), CHECK(kind IN ('application','domain','route','directory','service','certificate','certificate_version','certificate_use')))"
    )
    connection.execute(
        "CREATE TABLE server_map_links(server_id TEXT NOT NULL, link_id TEXT NOT NULL, from_kind TEXT NOT NULL, from_id TEXT NOT NULL, to_kind TEXT NOT NULL, to_id TEXT NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(server_id,link_id), FOREIGN KEY(server_id,from_kind,from_id) REFERENCES server_map_objects(server_id,kind,object_id), FOREIGN KEY(server_id,to_kind,to_id) REFERENCES server_map_objects(server_id,kind,object_id))"
    )
    connection.execute(
        "CREATE TABLE server_map_checks(server_id TEXT NOT NULL REFERENCES servers(id), check_id TEXT NOT NULL, expected_revision INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','published','failed')), data TEXT NOT NULL CHECK(json_valid(data)), created_at TEXT NOT NULL, PRIMARY KEY(server_id,check_id))"
    )
    connection.execute(
        "CREATE TABLE server_map_batches(server_id TEXT NOT NULL, check_id TEXT NOT NULL, batch INTEGER NOT NULL CHECK(batch>=0), digest TEXT NOT NULL, data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(server_id,check_id,batch), FOREIGN KEY(server_id,check_id) REFERENCES server_map_checks(server_id,check_id))"
    )
    connection.execute(
        "CREATE INDEX server_map_links_from_idx ON server_map_links(server_id,from_kind,from_id)"
    )
    connection.execute(
        "CREATE INDEX server_map_links_to_idx ON server_map_links(server_id,to_kind,to_id)"
    )
    for identity, raw in connection.execute("SELECT id,data FROM servers").fetchall():
        connection.execute("INSERT INTO server_legacy_snapshots VALUES (?,?)", (identity, raw))
        metadata = json.loads(raw)
        sync_legacy_applications(connection, identity, metadata.get("sites", []))
        seed_references(connection, identity, metadata, grandfather=True)
        # Existing JSON bytes stay exact for recovery. Reads use normalized rows; the
        # next base write stores an empty legacy sites projection instead of two owners.


def all_objects(connection: sqlite3.Connection, identity: str, kind: str) -> list[dict[str, Any]]:
    return [
        {"object_id": row[0], "kind": kind, **json.loads(row[1])}
        for row in connection.execute(
            "SELECT object_id,data FROM server_map_objects WHERE server_id=? AND kind=? ORDER BY object_id",
            (identity, kind),
        )
    ]


def legacy_applications(connection: sqlite3.Connection, identity: str) -> list[dict[str, Any]]:
    from harness.servers import SITE_FIELDS

    return [
        {
            "site_id": obj["object_id"],
            **{k: v for k, v in obj.items() if k in SITE_FIELDS and k != "site_id"},
        }
        for obj in [
            {"object_id": r[0], **json.loads(r[1])}
            for r in connection.execute(
                "SELECT object_id,data FROM server_map_objects WHERE server_id=? AND kind='application' ORDER BY rowid",
                (identity,),
            )
        ]
    ]


def sync_legacy_applications(
    connection: sqlite3.Connection, identity: str, sites: list[dict[str, Any]]
) -> None:
    existing = {
        row[0]: json.loads(row[1])
        for row in connection.execute(
            "SELECT object_id,data FROM server_map_objects WHERE server_id=? AND kind='application'",
            (identity,),
        )
    }
    incoming = {site["site_id"]: site for site in sites}
    for key in existing.keys() - incoming.keys():
        connection.execute(
            "DELETE FROM server_map_links WHERE server_id=? AND ((from_kind='application' AND from_id=?) OR (to_kind='application' AND to_id=?)) AND json_extract(data,'$.basis')='reference'",
            (identity, key, key),
        )
        if links_for(connection, identity, "application", key):
            raise ServerError("Application has links; remove its links explicitly before deleting")
        connection.execute(
            "DELETE FROM server_map_objects WHERE server_id=? AND kind='application' AND object_id=?",
            (identity, key),
        )
    for key, site in incoming.items():
        value = {**existing.get(key, {}), **{k: v for k, v in site.items() if k != "site_id"}}
        if key not in existing:
            value = validate_object("application", value)
        elif value != existing[key]:
            evidence = effective_evidence("application", existing[key])
            for field in FIELDS["application"]:
                if value.get(field) != existing[key].get(field):
                    evidence[field] = {
                        "field": field,
                        **{k: value.get(k) for k in ("source", "checked_at", "basis", "reason")},
                    }
            value["evidence"] = list(evidence.values())
            value = validate_object("application", value)
        _map_timestamp(value.get("checked_at"))
        _check_directory_use(connection, identity, "application", key, value)
        connection.execute(
            "INSERT INTO server_map_objects VALUES (?, 'application', ?, ?) ON CONFLICT(server_id,kind,object_id) DO UPDATE SET data=excluded.data",
            (identity, key, _json(value)),
        )


def seed_references(
    connection: sqlite3.Connection,
    identity: str,
    metadata: dict[str, Any],
    *,
    grandfather: bool = False,
    previous: dict[str, Any] | None = None,
) -> None:
    """Explicit old lists only; no inferred actual use or missing-object removal."""
    before = connection.execute(
        "SELECT COUNT(*) FROM server_map_objects WHERE server_id=?", (identity,)
    ).fetchone()[0]
    previous_sites = {site["site_id"]: site for site in (previous or {}).get("sites", [])}
    sources = [(None, metadata), *[(site["site_id"], site) for site in metadata.get("sites", [])]]
    for application_id, owner in sources:
        source = (
            owner.get("source") or metadata.get("provenance", {}).get("source") or "legacy metadata"
        )
        for field, kind, name_field in (
            ("domains", "domain", "hostname"),
            ("paths", "directory", "path"),
            ("runtime" if application_id else "services", "service", "identifier"),
        ):
            prior_owner = (
                previous_sites.get(application_id, {}) if application_id else (previous or {})
            )
            for text in owner.get(field, []):
                if text in prior_owner.get(field, []):
                    continue
                key = "legacy-" + hashlib.sha256((kind + text).encode()).hexdigest()[:24]
                try:
                    obj = validate_object(
                        kind,
                        {
                            "name": text,
                            name_field: text,
                            "source": source,
                            "basis": "import",
                            "reason": "Документированная прежняя ссылка; актуальное использование не проверено",
                            **({"discovery_sources": [source]} if kind == "domain" else {}),
                        },
                    )
                except ServerError:
                    continue  # exact invalid/non-path reference remains in the archival snapshot/old site
                connection.execute(
                    "INSERT OR IGNORE INTO server_map_objects VALUES (?,?,?,?)",
                    (identity, kind, key, _json(obj)),
                )
                if application_id:
                    link_id = (
                        "legacy-"
                        + hashlib.sha256((application_id + kind + text).encode()).hexdigest()[:24]
                    )
                    value = {
                        "role": "documented",
                        "source": source,
                        "checked_at": None,
                        "basis": "reference",
                        "reason": "Прежняя явная ссылка приложения, использование не проверено",
                    }
                    connection.execute(
                        "INSERT OR IGNORE INTO server_map_links VALUES (?,?, 'application', ?,?,?,?)",
                        (identity, link_id, application_id, kind, key, _json(value)),
                    )
    after = connection.execute(
        "SELECT COUNT(*) FROM server_map_objects WHERE server_id=?", (identity,)
    ).fetchone()[0]
    if not grandfather and after > MAX_OBJECTS and after > before:
        raise ServerError("Server object quota exceeded; legacy write was not truncated")


def object_detail(
    connection: sqlite3.Connection, identity: str, kind: str, key: str
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ServerError("Invalid map collection")
    key = server_id(key)
    row = connection.execute(
        "SELECT data FROM server_map_objects WHERE server_id=? AND kind=? AND object_id=?",
        (identity, kind, key),
    ).fetchone()
    if not row:
        raise ServerError("Inventory object not found")
    return {"kind": kind, "object_id": key, **json.loads(row[0])}


def links_for(
    connection: sqlite3.Connection, identity: str, kind: str, key: str
) -> list[dict[str, Any]]:
    return [
        {
            "link_id": row[0],
            "from_kind": row[1],
            "from_id": row[2],
            "to_kind": row[3],
            "to_id": row[4],
            **json.loads(row[5]),
        }
        for row in connection.execute(
            "SELECT link_id,from_kind,from_id,to_kind,to_id,data FROM server_map_links WHERE server_id=? AND ((from_kind=? AND from_id=?) OR (to_kind=? AND to_id=?)) ORDER BY link_id",
            (identity, kind, key, kind, key),
        )
    ]


def _http_failure_match(text: str) -> re.Match[str] | None:
    """Preserve explicit failures beyond an ambiguous API-root 403/404."""
    return re.search(
        r"\bHTTP\s*(?!403\b|404\b)[45]\d\d\b|\bcurl\s*=\s*[1-9]\d*\b"
        r"|\bTLS\s*=\s*(?:error|expired|untrusted|mismatch)\b"
        r"|\bDNS\s*=\s*(?:error|missing|failed|NXDOMAIN|SERVFAIL)\b",
        text,
        re.IGNORECASE,
    )


def _http_failure(text: str) -> bool:
    return _http_failure_match(text) is not None


def _current_probe_result(obj: dict[str, Any], field: str, hours: int) -> dict[str, Any] | None:
    # A common timestamp is safe only for an undivided legacy observation.
    # Once field provenance exists, another field's update cannot date this result.
    if obj.get("evidence") and not any(e["field"] == field for e in obj["evidence"]):
        return None
    evidence = effective_evidence(obj.get("kind", "domain"), obj).get(field, {})
    checked = evidence.get("checked_at")
    if (
        obj.get(field)
        and evidence.get("basis") == "probe"
        and evidence.get("source")
        and checked
        and 0 <= (datetime.now(UTC) - _date(checked)).total_seconds() <= hours * 3600
    ):
        return evidence
    return None


def _current_origin_failure(obj: dict[str, Any], hours: int) -> dict[str, Any] | None:
    if _http_failure(obj.get("origin_result", "")):
        return _current_probe_result(obj, "origin_result", hours)
    return None


def _probe_description(obj: dict[str, Any], field: str, evidence: dict[str, Any]) -> str:
    def short(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit] + "…"

    response = short(obj[field], 240)
    failure = _http_failure_match(obj[field])
    if failure and failure.group().casefold() not in response.casefold():
        response = failure.group() + "; " + response
    return (
        f"{response} (дата: {evidence['checked_at']}; источник: {short(evidence['source'], 128)})."
    )


def availability_of(obj: dict[str, Any], health_hours: int = 24) -> str:
    """A transport-only API root rejection is not proof of functional failure."""
    if obj.get("effective_availability"):
        return str(obj["effective_availability"])
    value = obj.get("availability", "unknown")
    if value != "error":
        return str(value)
    if _current_origin_failure(obj, health_hours):
        return "error"
    fields = effective_evidence(obj.get("kind", "domain"), obj)
    evidence = fields.get("availability", {})
    checked = evidence.get("checked_at")
    scope = fields.get("probe_scope", {})
    root_only = (
        obj.get("probe_scope") == "transport"
        and evidence.get("basis") == "probe"
        and scope.get("basis") == "probe"
        and checked
        and scope.get("checked_at") == checked
        and scope.get("source") == evidence.get("source")
    ) or (
        obj.get("probe_scope", "unknown") == "unknown"
        and "Корень API не является полной проверкой приложения" in evidence.get("reason", "")
        and evidence.get("basis") == "probe"
    )
    public = fields.get("public_result", {})
    response = obj.get("public_result", "")
    if (
        root_only
        and checked
        and public.get("checked_at") == checked
        and public.get("basis") == "probe"
        and public.get("source") == evidence.get("source")
        and re.match(r"HTTP\s+(?:403|404)\b", response)
        and not _http_failure(response)
        and not _http_failure(evidence.get("reason", ""))
        and obj.get("https") not in {"error", "expired", "untrusted", "mismatch"}
        and obj.get("destination") != "missing"
    ):
        return "unconfirmed"
    return "error"


def status_of(obj: dict[str, Any]) -> str:
    # An unknown business usage must not mask a known technical result.
    if obj.get("observed_status"):
        return str(obj["observed_status"])
    for value in (
        obj.get("state"),
        availability_of(obj),
        obj.get("usage"),
        obj.get("renewal_state"),
        obj.get("https"),
        obj.get("trust"),
    ):
        if value and value != "unknown":
            return str(value)
    return "unknown"


def _dated(obj: dict[str, Any], field: str, hours: int) -> bool:
    evidence = effective_evidence(obj["kind"], obj).get(field, {})
    checked = evidence.get("checked_at")
    return bool(
        checked
        and evidence.get("basis") in {"process", "probe", "configuration", "complete_scan"}
        and 0 <= (datetime.now(UTC) - _date(checked)).total_seconds() <= hours * 3600
    )


class InventoryView:
    """One read snapshot of bounded known objects/links; never changes operator fields."""

    def __init__(self, connection: sqlite3.Connection, identity: str) -> None:
        self.policy = server_policy(connection, identity)
        self.objects = {
            (row[0], row[1]): {"kind": row[0], "object_id": row[1], **json.loads(row[2])}
            for row in connection.execute(
                "SELECT kind,object_id,data FROM server_map_objects WHERE server_id=?", (identity,)
            )
        }
        self.links: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        self.availabilities: dict[tuple[str, str], str] = {}
        self.origin_failures: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for row in connection.execute(
            "SELECT from_kind,from_id,to_kind,to_id,data FROM server_map_links WHERE server_id=? ORDER BY link_id",
            (identity,),
        ):
            a, b = (row[0], row[1]), (row[2], row[3])
            relation = json.loads(row[4])
            self.links.setdefault(a, []).append((relation, self.objects[b]))
            self.links.setdefault(b, []).append((relation, self.objects[a]))

    def related(self, obj: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self.links.get((obj["kind"], obj["object_id"]), [])

    def verified(self, relation: dict[str, Any]) -> bool:
        checked = relation.get("checked_at")
        return bool(
            relation["basis"] in {"configuration", "process"}
            and checked
            and 0
            <= (datetime.now(UTC) - _date(checked)).total_seconds()
            <= self.policy["inventory_hours"] * 3600
        )

    def running(self, obj: dict[str, Any]) -> bool:
        if obj["kind"] == "service":
            return obj["state"] in {"running", "scheduled"} and _dated(
                obj, "state", self.policy["health_hours"]
            )
        if obj["kind"] == "application":
            return any(
                self.verified(r) and other["kind"] == "service" and self.running(other)
                for r, other in self.related(obj)
                if r["role"] == "runtime"
            )
        return False

    def availability(self, obj: dict[str, Any]) -> str:
        """Explain legacy inherited API-root results using the same dated probe batch."""
        key = (obj["kind"], obj["object_id"])
        if key in self.availabilities:
            return self.availabilities[key]
        value = availability_of(obj, self.policy["health_hours"])
        self.availabilities[key] = value
        origin = _current_origin_failure(obj, self.policy["health_hours"])
        if value == "error" and origin:
            self.origin_failures[key] = (obj, origin)
            return value
        if value != "error" or obj["kind"] not in {"application", "route"}:
            return value
        fields = effective_evidence(obj["kind"], obj)
        evidence = fields.get("availability", {})
        checked = evidence.get("checked_at")
        if (
            not checked
            or not _dated(obj, "availability", self.policy["health_hours"])
            or obj.get("probe_scope", "unknown") != "unknown"
            or not re.search(r"\bGET\s+/(?:[.;\s]|$)", evidence.get("reason", ""))
            or _http_failure(evidence.get("reason", ""))
            or obj.get("https") in {"error", "expired", "untrusted", "mismatch"}
        ):
            return value
        if obj["kind"] == "route":
            response = obj.get("public_result", "")
            public = fields.get("public_result", {})
            if (
                evidence.get("basis") != "probe"
                or obj.get("path") != "/"
                or public.get("basis") != "probe"
                or public.get("checked_at") != checked
                or public.get("source") != evidence.get("source")
                or not re.match(r"HTTP\s+(?:403|404)\b", response)
                or _http_failure(response)
            ):
                return value
        elif evidence.get("basis") != "process" or not self.running(obj):
            return value
        elif any(
            other["kind"] == "service"
            and relation["role"] == "runtime"
            and self.verified(relation)
            and other.get("state") == "failed"
            and _dated(other, "state", self.policy["health_hours"])
            for relation, other in self.related(obj)
        ):
            return value
        compatible_root = False
        for relation, other in self.related(obj):
            if other["kind"] not in {"domain", "route"} or relation["role"] not in {
                "route",
                "endpoint",
            }:
                continue
            if not (
                relation.get("basis") in {"probe", "configuration", "process"}
                and relation.get("checked_at")
                and 0
                <= (datetime.now(UTC) - _date(relation["checked_at"])).total_seconds()
                <= self.policy["inventory_hours"] * 3600
            ):
                continue
            other_evidence = effective_evidence(other["kind"], other).get("availability", {})
            other_checked = other_evidence.get("checked_at")
            if (
                not other_checked
                or other_evidence.get("basis") != "probe"
                or not _dated(other, "availability", self.policy["health_hours"])
            ):
                continue
            other_value = self.availability(other)
            # A known endpoint/transport failure in a current linked check must
            # not disappear because another endpoint only tested its root.
            if other_value == "error":
                cause = self.origin_failures.get((other["kind"], other["object_id"]))
                if cause:
                    self.origin_failures[key] = cause
                return value
            if (
                other_value == "unconfirmed"
                and other_evidence.get("source") == evidence.get("source")
                and abs((_date(other_checked) - _date(checked)).total_seconds()) <= 60
                and (
                    obj["kind"] != "route"
                    or (
                        other["kind"] == "domain"
                        and other.get("hostname") == obj.get("hostname")
                        and re.match(r"HTTP\s+(403|404)\b", other.get("public_result", ""))
                        and other["public_result"].split(";")[0]
                        == obj["public_result"].split(";")[0]
                    )
                )
            ):
                compatible_root = True
        if compatible_root:
            self.availabilities[key] = "unconfirmed"
        return self.availabilities[key]

    def describe(self, obj: dict[str, Any]) -> dict[str, Any]:
        related = self.related(obj)
        grouped = {
            kind: [(r, other) for r, other in related if other["kind"] == kind]
            for kind in ("application", "domain", "route", "service", "directory")
        }
        # Only short references leave this read boundary; complete links use their existing cursor.
        references: dict[str, dict[str, Any]] = {
            kind: {"total": len(items), "items": [], "omitted": len(items)}
            for kind, items in grouped.items()
        }
        # UTF-8 field limits do not bound escaped ASCII JSON. Share a byte budget
        # across groups, retaining their totals and the independent full-link cursor.
        for index in range(6):
            for kind, items in grouped.items():
                if index >= len(items):
                    continue
                r, other = items[index]
                group = references[kind]
                group["items"].append(
                    {
                        "object_id": other["object_id"],
                        "name": other["name"],
                        "role": r["role"],
                        "verified": self.verified(r),
                    }
                )
                group["omitted"] -= 1
                if len(json.dumps(references, ensure_ascii=True).encode()) > 8 * 1024:
                    group["items"].pop()
                    group["omitted"] += 1
        usage = "unknown"
        reason = ""
        gaps = []
        entry = obj.get("entry_type", "unknown")
        role = obj.get("role", "unknown")
        if obj["kind"] == "directory":
            context = obj.get("context", "")
            if entry == "unknown" and _dated(obj, "context", self.policy["inventory_hours"]):
                if context.endswith(" Каталог."):
                    entry = "directory"
                elif context.endswith(" Файл или отсутствующий путь."):
                    entry = "non_directory_unconfirmed"
            consumers = [
                (r, other)
                for r, other in related
                if other["kind"] in {"application", "service"} and self.verified(r)
            ]
            current = [other for r, other in consumers if self.running(other)]
            usage = "current" if current else "configured" if consumers else "unknown"
            roles = list(dict.fromkeys(r["role"] for r, _ in consumers))
            if role == "unknown" and roles:
                role = ", ".join(roles[:6])
            if current:
                reason = (
                    "Путь связан с текущими запущенными потребителями: "
                    + ", ".join(other["name"] for other in current[:6])
                    + "."
                )
            elif consumers:
                reason = "Есть проверенные ссылки конфигурации; текущий запуск потребителей не подтверждён."
            else:
                reason = (
                    context
                    or "Текущие потребители не установлены. Отсутствие связей не подтверждает ненужность."
                )
            checked = (
                set(obj.get("consumer_checks", []))
                if _dated(obj, "consumer_checks", self.policy["inventory_hours"])
                else set()
            )
            missing = [x for x in CONSUMER_SOURCES if x not in checked]
            if missing:
                gaps.append("Не подтверждён полный поиск потребителей: " + ", ".join(missing) + ".")
            if obj.get("size_bytes") is None:
                gaps.append("Размер не измерен.")
            if role == "unknown":
                gaps.append("Роль пути не установлена.")
            if obj.get("usage") == "leftover" and current:
                gaps.append(
                    "Решение об остатке противоречит текущим потребителям; требуется пересмотр оператором."
                )
            status = "current" if current else obj.get("usage", "unknown")
            if status == "unknown" and consumers:
                status = "configured"
        else:
            availability = self.availability(obj)
            status = status_of({**obj, "effective_availability": availability})
            if obj["kind"] == "application":
                runtimes = [
                    other
                    for relation, other in related
                    if other["kind"] == "service"
                    and relation["role"] == "runtime"
                    and self.verified(relation)
                ]
                usage = "current" if self.running(obj) else "configured" if runtimes else "unknown"
                if status == "unknown" and usage != "unknown":
                    status = "running" if usage == "current" else "configured"
            cause = self.origin_failures.get((obj["kind"], obj["object_id"]))
            if cause:
                point, proof = cause
                own = (point["kind"], point["object_id"]) == (obj["kind"], obj["object_id"])
                reason = (
                    "Внутренняя проверка origin: "
                    if own
                    else "Сбой связанной внутренней проверки origin «" + point["name"][:128] + "»: "
                ) + _probe_description(point, "origin_result", proof)
                public_point = obj if obj.get("public_result") else point
                public = _current_probe_result(
                    public_point, "public_result", self.policy["health_hours"]
                )
                if public:
                    reason += (
                        " Собственная публичная проверка: "
                        if public_point is obj
                        else " Публичная проверка связанной точки: "
                    ) + _probe_description(public_point, "public_result", public)
                if obj.get("probe_scope", "unknown") in {"unknown", "transport"}:
                    reason += " Работоспособность ожидаемого endpoint не проверена."
            elif availability == "unconfirmed":
                reason = "Проверен только транспортный ответ корня API (HTTP 403/404). Работоспособность ожидаемого endpoint не проверена."
            else:
                e = effective_evidence(obj["kind"], obj).get(
                    "state" if obj["kind"] == "service" else "availability", {}
                )
                reason = e.get("reason") or obj.get("reason", "")
            if obj["kind"] in {"application", "domain", "route"}:
                if obj.get("probe_scope", "unknown") in {"unknown", "transport"}:
                    gaps.append(
                        "Ожидаемый адрес и результат функциональной проверки не установлены."
                    )
                if obj["kind"] == "application" and not obj.get("description"):
                    gaps.append("Назначение и схема приложения ещё не описаны оператором.")
        return {
            "observed_status": status,
            "technical_usage": usage,
            "observed_reason": reason,
            "effective_availability": self.availability(obj),
            "entry_type_effective": entry,
            "effective_role": role,
            "related": references,
            "gaps": gaps,
        }

    def enrich(self, obj: dict[str, Any]) -> dict[str, Any]:
        return {**obj, **self.describe(obj)}


def problems(obj: dict[str, Any], policy: dict[str, Any] | None = None) -> list[str]:
    issues = []
    for key in ("availability", "https", "trust", "state", "renewal_state", "deployment_state"):
        value = availability_of(obj) if key == "availability" else obj.get(key)
        if value in {"error", "expired", "untrusted", "mismatch", "failed", "pending"}:
            issues.append(key)
    if obj.get("not_after"):
        days = (_date(obj["not_after"]) - datetime.now(UTC)).total_seconds() / 86400
        if days <= max((policy or DEFAULT_POLICY)["expiry_days"]):
            issues.append("expired" if days <= 0 else "expires_soon")
    if obj.get("kind") == "certificate_use":
        fingerprints = obj.get("observed_fingerprints", [])
        configured = obj.get("configured_fingerprint")
        file_value = obj.get("file_fingerprint")
        if (configured and file_value and configured != file_value) or (
            fingerprints
            and (
                (configured and configured not in fingerprints)
                or (file_value and file_value not in fingerprints)
            )
        ):
            issues.append("different_certificate")
    return issues


def freshness(obj: dict[str, Any], policy: dict[str, Any] | None = None) -> str:
    policy = policy or DEFAULT_POLICY
    operational = {
        "availability",
        "dns",
        "https",
        "observed_fingerprints",
        "renewal_state",
        "deployment_state",
        "state",
    }
    evidence = {item["field"]: item for item in obj.get("evidence", [])}
    states = []
    now = datetime.now(UTC)
    for field in operational & set(obj):
        if obj[field] in (None, "", "unknown", []):
            continue
        checked = (
            evidence[field]["checked_at"]
            if field in evidence
            else obj.get("observed_at")
            if field == "observed_fingerprints"
            else obj.get("checked_at")
        )
        if not checked:
            states.append("unknown")
        else:
            hours = (
                policy["health_hours"]
                if field in {"availability", "dns", "https", "observed_fingerprints", "state"}
                else policy["inventory_hours"]
            )
            states.append(
                "stale" if (now - _date(checked)).total_seconds() > hours * 3600 else "recent"
            )
    if "stale" in states:
        return "stale"
    if "unknown" in states:
        return "unknown"
    if states:
        return "recent"
    checked = obj.get("checked_at")
    if not checked:
        return "unknown"
    return (
        "stale"
        if (now - _date(checked)).total_seconds() > policy["inventory_hours"] * 3600
        else "recent"
    )


def certificate_context(connection: sqlite3.Connection, identity: str, key: str) -> dict[str, Any]:
    versions = []
    applications = []
    uses = []
    for relation in links_for(connection, identity, "certificate", key):
        other_kind, other_key = (
            (relation["to_kind"], relation["to_id"])
            if (relation["from_kind"], relation["from_id"]) == ("certificate", key)
            else (relation["from_kind"], relation["from_id"])
        )
        if other_kind == "certificate_version" and relation["role"] == "current_version":
            versions.append(object_detail(connection, identity, other_kind, other_key))
        elif other_kind == "application":
            applications.append(object_detail(connection, identity, other_kind, other_key)["name"])
        elif other_kind == "certificate_use":
            uses.append(object_detail(connection, identity, other_kind, other_key))
    known_dates = [item["not_after"] for item in versions if item["not_after"]]
    return {
        "current_versions": versions,
        "applications": applications,
        "uses": uses,
        "not_after": min(known_dates, key=_date) if known_dates else None,
    }


def collection_rows(
    connection: sqlite3.Connection,
    identity: str,
    kind: str,
    *,
    query: str = "",
    status: str = "",
    application_id: str = "",
    sort: str = "name",
    descending: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    if (
        kind not in KINDS
        or sort not in {"name", "status", "environment", "size_bytes", "not_after", "checked_at"}
        or type(descending) is not bool
    ):
        raise ServerError("Invalid collection filter or sort")
    query = safe_text(query, 256).casefold()
    status = safe_text(status, 64)
    policy = server_policy(connection, identity)
    view = InventoryView(connection, identity)
    rows = [view.enrich(obj) for (obj_kind, _), obj in view.objects.items() if obj_kind == kind]
    total = len(rows)
    if kind == "certificate":
        for obj in rows:
            obj["not_after"] = certificate_context(connection, identity, obj["object_id"])[
                "not_after"
            ]
    linked_ids = None
    if application_id:
        object_detail(connection, identity, "application", application_id)
        linked_ids = {
            link["to_id"]
            if link["from_kind"] == "application" and link["from_id"] == application_id
            else link["from_id"]
            for link in links_for(connection, identity, "application", application_id)
            if link["to_kind"] == kind or link["from_kind"] == kind
        }

    def contains(value: Any) -> bool:
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return value is not None and query in str(value).casefold()

    def matches(obj: dict[str, Any]) -> bool:
        if not query or contains(obj):
            return True
        for relation in links_for(connection, identity, kind, obj["object_id"]):
            other_kind, other_id = (
                (relation["to_kind"], relation["to_id"])
                if (relation["from_kind"], relation["from_id"]) == (kind, obj["object_id"])
                else (relation["from_kind"], relation["from_id"])
            )
            other = object_detail(connection, identity, other_kind, other_id)
            if any(
                query in str(other.get(field, "")).casefold()
                for field in ("name", "hostname", "path", "identifier")
            ):
                return True
        return False

    rows = [
        obj
        for obj in rows
        if matches(obj)
        and (
            not status
            or (status == "attention" and problems(obj, policy))
            or (status == "stale" and freshness(obj, policy) == "stale")
            or (
                status == "incomplete"
                and (obj.get("gaps") or status_of(obj) == "unknown" or not obj.get("checked_at"))
            )
            or status_of(obj) == status
            or (status in {"current", "configured"} and obj.get("technical_usage") == status)
            or (status != "unknown" and obj.get("entry_type_effective") == status)
            or (
                status != "unknown"
                and any(
                    (availability_of(obj) if field == "availability" else obj.get(field)) == status
                    for field, field_type in FIELDS[kind].items()
                    if field_type in ENUMS and not (kind == "directory" and field == "usage")
                )
            )
        )
        and (linked_ids is None or obj["object_id"] in linked_ids)
    ]

    # Unknown sort values remain last in either direction; ID gives stable ties.
    def sort_key(obj: dict[str, Any]) -> tuple[Any, str]:
        value = status_of(obj) if sort == "status" else obj.get(sort)
        if sort == "size_bytes":
            return (value if value is not None else 0, obj["object_id"])
        if sort in {"not_after", "checked_at"} and value:
            return (_date(value), obj["object_id"])
        return (str(value or "").casefold(), obj["object_id"])

    known = [
        obj
        for obj in rows
        if (status_of(obj) if sort == "status" else obj.get(sort)) not in (None, "", "unknown")
    ]
    unknown = [obj for obj in rows if obj not in known]
    known.sort(key=sort_key, reverse=descending)
    unknown.sort(key=lambda obj: obj["object_id"])
    return known + unknown, total


def map_contract() -> dict[str, Any]:
    return {
        "kinds": list(KINDS),
        "common_fields": sorted(COMMON - {"evidence"}),
        "object_fields": FIELDS,
        "enums": ENUMS,
        "basis": BASES,
        "evidence": {"fields": ["field", "source", "checked_at", "basis", "reason"], "maximum": 32},
        "links": {
            "fields": [
                "link_id",
                "from_kind",
                "from_id",
                "to_kind",
                "to_id",
                "role",
                "source",
                "checked_at",
                "basis",
                "reason",
            ],
            "current_version_role": "current_version",
        },
        "observations": {
            "fields": ["kind", "object_id", "values", "source", "checked_at", "basis", "reason"],
            "operator_fields_not_writable": sorted(OPERATOR_FIELDS),
            "portion_max": 25,
        },
        "read_derived": {
            "fields": [
                "observed_status",
                "technical_usage",
                "observed_reason",
                "effective_availability",
                "entry_type_effective",
                "effective_role",
                "related",
                "gaps",
            ],
            "technical_usage": ["unknown", "current", "configured"],
            "consumer_sources": list(CONSUMER_SOURCES),
            "related_maximum_per_kind": 6,
            "note": "Read-only derivation preserves operator usage and stored evidence; complete relations use links pagination.",
        },
        "coverage": {
            "fields": ["area", "state", "source", "reason"],
            "states": ["complete", "partial", "failed", "not_checked"],
        },
        "limits": {
            "objects_per_server": MAX_OBJECTS,
            "links_per_server": 20000,
            "object_bytes_ascii_json": MAX_OBJECT_BYTES,
            "list_items": 64,
            "application_legacy_list_items": 16,
            "check_portions": 400,
            "checks_per_server": 1000,
            "public_response_bytes": MAX_RESPONSE_BYTES,
        },
        "operations": {
            "upsert": ["action", "kind", "object_id", "data"],
            "delete": ["action", "kind", "object_id"],
            "link": ["action", "link"],
            "unlink": ["action", "link_id"],
            "check_start": ["action", "check_id", "coverage", "source", "started_at"],
            "check_append": ["action", "check_id", "batch", "observations"],
            "check_publish": ["action", "check_id", "coverage", "finished_at"],
            "check_fail": ["action", "check_id", "reason"],
        },
        "policy": DEFAULT_POLICY,
        "rules": [
            "Directory leftover requires operator basis, former application, decision owner/date and checked links date; active application links prohibit leftover",
            "Verified renewal/deployment requires dated probe evidence for the corresponding state field",
            "Observed TLS fingerprints require address, SNI, port and observed_at",
            "Staged check writes increment server revision; use returned revision. Missing observations never remove records",
            "Observed name creates a new record only; existing operator name is preserved",
        ],
    }


def read_map(
    connection: sqlite3.Connection,
    identity: str,
    *,
    collection: str,
    object_id: str | None = None,
    query: str = "",
    status: str = "",
    application_id: str = "",
    sort: str = "name",
    descending: bool = False,
    offset: int = 0,
    limit: int = 50,
    expected_revision: int | None = None,
    links_offset: int = 0,
) -> dict[str, Any]:
    identity = server_id(identity)
    if (
        type(offset) is not int
        or not 0 <= offset <= 2**31 - 1
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(links_offset) is not int
        or not 0 <= links_offset <= 2 * MAX_OBJECTS
    ):
        raise ServerError("Invalid inventory page")
    connection.execute("SAVEPOINT map_read")
    try:
        row = connection.execute("SELECT revision FROM servers WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ServerError("Server not found")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != row[0]
        ):
            raise ServerConflictError("Server revision changed; restart page traversal")
        policy = server_policy(connection, identity)
        if collection == "schema":
            return {
                "scope": "local_user",
                "server_id": identity,
                "revision": row[0],
                "collection": "schema",
                "schema": map_contract(),
            }
        if collection == "checks":
            rows = [
                {"check_id": x[0], "state": x[1], "created_at": x[2], **json.loads(x[3])}
                for x in connection.execute(
                    "SELECT check_id,state,created_at,data FROM server_map_checks WHERE server_id=? ORDER BY created_at DESC,check_id",
                    (identity,),
                )
            ]
            total = len(rows)
        else:
            rows, total = collection_rows(
                connection,
                identity,
                collection,
                query=query,
                status=status,
                application_id=application_id,
                sort=sort,
                descending=descending,
            )
        result: dict[str, Any] = {
            "policy": policy,
            "scope": "local_user",
            "server_id": identity,
            "revision": row[0],
            "collection": collection,
            "total": total,
            "matched": len(rows),
            "offset": offset,
            "rows": [],
            "next_offset": None,
            "complete": False,
        }
        if object_id:
            result["object"] = object_detail(connection, identity, collection, object_id)
            result["understanding"] = InventoryView(connection, identity).describe(result["object"])
            links = links_for(connection, identity, collection, object_id)
            result["links"] = links[links_offset : links_offset + 50]
            result["links_total"] = len(links)
            result["links_offset"] = links_offset
            result["links_next_offset"] = (
                links_offset + len(result["links"])
                if links_offset + len(result["links"]) < len(links)
                else None
            )
        else:
            for obj in rows[offset : offset + limit]:
                compact = {
                    key: obj.get(key)
                    for key in (
                        "object_id",
                        "kind",
                        "name",
                        "purpose",
                        "environment",
                        "hostname",
                        "path",
                        "role",
                        "usage",
                        "availability",
                        "https",
                        "size_bytes",
                        "state",
                        "not_after",
                        "renewal",
                        "deployment",
                        "owner",
                        "checked_at",
                        "reason",
                        "observed_status",
                        "technical_usage",
                        "observed_reason",
                        "effective_availability",
                        "entry_type_effective",
                        "effective_role",
                    )
                    if key in obj
                }
                compact["status"] = status_of(obj) if collection != "checks" else obj["state"]
                compact["attention"] = problems(obj, policy)
                compact["freshness"] = freshness(obj, policy)
                if collection == "checks":
                    compact = obj
                result["rows"].append(compact)
                if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_RESPONSE_BYTES - 1024:
                    result["rows"].pop()
                    break
            if rows[offset:] and not result["rows"]:
                raise ServerError("Inventory row exceeds response budget")
            result["next_offset"] = (
                offset + len(result["rows"]) if offset + len(result["rows"]) < len(rows) else None
            )
            result["complete"] = result["next_offset"] is None
        while len(
            json.dumps(result, ensure_ascii=True).encode()
        ) > MAX_RESPONSE_BYTES and result.get("links"):
            result["links"].pop()
            result["links_next_offset"] = links_offset + len(result["links"])
        if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_RESPONSE_BYTES:
            raise ServerError("Inventory detail exceeds response budget")
        return result
    finally:
        connection.execute("RELEASE SAVEPOINT map_read")


def validate_link(value: object) -> dict[str, Any]:
    allowed = {
        "link_id",
        "from_kind",
        "from_id",
        "to_kind",
        "to_id",
        "role",
        "source",
        "checked_at",
        "basis",
        "reason",
    }
    if (
        not isinstance(value, dict)
        or set(value) != allowed
        or value["from_kind"] not in KINDS
        or value["to_kind"] not in KINDS
    ):
        raise ServerError("Link requires explicit typed endpoints and evidence")
    result: dict[str, Any] = {k: server_id(value[k]) for k in ("link_id", "from_id", "to_id")}
    result.update({k: value[k] for k in ("from_kind", "to_kind")})
    result.update(
        {
            k: safe_text(value[k], 512 if k == "reason" else 256)
            for k in ("role", "source", "basis", "reason")
        }
    )
    result["checked_at"] = _map_timestamp(value["checked_at"])
    if not result["role"] or not result["source"] or result["basis"] not in BASES:
        raise ServerError("Link role, source and basis are required")
    return result


def _put_object(
    connection: sqlite3.Connection,
    identity: str,
    kind: str,
    key: str,
    data: dict[str, Any],
    *,
    preserve_decision: bool = False,
) -> None:
    exists = connection.execute(
        "SELECT 1 FROM server_map_objects WHERE server_id=? AND kind=? AND object_id=?",
        (identity, kind, key),
    ).fetchone()
    if (
        not exists
        and connection.execute(
            "SELECT COUNT(*) FROM server_map_objects WHERE server_id=?", (identity,)
        ).fetchone()[0]
        >= MAX_OBJECTS
    ):
        raise ServerError("Server inventory quota is 10000 objects; nothing was truncated")
    _check_directory_use(connection, identity, kind, key, data, preserve_decision=preserve_decision)
    if kind == "application":
        from harness.servers import SITE_FIELDS, validate_site

        validate_site({"site_id": key, **{k: v for k, v in data.items() if k in SITE_FIELDS}})
    if kind == "application" and data.get("profile_id"):
        raw = connection.execute("SELECT data FROM servers WHERE id=?", (identity,)).fetchone()[0]
        if data["profile_id"] not in {
            item["profile_id"] for item in json.loads(raw).get("platform_profiles", [])
        }:
            raise ServerError("Preparation profile must exist on the same server")
    connection.execute(
        "INSERT INTO server_map_objects VALUES (?,?,?,?) ON CONFLICT(server_id,kind,object_id) DO UPDATE SET data=excluded.data",
        (identity, kind, key, _json(data)),
    )


def _check_directory_use(
    connection: sqlite3.Connection,
    identity: str,
    kind: str,
    key: str,
    data: dict[str, Any],
    *,
    preserve_decision: bool = False,
) -> None:
    if kind != "directory" or data.get("usage") != "leftover":
        return
    if preserve_decision:
        prior = object_detail(connection, identity, kind, key)
        if prior.get("usage") == "leftover":
            return  # Observations cannot reconfirm or replace the operator decision.
    if kind == "directory" and data.get("usage") == "leftover":
        view = InventoryView(connection, identity)
        obj = {"kind": kind, "object_id": key, **data}
        if view.describe(obj)["technical_usage"] == "current":
            raise ServerError(
                "An active application uses this directory; revise its classification"
            )
    if (kind, data.get("usage")) in {("directory", "leftover"), ("application", "active")}:
        for relation in links_for(connection, identity, kind, key):
            other_kind, other_key = (
                (relation["to_kind"], relation["to_id"])
                if (relation["from_kind"], relation["from_id"]) == (kind, key)
                else (relation["from_kind"], relation["from_id"])
            )
            expected_kind, expected_usage = (
                ("application", "active") if kind == "directory" else ("directory", "leftover")
            )
            if (
                other_kind == expected_kind
                and relation["basis"] != "reference"
                and (
                    object_detail(connection, identity, other_kind, other_key).get("usage")
                    == expected_usage
                )
            ):
                raise ServerError(
                    "An active application uses this directory; revise its classification"
                )


def _put_link(connection: sqlite3.Connection, identity: str, data: dict[str, Any]) -> None:
    for prefix in ("from", "to"):
        kind = data[prefix + "_kind"]
        object_detail(connection, identity, kind, data[prefix + "_id"])
    key = data["link_id"]
    if (
        not connection.execute(
            "SELECT 1 FROM server_map_links WHERE server_id=? AND link_id=?", (identity, key)
        ).fetchone()
        and connection.execute(
            "SELECT COUNT(*) FROM server_map_links WHERE server_id=?", (identity,)
        ).fetchone()[0]
        >= MAX_LINKS
    ):
        raise ServerError("Server link quota is 20000; nothing was truncated")
    value = {
        k: v
        for k, v in data.items()
        if k not in {"link_id", "from_kind", "from_id", "to_kind", "to_id"}
    }
    connection.execute(
        "INSERT INTO server_map_links VALUES (?,?,?,?,?,?,?) ON CONFLICT(server_id,link_id) DO UPDATE SET from_kind=excluded.from_kind,from_id=excluded.from_id,to_kind=excluded.to_kind,to_id=excluded.to_id,data=excluded.data",
        (
            identity,
            key,
            data["from_kind"],
            data["from_id"],
            data["to_kind"],
            data["to_id"],
            _json(value),
        ),
    )


def _observations(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 25:
        raise ServerError("A check portion requires 1..25 typed observations")
    result = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"kind", "object_id", "values", "source", "checked_at", "basis", "reason"}
            or item["kind"] not in KINDS
            or not isinstance(item["values"], dict)
        ):
            raise ServerError("Invalid typed observation")
        kind = item["kind"]
        values = item["values"]
        if (
            set(values) - ({"name"} | set(FIELDS[kind]))
            or set(values) & OPERATOR_FIELDS
            or not values
        ):
            raise ServerError("Observations cannot overwrite operator intent or unknown fields")
        # Validate only provided values through the full type validator; required identity fields are explicit.
        probe = {
            "name": values.get("name", "Observation"),
            **values,
            "source": item["source"],
            "checked_at": item["checked_at"],
            "basis": item["basis"],
            "reason": item["reason"],
        }
        if kind == "directory" and "path" not in values:
            probe["path"] = "/unknown"
        probe["evidence"] = [
            {"field": field, **{k: probe[k] for k in ("source", "checked_at", "basis", "reason")}}
            for field in values
            if field in FIELDS[kind]
        ]
        normalized = validate_object(kind, probe)
        if not normalized["checked_at"] or normalized["basis"] == "unknown":
            raise ServerError("Observations require dated evidence and a known basis")
        result.append(
            {
                "kind": kind,
                "object_id": server_id(item["object_id"]),
                "values": {k: normalized[k] for k in values},
                **{k: normalized[k] for k in ("source", "checked_at", "basis", "reason")},
            }
        )
    return result


def mutate_map(
    connection: sqlite3.Connection, identity: str, expected_revision: int, operation: object
) -> dict[str, Any]:
    identity = server_id(identity)
    if (
        not isinstance(operation, dict)
        or type(expected_revision) is not int
        or expected_revision < 1
    ):
        raise ServerError("Map mutation requires a typed operation and explicit revision")
    action = operation.get("action")
    contracts = {
        "upsert": {"action", "kind", "object_id", "data"},
        "delete": {"action", "kind", "object_id"},
        "link": {"action", "link"},
        "unlink": {"action", "link_id"},
        "check_start": {"action", "check_id", "coverage", "source", "started_at"},
        "check_append": {"action", "check_id", "batch", "observations"},
        "check_publish": {"action", "check_id", "coverage", "finished_at"},
        "check_fail": {"action", "check_id", "reason"},
    }
    if (
        not isinstance(action, str)
        or action not in contracts
        or set(operation) != contracts[action]
    ):
        raise ServerError("Invalid inventory operation fields")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT revision FROM servers WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ServerError("Server not found")
        if row[0] != expected_revision:
            raise ServerConflictError("Server revision changed; fetch and reconcile before retry")
        now = _now()
        bumps = True
        if action == "upsert":
            kind = operation["kind"]
            key = server_id(operation["object_id"])
            data = validate_object(kind, operation["data"])
            _put_object(connection, identity, kind, key, data)
        elif action == "delete":
            kind, key = operation["kind"], server_id(operation["object_id"])
            object_detail(connection, identity, kind, key)
            if links_for(connection, identity, kind, key):
                raise ServerError("Object has links; remove its links explicitly before deleting")
            connection.execute(
                "DELETE FROM server_map_objects WHERE server_id=? AND kind=? AND object_id=?",
                (identity, kind, key),
            )
        elif action == "link":
            _put_link(connection, identity, validate_link(operation["link"]))
        elif action == "unlink":
            cursor = connection.execute(
                "DELETE FROM server_map_links WHERE server_id=? AND link_id=?",
                (identity, server_id(operation["link_id"])),
            )
            if not cursor.rowcount:
                raise ServerError("Link no longer exists")
        else:
            key = server_id(operation["check_id"])
            if action == "check_start":
                coverage = _coverage(operation["coverage"])
                data = {
                    "coverage": coverage,
                    "source": safe_text(operation["source"], 256),
                    "started_at": _map_timestamp(operation["started_at"]),
                    "finished_at": None,
                    "published_revision": None,
                }
                if not data["source"] or not data["started_at"]:
                    raise ServerError("Check source and start time are required")
                if (
                    connection.execute(
                        "SELECT COUNT(*) FROM server_map_checks WHERE server_id=?", (identity,)
                    ).fetchone()[0]
                    >= 1000
                ):
                    raise ServerError(
                        "Check history quota is 1000; archive/export requires an explicit operator action"
                    )
                connection.execute(
                    "INSERT INTO server_map_checks VALUES (?,?,?,'draft',?,?)",
                    (identity, key, expected_revision + 1, _json(data), now),
                )
            else:
                check = connection.execute(
                    "SELECT expected_revision,state,data FROM server_map_checks WHERE server_id=? AND check_id=?",
                    (identity, key),
                ).fetchone()
                if not check:
                    raise ServerError("Check not found")
                data = json.loads(check[2])
                if action == "check_publish" and check[1] == "published":
                    connection.execute("ROLLBACK")
                    return {
                        "scope": "local_user",
                        "server_id": identity,
                        "revision": expected_revision,
                        "check_id": key,
                        "published_revision": data["published_revision"],
                        "already_published": True,
                    }
                if check[1] != "draft" or check[0] != expected_revision:
                    raise ServerConflictError(
                        "Check is no longer a draft at this revision; reconcile without automatic rebasing"
                    )
                if action == "check_append":
                    batch = operation["batch"]
                    if type(batch) is not int or not 0 <= batch < 400:
                        raise ServerError("Check portions must be numbered 0..399")
                    observations = _observations(operation["observations"])
                    allowed_areas = {
                        area["area"]
                        for area in data["coverage"]
                        if area["state"] in {"complete", "partial"}
                    }
                    if any(
                        observation["kind"] not in allowed_areas for observation in observations
                    ):
                        raise ServerError("Observation is outside the declared check coverage")
                    digest = hashlib.sha256(_json(observations).encode()).hexdigest()
                    previous = connection.execute(
                        "SELECT digest FROM server_map_batches WHERE server_id=? AND check_id=? AND batch=?",
                        (identity, key, batch),
                    ).fetchone()
                    if previous and previous[0] != digest:
                        raise ServerError("The same check portion cannot have different content")
                    if previous:
                        connection.execute("COMMIT")
                        return {
                            "scope": "local_user",
                            "server_id": identity,
                            "revision": expected_revision,
                            "map_action": action,
                            "check_id": key,
                            "already_appended": True,
                        }
                    if not previous:
                        connection.execute(
                            "INSERT INTO server_map_batches VALUES (?,?,?,?,?)",
                            (identity, key, batch, digest, _json(observations)),
                        )
                    connection.execute(
                        "UPDATE server_map_checks SET expected_revision=? WHERE server_id=? AND check_id=?",
                        (expected_revision + 1, identity, key),
                    )
                elif action == "check_fail":
                    data["failure"] = safe_text(operation["reason"], 512)
                    connection.execute(
                        "UPDATE server_map_checks SET state='failed',data=? WHERE server_id=? AND check_id=?",
                        (_json(data), identity, key),
                    )
                else:
                    data["coverage"] = _coverage(operation["coverage"])
                    data["finished_at"] = _map_timestamp(operation["finished_at"])
                    if not data["finished_at"] or _date(data["finished_at"]) < _date(
                        data["started_at"]
                    ):
                        raise ServerError("Check finish must follow start")
                    batches = connection.execute(
                        "SELECT batch,data FROM server_map_batches WHERE server_id=? AND check_id=? ORDER BY batch",
                        (identity, key),
                    ).fetchall()
                    if not batches or [x[0] for x in batches] != list(range(len(batches))):
                        raise ServerError(
                            "Check portions must be contiguous from zero before publication"
                        )
                    allowed_areas = {
                        area["area"]
                        for area in data["coverage"]
                        if area["state"] in {"complete", "partial"}
                    }
                    for _, raw in batches:
                        for observation in json.loads(raw):
                            if observation["kind"] not in allowed_areas:
                                raise ServerError(
                                    "Published observations must belong to completed or partial coverage"
                                )
                            kind, object_key = observation["kind"], observation["object_id"]
                            try:
                                obj = object_detail(connection, identity, kind, object_key)
                                obj.pop("object_id")
                                obj.pop("kind")
                                existing_object = True
                            except ServerError:
                                existing_object = False
                                obj = {
                                    "name": observation["values"].get("name", object_key),
                                    "source": observation["source"],
                                    "basis": observation["basis"],
                                }
                            evidence = effective_evidence(kind, obj)
                            for field, value in observation["values"].items():
                                if field == "name" and existing_object:
                                    continue
                                if field == "discovery_sources":
                                    value = list(dict.fromkeys(obj.get(field, []) + value))
                                prior = evidence.get(field)
                                if (
                                    prior
                                    and prior["checked_at"]
                                    and _date(prior["checked_at"])
                                    > _date(observation["checked_at"])
                                ):
                                    continue
                                obj[field] = value
                                if field in FIELDS[kind]:
                                    evidence[field] = {
                                        "field": field,
                                        **{
                                            k: observation[k]
                                            for k in ("source", "checked_at", "basis", "reason")
                                        },
                                    }
                            obj["evidence"] = list(evidence.values())
                            obj["checked_at"] = max(
                                [e["checked_at"] for e in evidence.values() if e["checked_at"]],
                                key=_date,
                                default=observation["checked_at"],
                            )
                            _put_object(
                                connection,
                                identity,
                                kind,
                                object_key,
                                validate_object(kind, obj),
                                preserve_decision=existing_object,
                            )
                    data["published_revision"] = expected_revision + 1
                    connection.execute(
                        "UPDATE server_map_checks SET state='published',data=? WHERE server_id=? AND check_id=?",
                        (_json(data), identity, key),
                    )
        if bumps:
            connection.execute(
                "UPDATE servers SET revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (now, identity, expected_revision),
            )
            connection.execute(
                "INSERT INTO server_events(server_id,revision,kind,data,recorded_at) VALUES (?,?, 'updated',?,?)",
                (
                    identity,
                    expected_revision + 1,
                    _json({"summary": "Карта размещения обновлена", "map_action": action}),
                    now,
                ),
            )
        connection.execute("COMMIT")
        return {
            "scope": "local_user",
            "server_id": identity,
            "revision": expected_revision + int(bumps),
            "map_action": action,
            "check_id": operation.get("check_id"),
            "object_id": operation.get("object_id"),
        }
    except sqlite3.IntegrityError as exc:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise ServerError("Inventory identity/link conflict; nothing was saved") from exc
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _coverage(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ServerError("Check coverage requires 1..8 areas")
    result = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"area", "state", "source", "reason"}
            or item["area"] not in KINDS
            or item["state"] not in {"complete", "partial", "failed", "not_checked"}
        ):
            raise ServerError("Invalid typed check coverage")
        result.append({k: safe_text(item[k], 512 if k == "reason" else 256) for k in item})
    if len({x["area"] for x in result}) != len(result):
        raise ServerError("Check areas must be unique")
    return result


DEFAULT_POLICY: dict[str, Any] = {
    "health_hours": 24,
    "inventory_hours": 168,
    "passport_days": 30,
    "expiry_days": [30, 14, 7],
}


def validate_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise ServerError(
            "Map policy requires health_hours, inventory_hours, passport_days and expiry_days"
        )
    for field in ("health_hours", "inventory_hours", "passport_days"):
        if type(value[field]) is not int or not 1 <= value[field] <= 8760:
            raise ServerError("Freshness intervals must be positive integers up to 8760")
    days = value["expiry_days"]
    if (
        not isinstance(days, list)
        or not 1 <= len(days) <= 3
        or any(type(day) is not int or not 1 <= day <= 366 for day in days)
        or len(set(days)) != len(days)
    ):
        raise ServerError("Certificate warnings require 1..3 distinct days in 1..366")
    return {**value, "expiry_days": sorted(days, reverse=True)}


def server_policy(connection: sqlite3.Connection, identity: str) -> dict[str, Any]:
    row = connection.execute("SELECT data FROM servers WHERE id=?", (identity,)).fetchone()
    if not row:
        raise ServerError("Server not found")
    return {**DEFAULT_POLICY, **json.loads(row[0]).get("map_policy", {})}
