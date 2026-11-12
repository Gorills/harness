"""Conservative import of an index's explicit connection records; no raw card embedding."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.servers import ServerError, safe_text, server_id, validate_server

_TABLE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+)\|\s*`(ssh [^`]+)`\s*\|\s*$")
_HOST = re.compile(r"[A-Za-z0-9_.:-]+\Z")
_DOMAIN = re.compile(r"(?:\*\.)?[\w-]+(?:\.[\w-]+)+\Z")


def preview_import(root: Path) -> list[dict[str, Any]]:
    root = root.resolve(strict=True)
    path = root / ".cursor/rules/vps-hosts.mdc"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ServerError("Import requires a bounded regular vps-hosts.mdc index")
    text = path.read_text(encoding="utf-8")
    records: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC).isoformat()
    for line in text.splitlines():
        match = _TABLE.fullmatch(line)
        if match is None:
            continue
        source_id, name, command = match.groups()
        server_id(source_id)
        safe_text(name, 160)
        words = shlex.split(command)
        connection: dict[str, Any] = {}
        address = ""
        index = 1
        while index < len(words):
            word = words[index]
            if word == "-p" and index + 1 < len(words):
                try:
                    connection["port"] = int(words[index + 1])
                except ValueError as exc:
                    raise ServerError("Invalid imported SSH metadata") from exc
                index += 2
            elif "@" in word and not address:
                user, address = word.split("@", 1)
                if not _HOST.fullmatch(user) or not _HOST.fullmatch(address):
                    raise ServerError("Invalid imported SSH metadata")
                connection["user"] = user
                index += 1
            else:
                raise ServerError("Import accepts only user@host and optional -p metadata")
        if not address or source_id in records:
            raise ServerError("Duplicate or incomplete source server identity")
        # Same named connection remains distinct from a shared physical host until verified.
        source_key = "server-monitoring:" + source_id
        identity = "srv-" + hashlib.sha256(source_key.encode()).hexdigest()[:24]
        records[source_id] = {
            "server_id": identity,
            "source_key": source_key,
            "metadata": {
                "name": name.strip(),
                "aliases": [source_id],
                "role": name.strip(),
                "environment": "test" if "test" in source_id or "test" in name.lower() else "",
                "addresses": [address],
                "connection": connection,
                "provenance": {
                    "source": "server-monitoring/.cursor/rules/vps-hosts.mdc#" + source_id,
                    "imported_at": now,
                    "checked_at": None,
                },
                "rules": [
                    "Imported connection metadata is unverified; verify connected host and service before authorized mutation."
                ],
            },
        }
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = line.strip("|").split("|")
        if len(cells) != 2:
            continue
        ids = re.findall(r"`([^`]+)`", cells[1])
        if len(ids) != 1 or ids[0] not in records:
            continue
        domains = [
            domain for domain in re.findall(r"`([^`]+)`", cells[0]) if _DOMAIN.fullmatch(domain)
        ]
        metadata = records[ids[0]]["metadata"]
        metadata["domains"] = list(dict.fromkeys([*metadata.get("domains", []), *domains]))
    if not records or len(records) > 100:
        raise ServerError("Index has no safe records or exceeds import limit")
    for record in records.values():
        record["metadata"] = validate_server(record["metadata"])
    return list(records.values())


def enrich_selected_card(
    root: Path, record: dict[str, Any], source_id: str, *, sites_manifest: Path | None = None
) -> dict[str, Any]:
    """Read exactly the chosen card; extract references from allowlisted operational sections."""
    server_id(source_id)
    path = root.resolve(strict=True) / f".cursor/rules/host-{source_id}.mdc"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ServerError("Selected source card is missing or unsafe")
    text = path.read_text(encoding="utf-8")
    # Free-form text is deliberately never copied. Only known service tokens and absolute
    # operational paths from these headings become references; no credentials/commands/env/dump.
    allowed = {
        "фронтенд",
        "основной backend и фоновые задачи",
        "дополнительная cms strapi",
        "домены, tls и nginx",
        "ресурсы, логи и автозапуск",
        "сервисы",
        "services",
        "важные пути",
        "paths",
    }
    section = ""
    services: set[str] = set()
    paths: set[str] = set()
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().casefold()
            continue
        if section not in allowed:
            continue
        # Entire sensitive lines are skipped before token extraction, including fences/commands.
        if re.search(
            r"password|passwd|secret|token|api[_-]?key|парол|токен|private|BEGIN|\.env|dump\.sql",
            line,
            re.I,
        ):
            continue
        for service in (
            "nginx",
            "apache",
            "django",
            "strapi",
            "postgresql",
            "mysql",
            "redis",
            "celery",
            "gunicorn",
            "uvicorn",
            "docker",
            "pm2",
        ):
            if re.search(r"\b" + service + r"\b", line, re.I):
                services.add(service)
        for value in re.findall(r"`(/[^`\s]+)`", line):
            if re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", value) and not re.search(
                r"(?:^|/)(?:\.ssh|\.aws|\.env|id_rsa|id_ed25519|.*\.sql)(?:/|$)", value
            ):
                paths.add(value)
    metadata = {
        **record["metadata"],
        "services": sorted(services),
        "paths": sorted(paths),
        "provenance": {
            **record["metadata"]["provenance"],
            "source": f"server-monitoring/.cursor/rules/host-{source_id}.mdc (allowlisted references; unverified)",
        },
    }
    metadata["rules"] = [
        *metadata["rules"],
        "Audit is read-only: no temporary files, installs, cron/config/service changes or notifications without direct request.",
        "Database-affecting work requires the current verified service dump procedure; imported backup text is not a verified procedure.",
    ]
    if sites_manifest is not None:
        if (
            sites_manifest.is_symlink()
            or not sites_manifest.is_file()
            or sites_manifest.stat().st_size > 512 * 1024
        ):
            raise ServerError("Sites manifest must be a bounded regular JSON file")
        try:
            manifest = json.loads(sites_manifest.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ServerError("Sites manifest must contain valid JSON") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) - {"references"}
            != {
                "source_id",
                "card_sha256",
                "index_sha256",
                "sites",
                "platform_profiles",
                "omissions",
            }
            or manifest["source_id"] != source_id
            or manifest["card_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()
            or manifest["index_sha256"]
            != hashlib.sha256((root / ".cursor/rules/vps-hosts.mdc").read_bytes()).hexdigest()
        ):
            raise ServerError(
                "Sites manifest must match the explicitly selected source card and snapshot"
            )
        references = manifest.get("references", [])
        if not isinstance(references, list) or len(references) > 4:
            raise ServerError("Invalid reviewed source references")
        source_lines = {
            f"server-monitoring/.cursor/rules/host-{source_id}.mdc": len(text.splitlines()),
            "server-monitoring/.cursor/rules/vps-hosts.mdc": len(
                (root / ".cursor/rules/vps-hosts.mdc").read_text().splitlines()
            ),
        }
        source_root = root.resolve(strict=True)
        # Only files explicitly linked by this selected card can supply reviewed evidence.
        linked = set()
        for link in re.findall(r"\]\(([^\s)]+)\)", text):
            if ":" in link or link.startswith("/"):
                continue
            candidate = (path.parent / link.split("#", 1)[0]).resolve()
            if candidate.is_relative_to(source_root):
                linked.add(candidate)
        for reference in references:
            if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
                raise ServerError("Reviewed reference requires path and hash")
            relative = reference["path"]
            if (
                not isinstance(relative, str)
                or not re.fullmatch(r"[A-Za-z0-9_/-]+\.(?:md|mdc)", relative)
                or re.search(r"(?:vault|secret|credential|password|private)", relative, re.I)
                or any(part in {"", ".", ".."} for part in relative.split("/"))
            ):
                raise ServerError("Invalid reviewed reference path")
            target = source_root / relative
            if (
                target.resolve() not in linked
                or any(
                    parent.is_symlink()
                    for parent in [target, *target.parents]
                    if parent.is_relative_to(source_root)
                )
                or not target.is_file()
                or target.stat().st_size > 128 * 1024
            ):
                raise ServerError("Reviewed reference must be a bounded regular selected-card link")
            contents = target.read_bytes()
            if reference["sha256"] != hashlib.sha256(contents).hexdigest():
                raise ServerError("Reviewed reference snapshot changed")
            try:
                source_lines["server-monitoring/" + relative] = len(
                    contents.decode("utf-8").splitlines()
                )
            except UnicodeError as exc:
                raise ServerError("Reviewed reference must be UTF-8 text") from exc
        sites = manifest["sites"]
        profiles = manifest["platform_profiles"]
        omissions = manifest["omissions"]
        if (
            not isinstance(sites, list)
            or not isinstance(profiles, list)
            or not isinstance(omissions, list)
            or len(omissions) > 8
        ):
            raise ServerError("Invalid sites manifest lists")
        for site in [*sites, *profiles]:
            source = site.get("source", "") if isinstance(site, dict) else ""
            if (
                not isinstance(site, dict)
                or site.get("checked_at") is not None
                or not isinstance(source, str)
                or not source
            ):
                raise ServerError(
                    "Imported sites require selected-card line anchors and unverified checked_at"
                )
            for evidence in source.split(";"):
                anchor = re.fullmatch(r"(.+)#L(\d+)(?:-L(\d+))?", evidence)
                if (
                    anchor is None
                    or anchor[1] not in source_lines
                    or (anchor[1].endswith("/vps-hosts.mdc") and site not in profiles)
                    or not 1
                    <= int(anchor[2])
                    <= int(anchor[3] or anchor[2])
                    <= source_lines[anchor[1]]
                ):
                    raise ServerError("Invalid reviewed-source line anchor")
        metadata["sites"] = sites
        metadata["platform_profiles"] = profiles
        metadata["facts"] = [
            {
                "text": safe_text(omission, 512),
                "source": f"server-monitoring/.cursor/rules/host-{source_id}.mdc",
                "checked_at": None,
            }
            for omission in omissions
        ]
    return {**record, "metadata": validate_server(metadata)}
