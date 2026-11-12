# ruff: noqa: RUF001
"""Server pages composed inside the existing dashboard shell and progressive forms."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from harness.servers import (
    PASSPORT_NUMBER_FIELDS,
    PASSPORT_TEXT_FIELDS,
    ServerError,
    archive_server,
    edit_server,
    get_server,
    list_servers,
    load_server_metadata,
    record_event,
    validate_profile,
    validate_site,
)
from harness.storage import connect_database


def parse_server_route(path: str, query: str) -> tuple[str | None, dict[str, str]]:
    parts = path.strip("/").split("/")
    if not path.endswith("/") or parts[0] != "servers" or len(parts) > 2:
        raise ServerError("Invalid server route")
    values = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=24)
    if set(values) - {
        "q",
        "archived",
        "environment",
        "offset",
        "before",
        "sites_q",
        "sites_offset",
        "site_id",
        "profiles_offset",
        "profile_id",
        "view",
        "collection",
        "object_id",
        "map_q",
        "map_status",
        "map_sort",
        "map_order",
        "map_size",
        "map_offset",
        "app_id",
    } or any(len(v) != 1 for v in values.values()):
        raise ServerError("Invalid server filter")
    return (parts[1] if len(parts) == 2 else None), {key: v[0] for key, v in values.items()}


def mutate_servers_page(database: Path, identity: str | None, fields: dict[str, str]) -> None:
    action = fields.get("action")
    target = fields.get("server_id", "")
    if identity is not None and identity != target:
        raise ServerError("Server target does not match page")
    try:
        revision = int(fields.get("expected_revision", ""))
    except ValueError as exc:
        raise ServerError("Revision is required") from exc
    connection = connect_database(database)
    try:
        if action and action.startswith("server_map_") and identity is not None:
            from harness.dashboard_server_map import mutate_form

            mutate_form(connection, identity, revision, fields)
        elif action == "server_save":
            if identity is None and revision != 0:
                raise ServerError("Create requires revision zero")
            previous = (
                {}
                if revision == 0
                else {
                    key: value
                    for key, value in load_server_metadata(connection, target).items()
                    if key not in {"sites", "platform_profiles"}
                }
            )
            allowed = {
                "action",
                "server_id",
                "expected_revision",
                "name",
                "role",
                "environment",
                "aliases",
                "addresses",
                "domains",
                "services",
                "paths",
                "rules",
                "project_ids",
                "ssh_alias",
                "user",
                "port",
                "key_reference",
            }
            if set(fields) - allowed:
                raise ServerError("Invalid form fields")
            metadata: dict[str, Any] = {
                **previous,
                **{key: fields.get(key, "") for key in ("name", "role", "environment")},
            }
            for key in (
                "aliases",
                "addresses",
                "domains",
                "services",
                "paths",
                "rules",
                "project_ids",
            ):
                metadata[key] = [
                    line.strip() for line in fields.get(key, "").splitlines() if line.strip()
                ]
            metadata["connection"] = {
                key: fields.get(key, "") for key in ("ssh_alias", "user", "key_reference")
            }
            if fields.get("port"):
                try:
                    metadata["connection"]["port"] = int(fields["port"])
                except ValueError as exc:
                    raise ServerError("Invalid SSH port") from exc
            edit_server(connection, target, revision, metadata=metadata)
        elif action == "server_passport_save" and identity is not None:
            passport_fields = (
                *PASSPORT_TEXT_FIELDS,
                *PASSPORT_NUMBER_FIELDS,
                "storage",
                "source",
                "checked_at",
            )
            if set(fields) - {
                "action",
                "server_id",
                "expected_revision",
                *("passport_" + key for key in passport_fields),
            }:
                raise ServerError("Invalid passport form fields")
            passport: dict[str, Any] = {
                key: fields.get("passport_" + key, "") for key in PASSPORT_TEXT_FIELDS
            }
            for key in PASSPORT_NUMBER_FIELDS:
                value = fields.get("passport_" + key, "").strip()
                try:
                    passport[key] = int(value) if value else None
                except ValueError as exc:
                    raise ServerError("Passport capacities must be integers") from exc
            passport["storage"] = [
                line.strip()
                for line in fields.get("passport_storage", "").splitlines()
                if line.strip()
            ]
            passport["source"] = fields.get("passport_source", "operator")
            passport["checked_at"] = fields.get("passport_checked_at") or None
            edit_server(connection, target, revision, metadata={"passport": passport})
        elif action == "server_archive" and identity is not None:
            if set(fields) - {"action", "server_id", "expected_revision", "archived"}:
                raise ServerError("Invalid archive form")
            archive_server(connection, target, revision, fields.get("archived") == "1")
        elif action in {"server_site_save", "server_site_delete"} and identity is not None:
            allowed = {"action", "server_id", "expected_revision", "site_key"}
            if action == "server_site_save":
                allowed.update(
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
            if set(fields) - allowed:
                raise ServerError("Invalid site form fields")
            metadata = load_server_metadata(connection, target)
            key = fields.get("site_key", "")
            sites = metadata["sites"]
            if key != "@new" and not any(site["site_id"] == key for site in sites):
                raise ServerError("Site no longer exists; preserve the draft and reconcile")
            if action == "server_site_delete":
                edit_server(connection, target, revision, delete_site_id=key)
            else:
                site: dict[str, Any] = {
                    k: fields.get(k, "")
                    for k in (
                        "site_id",
                        "profile_id",
                        "name",
                        "purpose",
                        "environment",
                        "source",
                        "access_policy",
                    )
                }
                if key != "@new" and site["site_id"] != key:
                    raise ServerError("Existing site identity cannot change")
                for list_key in ("domains", "runtime", "paths", "dependencies"):
                    site[list_key] = [
                        line.strip()
                        for line in fields.get(list_key, "").splitlines()
                        if line.strip()
                    ]
                try:
                    site["ports"] = [
                        int(line.strip())
                        for line in fields.get("ports", "").splitlines()
                        if line.strip()
                    ]
                except ValueError as exc:
                    raise ServerError("Ports must be integers, one per line") from exc
                site["stack"] = []
                for line in fields.get("stack", "").splitlines():
                    if line.strip():
                        name, _, version = line.partition("|")
                        site["stack"].append({"name": name.strip(), "version": version.strip()})
                site["checked_at"] = fields.get("checked_at") or None
                normalized = validate_site(site)
                if key == "@new":
                    if any(existing["site_id"] == normalized["site_id"] for existing in sites):
                        raise ServerError(
                            "Site ID already exists; choose another explicit identity"
                        )
                edit_server(connection, target, revision, site=normalized)
        elif action in {"server_profile_save", "server_profile_delete"} and identity is not None:
            allowed = {"action", "server_id", "expected_revision", "profile_key"}
            if action == "server_profile_save":
                allowed.update(
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
            if set(fields) - allowed:
                raise ServerError("Invalid preparation profile form")
            metadata = load_server_metadata(connection, target)
            profiles = metadata["platform_profiles"]
            key = fields.get("profile_key", "")
            if key != "@new" and not any(profile["profile_id"] == key for profile in profiles):
                raise ServerError("Preparation profile no longer exists; reconcile the draft")
            if action == "server_profile_delete":
                edit_server(connection, target, revision, delete_profile_id=key)
            else:
                profile: dict[str, Any] = {
                    k: fields.get(k, "")
                    for k in (
                        "profile_id",
                        "name",
                        "environment",
                        "domain_pattern",
                        "path_pattern",
                        "access_policy",
                        "source",
                    )
                }
                if key != "@new" and profile["profile_id"] != key:
                    raise ServerError("Existing profile identity cannot change")
                for list_key in ("runtime", "steps", "dependencies"):
                    profile[list_key] = [
                        line.strip()
                        for line in fields.get(list_key, "").splitlines()
                        if line.strip()
                    ]
                profile["stack"] = []
                for line in fields.get("stack", "").splitlines():
                    if line.strip():
                        name, _, version = line.partition("|")
                        profile["stack"].append({"name": name.strip(), "version": version.strip()})
                profile["checked_at"] = fields.get("checked_at") or None
                normalized = validate_profile(profile)
                if key == "@new":
                    if any(p["profile_id"] == normalized["profile_id"] for p in profiles):
                        raise ServerError("Preparation profile ID already exists")
                edit_server(connection, target, revision, profile=normalized)
        elif action == "server_record" and identity is not None:
            if set(fields) - {
                "action",
                "server_id",
                "expected_revision",
                "kind",
                "summary",
                "findings",
                "checked_at",
                "source",
            }:
                raise ServerError("Invalid record form")
            record_event(
                connection,
                target,
                revision,
                {
                    "kind": fields.get("kind"),
                    "summary": fields.get("summary"),
                    "findings": fields.get("findings", "").splitlines(),
                    "checked_at": fields.get("checked_at"),
                    "source": fields.get("source", "operator"),
                },
            )
        else:
            raise ServerError("Invalid server action")
    finally:
        connection.close()


def _badge(value: str, *, muted: bool = False) -> str:
    return (
        '<span class="server-badge'
        + (" is-muted" if muted else "")
        + '">'
        + escape(value or "Не задано")
        + "</span>"
    )


def _facts(items: list[tuple[str, Any]], *, empty: str = "Не указано") -> str:
    return (
        '<dl class="server-facts">'
        + "".join(
            "<div><dt>"
            + escape(label)
            + (
                '</dt><dd class="server-fact-empty">'
                if value is None or value == "" or value == []
                else "</dt><dd>"
            )
            + escape(
                ("\n".join(map(str, value)) or empty)
                if isinstance(value, list)
                else empty
                if value is None or value == ""
                else str(value)
            )
            + "</dd></div>"
            for label, value in items
        )
        + "</dl>"
    )


def _stack(values: list[dict[str, str]]) -> str:
    return ", ".join(
        item["name"] + (" " + item["version"] if item["version"] else "") for item in values
    )


def _field(
    name: str,
    label: str,
    value: Any,
    draft: dict[str, str],
    *,
    multiline: bool = False,
    required: bool = False,
    readonly: bool = False,
    help_text: str = "",
) -> str:
    default_value = value
    value = draft.get(name, value)
    if isinstance(value, list):
        value = "\n".join(
            item["name"] + (" | " + item["version"] if item["version"] else "")
            if isinstance(item, dict)
            else str(item)
            for item in value
        )
    attrs = ' data-recovered-draft="true"' if name in draft else ""
    if name.startswith(("map_", "link_")) or name == "object_id":
        if isinstance(default_value, list):
            default_value = "\n".join(
                item["name"] + (" | " + item["version"] if item["version"] else "")
                if isinstance(item, dict)
                else str(item)
                for item in default_value
            )
        attrs += (
            ' data-server-default="'
            + escape("" if default_value is None else str(default_value), quote=True)
            + '"'
        )
    attrs += " required" if required else ""
    attrs += " readonly" if readonly else ""
    if name in {"server_id", "site_id", "profile_id", "object_id"}:
        attrs += ' pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}"'
    if name in {"map_size_bytes", "map_port"}:
        attrs += ' type="number" step="1" min="' + ("1" if name == "map_port" else "0") + '"'
    if name == "name":
        attrs += ' maxlength="160"'
    if name.removeprefix("passport_") in PASSPORT_NUMBER_FIELDS:
        key = name.removeprefix("passport_")
        attrs += ' type="number" step="1" min="' + ("0" if key == "swap_mib" else "1") + '"'
        attrs += ' max="' + str(65536 if key == "cpu_cores" else 2**40) + '"'
    content = "<label>" + escape(label)
    if multiline:
        content += (
            '<textarea name="'
            + name
            + '" rows="3"'
            + attrs
            + ">"
            + escape(str(value or ""))
            + "</textarea>"
        )
    else:
        content += (
            '<input name="'
            + name
            + '" value="'
            + escape("" if value is None else str(value), quote=True)
            + '"'
            + attrs
            + ">"
        )
    if help_text:
        content += "<small>" + escape(help_text) + "</small>"
    return content + "</label>"


def _post(
    action: str,
    identity: str,
    revision: str,
    *,
    key_name: str = "",
    key: str = "",
    identity_field: bool = True,
) -> str:
    values = {"action": action, "expected_revision": revision}
    if identity_field:
        values["server_id"] = identity
    if key_name:
        values[key_name] = key
    return '<form method="post" class="server-editor">' + "".join(
        '<input type="hidden" name="' + name + '" value="' + escape(value, quote=True) + '">'
        for name, value in values.items()
    )


def _group(title: str, fields: str) -> str:
    return (
        '<fieldset class="server-fieldset"><legend>'
        + escape(title)
        + '</legend><div class="server-form-grid">'
        + fields
        + "</div></fieldset>"
    )


def _editor(
    metadata: dict[str, Any],
    identity: str,
    revision: int,
    draft: dict[str, str],
    error: str,
    *,
    creating: bool = False,
) -> str:
    selected = draft if error and draft.get("action") == "server_save" else {}
    content = _post(
        "server_save",
        identity,
        selected.get("expected_revision", str(revision)),
        identity_field=False,
    )
    content += _group(
        "Основные сведения",
        "".join(
            [
                _field(
                    "name", "Название сервера", metadata.get("name", ""), selected, required=True
                ),
                _field("role", "Назначение", metadata.get("role", ""), selected),
                _field(
                    "environment",
                    "Окружение",
                    metadata.get("environment", ""),
                    selected,
                    help_text="Например: production, test или development",
                ),
                _field(
                    "server_id",
                    "Постоянный ID",
                    identity,
                    selected,
                    required=True,
                    readonly=not creating,
                    help_text="Латинские буквы, цифры, дефис и подчёркивание",
                ),
            ]
        ),
    )
    connection = metadata.get("connection", {})
    content += _group(
        "Адреса и подключение",
        "".join(
            [
                _field(
                    "addresses",
                    "IP-адреса и хосты",
                    metadata.get("addresses", []),
                    selected,
                    multiline=True,
                    help_text="По одному на строку",
                ),
                _field(
                    "domains",
                    "Домены сервера",
                    metadata.get("domains", []),
                    selected,
                    multiline=True,
                    help_text="По одному на строку",
                ),
                _field("ssh_alias", "SSH-алиас", connection.get("ssh_alias", ""), selected),
                _field("user", "Пользователь SSH", connection.get("user", ""), selected),
                _field("port", "Порт SSH", connection.get("port", ""), selected),
                _field(
                    "key_reference",
                    "Ссылка на ключ",
                    connection.get("key_reference", ""),
                    selected,
                    help_text="Название или ссылка, без содержимого ключа",
                ),
            ]
        ),
    )
    content += _group(
        "Сервисы и правила работы",
        "".join(
            _field(
                key,
                label,
                metadata.get(key, []),
                selected,
                multiline=True,
                help_text="По одному на строку",
            )
            for key, label in [
                ("services", "Сервисы"),
                ("paths", "Важные пути"),
                ("rules", "Правила работы"),
            ]
        ),
    )
    content += _group(
        "Связи и обозначения",
        "".join(
            _field(
                key,
                label,
                metadata.get(key, []),
                selected,
                multiline=True,
                help_text="По одному на строку",
            )
            for key, label in [
                ("aliases", "Другие названия"),
                ("project_ids", "ID связанных проектов"),
            ]
        ),
    )
    return (
        content
        + '<div class="server-form-footer"><p class="server-help">Пароли, токены, приватные ключи и env-файлы храните в личном хранилище.</p><button class="btn btn-primary" type="submit">Сохранить карточку</button></div></form>'
    )


def _pager(
    filters: dict[str, str],
    page: dict[str, Any],
    offset_key: str,
    selection_key: str,
    label: str,
    anchor: str,
) -> str:
    links = []
    if page["offset"] or filters.get(selection_key):
        previous = {
            key: value for key, value in filters.items() if key not in {offset_key, selection_key}
        }
        links.append(
            '<a class="btn" href="?'
            + escape(urlencode(previous), quote=True)
            + "#"
            + anchor
            + '">К началу</a>'
        )
    if page["next_offset"] is not None:
        links.append(
            '<a class="btn" href="?'
            + escape(urlencode({**filters, offset_key: page["next_offset"]}), quote=True)
            + "#"
            + anchor
            + '">Следующие '
            + label
            + " →</a>"
        )
    return (
        '<nav class="server-pagination" aria-label="Страницы '
        + label
        + '">'
        + "".join(links)
        + "</nav>"
        if links
        else ""
    )


def _render_sites(
    card: dict[str, Any], draft: dict[str, str], error: str, filters: dict[str, str]
) -> str:
    sites, page = card["metadata"]["sites"], card["sites_page"]
    content = '<section class="server-view" id="server-sites" data-server-view="sites"><div class="server-section-heading"><div><h2>Сайты и приложения</h2><p>Что размещено на сервере и как запускается.</p></div><a class="btn btn-primary" href="#site-@new" data-server-open="site-@new">Добавить приложение</a></div>'
    content += (
        '<form method="get" data-server-search class="server-filters server-app-search" action="#server-sites"><label>Поиск приложения или домена<input type="search" name="sites_q" placeholder="Название, домен или назначение" value="'
        + escape(filters.get("sites_q", ""), quote=True)
        + '"></label>'
    )
    content += "".join(
        '<input type="hidden" name="' + name + '" value="' + escape(value, quote=True) + '">'
        for name, value in filters.items()
        if name not in {"sites_q", "sites_offset", "site_id"}
    )
    content += '<button class="btn">Найти приложение</button><p class="server-filter-status" role="status" data-server-filter-status></p></form>'
    content += (
        '<p class="server-list-note">Показано '
        + str(len(sites))
        + " из "
        + str(page["total"])
        + " · всего приложений: "
        + str(page["full_total"])
        + "</p>"
    )
    if not sites:
        content += (
            '<div class="server-empty"><h3>'
            + (
                "По выбранным условиям приложений нет"
                if page["full_total"]
                else "Приложения ещё не описаны"
            )
            + "</h3><p>"
            + (
                "Измените поисковый запрос."
                if page["full_total"]
                else "Добавьте домен, стек и способ запуска. Отсутствие записи не означает, что сервер пуст."
            )
            + "</p></div>"
        )
    for site in [*sites, None]:
        key = "@new" if site is None else site["site_id"]
        failed = bool(
            error
            and draft.get("site_key") == key
            and draft.get("action") in {"server_site_save", "server_site_delete"}
        )
        selected = draft if failed and draft.get("action") == "server_site_save" else {}
        values = site or {"source": "operator"}
        revision = (
            draft.get("expected_revision", str(card["revision"]))
            if failed
            else str(card["revision"])
        )
        content += (
            '<details class="server-entry'
            + (" server-add" if site is None else "")
            + '" id="site-'
            + escape(key, quote=True)
            + '"'
            + (" open" if failed or filters.get("site_id") == key else "")
            + "><summary>"
        )
        if site:
            content += (
                '<span class="server-entry-copy"><strong>'
                + escape(site["name"])
                + '</strong><span class="mono">'
                + escape(", ".join(site["domains"]) or "Домен не указан")
                + "</span><small>"
                + escape(_stack(site["stack"]) or site["purpose"] or "Стек не указан")
                + "</small></span>"
                + _badge(site["environment"])
                + '<span class="server-entry-hint">Подробнее</span>'
            )
        else:
            content += "＋ Добавить сайт или приложение"
        content += '</summary><div class="server-entry-body">'
        if site:
            content += _facts(
                [
                    ("Назначение", site["purpose"]),
                    ("Домены", site["domains"]),
                    ("Стек", _stack(site["stack"])),
                    ("Запуск и службы", site["runtime"]),
                    ("Порты", ", ".join(map(str, site["ports"]))),
                    ("Пути", site["paths"]),
                    ("Зависимости", site["dependencies"]),
                    ("Доступ", site["access_policy"]),
                    ("Профиль подготовки", site["profile_id"]),
                    ("ID приложения", site["site_id"]),
                    ("Источник", site["source"]),
                    ("Проверено", site["checked_at"] or "Не проверено"),
                ]
            )
            content += (
                '<details class="server-edit-disclosure" id="site-edit-'
                + escape(key, quote=True)
                + '"'
                + (" open" if failed else "")
                + "><summary>Редактировать приложение</summary>"
            )
        content += _post(
            "server_site_save", card["server_id"], revision, key_name="site_key", key=key
        )
        groups = [
            (
                "Основные сведения",
                [
                    ("name", "Название", False),
                    ("site_id", "Постоянный ID приложения", False),
                    ("purpose", "Назначение", False),
                    ("environment", "Окружение", False),
                    ("domains", "Домены", True),
                    ("profile_id", "ID профиля подготовки", False),
                ],
            ),
            (
                "Стек и размещение",
                [
                    ("stack", "Стек: название | версия", True),
                    ("runtime", "Запуск и службы", True),
                    ("ports", "Порты", True),
                    ("paths", "Пути размещения", True),
                    ("dependencies", "Зависимости и ссылки на БД", True),
                    ("access_policy", "Политика доступа", True),
                ],
            ),
            (
                "Источник сведений",
                [("source", "Источник", False), ("checked_at", "Когда проверено", False)],
            ),
        ]
        for title, fields in groups:
            content += _group(
                title,
                "".join(
                    _field(
                        name,
                        label,
                        values.get(name, ""),
                        selected,
                        multiline=multi,
                        required=name in {"site_id", "name", "source"},
                        readonly=bool(site and name == "site_id"),
                        help_text="ISO с часовым поясом; пусто — неизвестно"
                        if name == "checked_at"
                        else "По одному на строку"
                        if multi
                        else "",
                    )
                    for name, label, multi in fields
                ),
            )
        content += '<div class="server-form-footer"><p class="server-help">Не добавляйте пароли, env-файлы и строки подключения с учётными данными.</p><button class="btn btn-primary">Сохранить приложение</button></div></form>'
        if site:
            content += (
                _post(
                    "server_site_delete", card["server_id"], revision, key_name="site_key", key=key
                )
                + '<div class="server-form-footer"><button class="btn btn-danger">Удалить запись приложения</button><p class="server-help">Сервер и само приложение не изменяются.</p></div></form></details>'
            )
        content += "</div></details>"
    return (
        content
        + _pager(filters, page, "sites_offset", "site_id", "приложения", "server-sites")
        + "</section>"
    )


def _render_profiles(
    card: dict[str, Any], draft: dict[str, str], error: str, filters: dict[str, str]
) -> str:
    profiles, page = card["metadata"]["platform_profiles"], card["profiles_page"]
    content = '<section class="server-view" id="server-profiles" data-server-view="profiles"><div class="server-section-heading"><div><h2>Профили подготовки</h2><p>Повторно используемые правила размещения для разных окружений и стеков.</p></div><a class="btn btn-primary" href="#profile-@new" data-server-open="profile-@new">Добавить профиль</a></div>'
    if not profiles:
        content += '<div class="server-empty"><h3>Профили ещё не описаны</h3><p>Сохраните известные правила подготовки площадки: домен, путь, стек и порядок проверки.</p></div>'
    for profile in [*profiles, None]:
        key = "@new" if profile is None else profile["profile_id"]
        failed = bool(
            error
            and draft.get("profile_key") == key
            and draft.get("action") in {"server_profile_save", "server_profile_delete"}
        )
        selected = draft if failed and draft.get("action") == "server_profile_save" else {}
        values = profile or {"source": "operator"}
        revision = (
            draft.get("expected_revision", str(card["revision"]))
            if failed
            else str(card["revision"])
        )
        content += (
            '<details class="server-entry'
            + (" server-add" if profile is None else "")
            + '" id="profile-'
            + escape(key, quote=True)
            + '"'
            + (" open" if failed or filters.get("profile_id") == key else "")
            + "><summary>"
        )
        if profile:
            content += (
                '<span class="server-entry-copy"><strong>'
                + escape(profile["name"])
                + "</strong><span>"
                + escape(_stack(profile["stack"]) or "Стек не указан")
                + "</span><small>"
                + escape(profile["domain_pattern"] or "Шаблон домена не указан")
                + "</small></span>"
                + _badge(profile["environment"])
                + '<span class="server-entry-hint">Подробнее</span>'
            )
        else:
            content += "＋ Добавить профиль подготовки"
        content += '</summary><div class="server-entry-body">'
        if profile:
            content += _facts(
                [
                    ("Шаблон домена", profile["domain_pattern"]),
                    ("Шаблон пути", profile["path_pattern"]),
                    ("Стек", _stack(profile["stack"])),
                    ("Запуск и службы", profile["runtime"]),
                    ("Доступ", profile["access_policy"]),
                    ("Шаги подготовки", profile["steps"]),
                    ("Зависимости", profile["dependencies"]),
                    ("ID профиля", key),
                    ("Источник", profile["source"]),
                    ("Проверено", profile["checked_at"] or "Не проверено"),
                ]
            )
            content += (
                '<details class="server-edit-disclosure" id="profile-edit-'
                + escape(key, quote=True)
                + '"'
                + (" open" if failed else "")
                + "><summary>Редактировать профиль</summary>"
            )
        content += _post(
            "server_profile_save", card["server_id"], revision, key_name="profile_key", key=key
        )
        groups = [
            (
                "Основные сведения",
                [
                    ("name", "Название профиля", False),
                    ("profile_id", "Постоянный ID профиля", False),
                    ("environment", "Окружение", False),
                    ("stack", "Стек: название | версия", True),
                ],
            ),
            (
                "Правила размещения",
                [
                    ("domain_pattern", "Шаблон домена", False),
                    ("path_pattern", "Шаблон пути", False),
                    ("runtime", "Запуск и службы", True),
                    ("access_policy", "Политика доступа", True),
                    ("steps", "Шаги подготовки и проверки", True),
                    ("dependencies", "Зависимости", True),
                ],
            ),
            (
                "Источник сведений",
                [("source", "Источник", False), ("checked_at", "Когда проверено", False)],
            ),
        ]
        for title, fields in groups:
            content += _group(
                title,
                "".join(
                    _field(
                        name,
                        label,
                        values.get(name, ""),
                        selected,
                        multiline=multi,
                        required=name in {"profile_id", "name", "source"},
                        readonly=bool(profile and name == "profile_id"),
                        help_text="ISO с часовым поясом; пусто — неизвестно"
                        if name == "checked_at"
                        else "По одному на строку"
                        if multi
                        else "",
                    )
                    for name, label, multi in fields
                ),
            )
        content += '<div class="server-form-footer"><p class="server-help">Профиль описывает подготовку площадки. Фактическое размещение хранится в приложении.</p><button class="btn btn-primary">Сохранить профиль</button></div></form>'
        if profile:
            content += (
                _post(
                    "server_profile_delete",
                    card["server_id"],
                    revision,
                    key_name="profile_key",
                    key=key,
                )
                + '<div class="server-form-footer"><button class="btn btn-danger">Удалить профиль</button></div></form></details>'
            )
        content += "</div></details>"
    return (
        content
        + _pager(filters, page, "profiles_offset", "profile_id", "профили", "server-profiles")
        + "</section>"
    )


def _checked_time(value: str | None) -> str:
    if not value:
        return "Не проверено"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        label = parsed.strftime("%d.%m.%Y %H:%M UTC")
    except (ValueError, OverflowError):
        # Valid timezone-bearing dates at datetime's limits may overflow conversion to UTC.
        label = value
    return (
        '<time data-local-time data-time-year datetime="'
        + escape(value, quote=True)
        + '">'
        + escape(label)
        + "</time>"
    )


def _capacity(value: int | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "Не выделен (0 МиБ)"
    return f"{value / 1024:,.2f} ГиБ ({value} МиБ)".replace(",", " ")


def _storage_capacity(value: str) -> str:
    match = re.fullmatch(r"(\d+)\s*(?:bytes?|байт(?:а|ов)?|B)", value, re.IGNORECASE)
    if not match:
        return value
    amount = int(match[1])
    units = ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ", "ЭиБ")
    index = 0
    while amount >= 1024 ** (index + 1) and index < len(units) - 1:
        index += 1
    number = f"{amount / 1024**index:,.2f}".rstrip("0").rstrip(".")
    return number.replace(",", " ").replace(".", ",") + " " + units[index]


def _render_storage(entries: list[str]) -> str:
    content = '<div class="server-storage"><h3>Диски и тома</h3>'
    if not entries:
        return content + '<p class="server-help">Диски и тома не указаны</p></div>'
    content += (
        '<table class="server-storage-table"><thead><tr>'
        '<th scope="col">Устройство / том</th><th scope="col">Ёмкость</th>'
        '<th scope="col">Тип и размещение</th></tr></thead><tbody>'
    )
    for entry in entries:
        parts = [part.strip() for part in re.split(r"[;·]", entry)]
        # Known lsblk-style legacy records put the capacity after a device colon.
        legacy = re.fullmatch(
            r"(/[^:;]+):\s*(\d+(?:[.,]\d+)?\s*(?:bytes?|[KMGTPE]?i?B)(?:\s*\([^()]+\))?)",
            parts[0],
            re.IGNORECASE,
        )
        if legacy:
            parts[:1] = [legacy[1], legacy[2]]
        capacity_index = next(
            (
                index
                for index, part in enumerate(parts[1:], 1)
                if re.fullmatch(
                    r"\d+(?:[.,]\d+)?\s*(?:bytes?|байт(?:а|ов)?|[KMGTPE]?i?B|[КМГТПЭ]и?Б)(?:\s*\([^()]+\))?",
                    part,
                    re.IGNORECASE,
                )
            ),
            None,
        )
        if capacity_index is None:
            # Free-form records remain intact when their layout is unknown.
            content += '<tr><td colspan="3">' + escape(entry) + "</td></tr>"
            continue
        capacity = parts.pop(capacity_index)
        content += (
            '<tr><td data-label="Устройство / том"><strong>'
            + escape(parts[0])
            + '</strong></td><td data-label="Ёмкость" class="server-storage-capacity" title="'
            + escape(capacity, quote=True)
            + '">'
            + escape(_storage_capacity(capacity))
            + '</td><td data-label="Тип и размещение">'
            + escape(" · ".join(parts[1:]) or "Не указано")
            + "</td></tr>"
        )
    return content + "</tbody></table></div>"


def _passport_freshness_note(card: dict[str, Any]) -> str:
    from harness.server_map import DEFAULT_POLICY

    passport = card["metadata"].get("passport", {})
    if not passport:
        return "Паспорт не заполнен"
    checked = passport.get("checked_at")
    if not checked:
        return "Паспорт не проверен"
    passport_days = card["metadata"].get("map_policy", DEFAULT_POLICY)["passport_days"]
    age = datetime.now(UTC) - datetime.fromisoformat(checked.replace("Z", "+00:00"))
    return (
        "Проверка паспорта устарела; нужна сверка"
        if age.total_seconds() > passport_days * 86400
        else "Паспорт проверен в пределах выбранного интервала"
    )


def _render_passport(card: dict[str, Any], draft: dict[str, str], error: str) -> str:
    passport = card["metadata"].get("passport", {})
    content = (
        '<details class="server-panel server-passport" id="server-passport-full"'
        + (" open" if error and draft.get("action") == "server_passport_save" else "")
        + "><summary><strong>Полный паспорт</strong><span>Система, оборудование и размещение</span></summary>"
        '<div class="server-passport-content"><div class="server-section-heading">'
        "<div><h2>Паспорт сервера</h2><p>Характеристики машины и размещение.</p></div>"
        '<a class="btn" href="#server-passport-edit" data-server-open="server-passport-edit">'
        + ("Редактировать паспорт" if passport else "Заполнить паспорт")
        + "</a></div>"
    )
    if not passport:
        content += '<p class="server-passport-empty">Характеристики ещё не заполнены. Запишите проверенные сведения о машине в отдельные поля.</p>'
    content += '<div class="server-passport-grid">'
    for title, items in [
        (
            "Система",
            [
                ("Имя хоста", passport.get("hostname")),
                ("ОС", passport.get("os")),
                ("Ядро", passport.get("kernel")),
            ],
        ),
        (
            "Оборудование",
            [
                ("Процессор", passport.get("cpu_model")),
                ("Логические CPU", passport.get("cpu_cores")),
                ("Оперативная память", _capacity(passport.get("memory_mib"))),
                ("Swap", _capacity(passport.get("swap_mib"))),
            ],
        ),
        (
            "Размещение",
            [
                ("Провайдер", passport.get("provider")),
                ("Локация / ЦОД", passport.get("location")),
                ("Виртуализация", passport.get("virtualization")),
            ],
        ),
    ]:
        content += "<div><h3>" + title + "</h3>" + _facts(items) + "</div>"
    content += "</div>" + _render_storage(passport.get("storage", []))
    if passport:
        content += '<div class="server-evidence-meta">' + _checked_time(passport["checked_at"])
        content += "<span>" + _passport_freshness_note(card) + "</span>"
        content += (
            '<details id="server-passport-source"><summary>Источник сведений</summary><p>'
            + escape(passport["source"])
            + "</p></details></div>"
        )
    selected = draft if error and draft.get("action") == "server_passport_save" else {}
    content += (
        '<details class="server-edit-disclosure" id="server-passport-edit"'
        + (" open" if selected else "")
        + "><summary>Сведения паспорта</summary>"
        + _post(
            "server_passport_save",
            card["server_id"],
            selected.get("expected_revision", str(card["revision"])),
        )
    )
    for title, fields in [
        (
            "Система",
            [
                ("hostname", "Имя хоста", ""),
                ("os", "Операционная система и версия", ""),
                ("kernel", "Версия ядра", ""),
            ],
        ),
        (
            "Оборудование",
            [
                ("cpu_model", "Модель процессора", ""),
                ("cpu_cores", "Количество логических CPU", "Число потоков / vCPU, доступных ОС"),
                (
                    "memory_mib",
                    "Оперативная память, МиБ",
                    "Общий объём, не свободная память; 1 ГиБ = 1024 МиБ",
                ),
                ("swap_mib", "Swap, МиБ", "0 — не выделен; пусто — неизвестно"),
                (
                    "storage",
                    "Диски и тома",
                    "Один на строку: /dev/vda; 450 GiB; SSD; mount / — разделяйте поля точкой с запятой или ·",
                ),
            ],
        ),
        (
            "Размещение",
            [
                ("provider", "Провайдер", ""),
                ("location", "Локация / ЦОД", ""),
                (
                    "virtualization",
                    "Виртуализация / тип машины",
                    "Например: KVM, VPS или физический сервер",
                ),
            ],
        ),
        (
            "Источник сведений",
            [
                ("source", "Источник характеристик", ""),
                ("checked_at", "Когда проверено", "ISO с часовым поясом; пусто — не проверено"),
            ],
        ),
    ]:
        content += _group(
            title,
            "".join(
                _field(
                    "passport_" + key,
                    label,
                    passport.get(key, "operator" if key == "source" else ""),
                    selected,
                    multiline=key == "storage",
                    required=key == "source",
                    help_text=help_text,
                )
                for key, label, help_text in fields
            ),
        )
    return (
        content + '<div class="server-form-footer"><p class="server-help">'
        "Пустое поле означает неизвестное значение. Загрузка, свободная память и сбои служб относятся к наблюдениям проверки."
        '</p><button class="btn btn-primary">Сохранить паспорт</button></div></form></details></div></details>'
    )


_OBSERVATION_LABELS = {
    "system": "Система и идентификация",
    "hardware": "Оборудование",
    "resources": "Загрузка и использование ресурсов",
    "services": "Службы и контейнеры",
    "access": "Доступ и SSH",
    "network": "Домены, HTTP и TLS",
    "limitations": "Ограничения проверки",
    "notes": "Дополнительные сведения",
}
_LEGACY_FACT_PREFIXES = {
    "system": ("verified ssh endpoint", "hostname:", "os:", "система:", "идентификация:"),
    "hardware": ("hardware:", "cpu:", "оборудование:", "процессор:"),
    "resources": ("resources:", "ресурсы:", "загрузка:"),
    "services": ("failed units:", "docker containers:", "services:", "сервисы:", "службы:"),
    "access": ("effective sshd defaults:", "ssh:", "доступ:"),
    "network": ("domain checks:", "domains:", "домены:", "http /", "http / локальный"),
    "limitations": ("limits:", "ограничения:", "imported service, path and site references"),
}


def _observation_category(fact: dict[str, Any]) -> str:
    if "category" in fact:
        return str(fact["category"])
    # Presentation only. Never extract inventory, mark freshness or rewrite legacy facts.
    text = fact["text"].strip().casefold()
    return next(
        (
            category
            for category, prefixes in _LEGACY_FACT_PREFIXES.items()
            if text.startswith(prefixes)
        ),
        "notes",
    )


def _render_observations(facts: list[dict[str, Any]]) -> str:
    if not facts:
        return ""
    content = '<section class="server-observations"><div class="server-section-heading"><div><h2>Наблюдения проверок</h2><p>Состояние на дату проверки. Текущую исправность нужно проверять заново.</p></div></div>'
    groups: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
    for fact in facts:
        groups.setdefault((fact["checked_at"], fact["source"]), []).append(fact)
    # Checked snapshots first; keep unchecked/imported context behind an explicit disclosure.
    for (checked_at, source), items in sorted(
        groups.items(),
        key=lambda item: (
            item[0][0] is not None,
            datetime.fromisoformat(item[0][0].replace("Z", "+00:00"))
            if item[0][0] is not None
            else datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    ):
        if checked_at is None:
            content += (
                '<details class="server-panel server-unverified"><summary>Непроверенные сведения · '
                + str(len(items))
                + "</summary>"
            )
        else:
            content += (
                '<section class="server-panel server-observation-snapshot"><h3>Проверка · '
                + _checked_time(checked_at)
                + "</h3>"
            )
        content += '<div class="server-observation-grid">'
        for category, label in _OBSERVATION_LABELS.items():
            matching = [fact for fact in items if _observation_category(fact) == category]
            if matching:
                content += '<section class="server-observation-group"><h4>' + label + "</h4><ul>"
                content += "".join("<li>" + escape(fact["text"]) + "</li>" for fact in matching)
                content += "</ul></section>"
        content += (
            '</div><details class="server-evidence-source"><summary>Источник '
            + ("проверки" if checked_at else "сведений")
            + "</summary><p>"
            + escape(source or "Не указан")
            + "</p></details>"
        )
        content += "</details>" if checked_at is None else "</section>"
    return content + "</section>"


def _render_overview(
    card: dict[str, Any], draft: dict[str, str], error: str, summary: str = ""
) -> str:
    metadata, identity = card["metadata"], card["server_id"]
    content = '<section class="server-view" id="server-overview" data-server-view="overview">'
    passport = metadata.get("passport", {})
    content += (
        '<section class="server-panel server-passport-brief"><div class="server-panel-title">'
        '<h2>Краткий паспорт и адреса</h2></div><div class="server-passport-grid server-passport-brief-grid">'
    )
    for title, items in [
        (
            "Назначение и система",
            [
                (
                    "Назначение / окружение",
                    " · ".join(filter(None, [metadata["role"], metadata["environment"]])),
                ),
                ("IP-адреса и хосты", metadata["addresses"]),
                ("ОС", passport.get("os")),
            ],
        ),
        (
            "Оборудование",
            [
                ("Процессор", passport.get("cpu_model")),
                ("Логические CPU", passport.get("cpu_cores")),
                ("Оперативная память", _capacity(passport.get("memory_mib"))),
            ],
        ),
        (
            "Размещение",
            [
                ("Провайдер", passport.get("provider")),
                ("Локация / ЦОД", passport.get("location")),
                ("Виртуализация", passport.get("virtualization")),
            ],
        ),
    ]:
        content += "<div><h3>" + title + "</h3>" + _facts(items) + "</div>"
    content += "</div>" + _render_storage(passport.get("storage", []))
    content += (
        '<div class="server-evidence-meta"><span>Дата проверки: '
        + _checked_time(passport.get("checked_at"))
        + "</span><span>"
        + _passport_freshness_note(card)
        + '</span><span class="server-brief-source">Источник: '
        + escape(
            (passport.get("source", "")[:160] + "…")
            if len(passport.get("source", "")) > 160
            else passport.get("source") or "Не указан"
        )
        + "</span></div>"
    )
    passport_target = "server-passport-source" if passport else "server-passport-edit"
    content += (
        '<div class="server-passport-footer"><p class="server-help">Это характеристики машины; свободные ресурсы требуют отдельного наблюдения с датой.</p><a class="text-link" href="#'
        + passport_target
        + '" data-server-open="'
        + passport_target
        + '">Полный паспорт и источник сведений</a></div></section>'
    )
    content += summary
    content += _render_passport(card, draft, error)
    content += '<div class="server-overview-grid"><section class="server-panel"><div class="server-panel-title"><h2>Адреса и назначение</h2></div>'
    content += _facts(
        [
            ("Назначение", metadata["role"]),
            ("Окружение", metadata["environment"]),
            ("IP-адреса и хосты", metadata["addresses"]),
            ("Домены сервера", metadata["domains"]),
        ]
    )
    content += '</section><section class="server-panel"><div class="server-panel-title"><h2>Подключение SSH</h2></div>'
    connection = metadata["connection"]
    content += _facts(
        [
            ("SSH-алиас", connection.get("ssh_alias")),
            ("Пользователь", connection.get("user")),
            ("Порт", connection.get("port")),
            ("Ссылка на ключ", connection.get("key_reference")),
        ]
    )
    content += '</section><section class="server-panel"><div class="server-panel-title"><h2>Сервисы и размещение</h2></div>'
    content += (
        _facts([("Сервисы", metadata["services"]), ("Важные пути", metadata["paths"])])
        + "</section>"
    )
    content += '<section class="server-panel"><div class="server-panel-title"><h2>Правила работы</h2></div>'
    content += (
        '<ul class="server-rules">'
        + "".join("<li>" + escape(rule) + "</li>" for rule in metadata["rules"])
        + "</ul>"
        if metadata["rules"]
        else '<p class="server-help">Правила ещё не указаны.</p>'
    )
    content += "</section></div>"
    content += _render_observations(metadata["facts"])
    content += (
        '<details class="server-panel server-edit-disclosure" id="server-edit"'
        + (" open" if error and draft.get("action") == "server_save" else "")
        + "><summary>Редактировать сведения сервера</summary>"
    )
    content += _editor(metadata, identity, card["revision"], draft, error) + "</details>"
    content += (
        '<details class="server-panel server-secondary" id="server-reference"'
        + (" open" if error and draft.get("action") == "server_archive" else "")
        + "><summary>Источник, идентификаторы и архив</summary>"
    )
    content += _facts(
        [
            ("ID сервера", identity),
            ("Другие названия", metadata["aliases"]),
            ("ID связанных проектов", metadata["project_ids"]),
            ("Источник", metadata["provenance"]["source"]),
            (
                "Проверено",
                metadata["provenance"]["checked_at"] or "Сведения ещё не проверены на сервере",
            ),
            ("Ревизия", card["revision"]),
        ]
    )
    archive_revision = (
        draft.get("expected_revision", str(card["revision"]))
        if error and draft.get("action") == "server_archive"
        else str(card["revision"])
    )
    content += (
        _post("server_archive", identity, archive_revision)
        + '<input type="hidden" name="archived" value="'
        + ("0" if card["archived"] else "1")
        + '"><div class="server-form-footer"><button class="btn">'
        + ("Восстановить" if card["archived"] else "В архив")
        + '</button><p class="server-help">Карточка и история сохранятся.</p></div></form></details></section>'
    )
    return content


def _render_history(
    card: dict[str, Any], draft: dict[str, str], error: str, filters: dict[str, str]
) -> str:
    selected = draft if error and draft.get("action") == "server_record" else {}
    content = '<section class="server-view" id="server-history" data-server-view="history"><div class="server-section-heading"><div><h2>История работ</h2><p>Аудиты, операции и изменения сведений сервера.</p></div><a class="btn btn-primary" href="#server-record" data-server-open="server-record">Добавить запись</a></div>'
    content += (
        '<details class="server-panel server-edit-disclosure" id="server-record"'
        + (" open" if selected else "")
        + '><summary>Записать результат серверного аудита или операции</summary><p class="server-help">Укажите, что изменено, когда и как проверено. Для аудита без вмешательства явно укажите отсутствие изменений. Диагностика кода, SQL и бизнесовых данных хранится в Task проекта.</p>'
    )
    content += _post(
        "server_record", card["server_id"], selected.get("expected_revision", str(card["revision"]))
    )
    marker = ' data-recovered-draft="true"' if "kind" in selected else ""
    kind = selected.get("kind", "audit")
    controls = (
        '<label>Тип<select name="kind"'
        + marker
        + '><option value="audit"'
        + (" selected" if kind != "operation" else "")
        + '>Аудит</option><option value="operation"'
        + (" selected" if kind == "operation" else "")
        + ">Операция</option></select></label>"
    )
    controls += _field("source", "Источник проверки", "operator", selected, required=True)
    controls += _field("summary", "Результат", "", selected, multiline=True, required=True)
    controls += _field(
        "findings", "Наблюдения", "", selected, multiline=True, help_text="По одному на строку"
    )
    controls += _field(
        "checked_at",
        "Когда проверено",
        "",
        selected,
        required=True,
        help_text="ISO со смещением часового пояса",
    )
    content += (
        _group("Результат проверки", controls)
        + '<div class="server-form-footer"><button class="btn btn-primary">Записать результат</button></div></form></details>'
    )
    if not card["history"]:
        content += '<div class="server-empty"><h3>Записей пока нет</h3><p>Здесь появятся результаты работ и изменения карточки.</p></div>'
    labels = {
        "created": "Создание карточки",
        "updated": "Изменение карточки",
        "archived": "В архив",
        "restored": "Восстановление",
        "audit": "Аудит",
        "operation": "Операция",
    }
    content += '<div class="server-timeline">'
    for event in card["history"]:
        data = event["data"]
        content += (
            '<article class="server-event"><div class="server-event-heading"><strong>'
            + escape(labels[event["kind"]])
            + '</strong><time datetime="'
            + escape(event["recorded_at"], quote=True)
            + '">'
            + escape(event["recorded_at"].replace("T", " ").split(".")[0])
            + " UTC</time></div><p>"
            + escape(
                "Сведения сохранены" if data["summary"] == "Metadata saved" else data["summary"]
            )
            + "</p>"
        )
        if data.get("findings"):
            content += (
                "<ul>"
                + "".join("<li>" + escape(finding) + "</li>" for finding in data["findings"])
                + "</ul>"
            )
        if event["kind"] in {"audit", "operation"}:
            content += (
                "<small>"
                + escape(
                    data.get("source", "") + " · " + (data.get("checked_at") or "Не проверено")
                )
                + "</small>"
            )
        content += "</article>"
    content += "</div>"
    if card["history_truncated"] and card["next_before"]:
        content += (
            '<nav class="server-pagination"><a class="btn" href="?'
            + escape(urlencode({**filters, "before": card["next_before"]}), quote=True)
            + '#server-history">Предыдущие записи</a></nav>'
        )
    return content + "</section>"


def _render_directory(
    result: dict[str, Any], filters: dict[str, str], draft: dict[str, str], error: str, status: str
) -> str:
    page_url = "/servers/" + ("?" + urlencode(filters) if filters else "")
    content = (
        '<div class="server-directory"><header class="server-heading" data-server-page-url="'
        + escape(page_url, quote=True)
        + '"><div><h1>Серверы</h1><p>Адреса, приложения и история работ в одном реестре.</p></div></header>'
        + status
    )
    content += (
        '<details id="server-create" class="server-panel server-create"'
        + (" open" if error else "")
        + "><summary>＋ Добавить сервер</summary>"
    )
    content += (
        _editor({}, draft.get("server_id", uuid4().hex), 0, draft, error, creating=True)
        + "</details>"
    )
    content += '<section class="server-directory-panel"><nav class="server-list-tabs" aria-label="Состояние серверов">'
    for archived, label in [("0", "Активные"), ("1", "Архив")]:
        active = (filters.get("archived", "0") == "1") == (archived == "1")
        query = {key: value for key, value in filters.items() if key in {"q", "environment"}}
        query["archived"] = archived
        content += (
            '<a href="?'
            + escape(urlencode(query), quote=True)
            + '"'
            + (' aria-current="page"' if active else "")
            + ">"
            + label
            + "</a>"
        )
    content += (
        '</nav><form method="get" data-server-search class="server-filters"><input type="hidden" name="archived" value="'
        + ("1" if filters.get("archived") == "1" else "0")
        + '"><label class="server-search">Поиск<input type="search" name="q" placeholder="Сервер, приложение, адрес или домен" value="'
        + escape(filters.get("q", ""), quote=True)
        + '"></label><label>Окружение<input name="environment" placeholder="Все окружения" value="'
        + escape(filters.get("environment", ""), quote=True)
        + '"></label><button class="btn">Найти</button>'
    )
    if filters.get("q") or filters.get("environment"):
        content += (
            '<a class="text-link" href="?archived='
            + ("1" if filters.get("archived") == "1" else "0")
            + '">Сбросить</a>'
        )
    content += (
        '<p class="server-filter-status" role="status" data-server-filter-status></p></form><div class="server-directory-caption">Найдено серверов: <strong>'
        + str(result["total"])
        + "</strong></div>"
    )
    if result["servers"]:
        content += '<div class="server-table"><table><thead><tr><th scope="col">Сервер / назначение</th><th scope="col">Окружение</th><th scope="col">Адреса</th><th scope="col">Домены</th><th scope="col"><span class="sr-only">Открыть</span></th></tr></thead><tbody>'
        for row in result["servers"]:
            href = "/servers/" + row["server_id"] + "/"
            if filters.get("q"):
                href += "?" + urlencode({"sites_q": filters["q"]}) + "#server-sites"
            content += (
                '<tr><td><a class="server-name" href="'
                + escape(href, quote=True)
                + '">'
                + escape(row["name"])
                + "</a><small>"
                + escape(row["role"] or "Назначение не указано")
                + "</small></td><td>"
                + _badge(row["environment"])
                + '</td><td class="mono" data-label="Адреса">'
                + escape("\n".join(row["addresses"]) or "Не указаны")
                + '</td><td class="mono" data-label="Домены">'
                + escape("\n".join(row["domains"]) or "Не указаны")
                + '</td><td><a class="server-row-open" href="'
                + escape(href, quote=True)
                + '" aria-label="Открыть сервер '
                + escape(row["name"], quote=True)
                + '">→</a></td></tr>'
            )
        content += "</tbody></table></div>"
    else:
        content += '<div class="server-empty"><h2>Серверов по выбранным условиям нет</h2><p>Добавьте карточку или измените поиск.</p></div>'
    if filters.get("offset") or result["truncated"]:
        content += '<nav class="server-pagination" aria-label="Страницы серверов">'
        if filters.get("offset"):
            content += (
                '<a class="btn" href="?'
                + escape(
                    urlencode({key: value for key, value in filters.items() if key != "offset"}),
                    quote=True,
                )
                + '">К началу</a>'
            )
        if result["truncated"]:
            content += (
                '<a class="btn" href="?'
                + escape(urlencode({**filters, "offset": result["next_offset"]}), quote=True)
                + '">Следующие серверы →</a>'
            )
        content += "</nav>"
    return content + "</section></div>"


def server_error_message(error: str, fields: dict[str, str]) -> str:
    """Actionable UI feedback without changing the bounded domain error contract."""
    from harness.dashboard_server_map import LABELS

    if error.startswith("Server revision changed") or error.startswith("Check is no longer"):
        return "Сведения изменились после открытия формы. Черновик сохранён. Нажмите «Обновить», сравните изменения и сохраните снова."
    if error.startswith("Inventory numeric field "):
        field = error.split()[3]
        return LABELS.get(field, "Число") + (
            ": введите целое число от 1 до 65535 или оставьте поле пустым."
            if field == "port"
            else ": введите целое число от 0 до 9223372036854775807 или оставьте поле пустым."
        )
    if error in {
        "checked_at must include timezone",
        "Inventory date is outside the supported UTC range",
    }:
        return "Дата проверки / наблюдения: укажите допустимую дату с часовым поясом, например 2026-10-06T12:00:00+07:00, или оставьте необязательное поле пустым."
    messages = {
        "Expected endpoint checks require probe_url and expected_result": "Для проверки ожидаемого адреса укажите проверенный URL и ожидаемый ответ. Поисковый GET / сохраняйте как транспортную проверку.",
        "Probe URL requires a non-secret HTTP/HTTPS address": "Укажите полный HTTP(S) URL без пароля или имени пользователя.",
        "Consumer checks accept docker/systemd/cron/proxy/symlinks": "Укажите только полностью проверенные источники: docker, systemd, cron, proxy, symlinks; по одному на строку.",
        "Object has links; remove its links explicitly before deleting": "Запись используется в связях. Откройте раздел «Связи», уберите нужные связи и повторите удаление записи.",
        "Application has links; remove its links explicitly before deleting": "Приложение используется в связях. Сначала уберите его связи, затем повторите удаление.",
        "Site preparation profile must exist within the same server card": "Профиль используется приложением. Сначала выберите другой профиль или снимите его в редакторе приложения, затем удалите профиль.",
        "Preparation profile must exist on the same server": "Профиль подготовки больше не существует на этом сервере. Выберите доступный профиль или оставьте поле пустым.",
        "An active application uses this directory; revise its classification": "Активное приложение использует этот каталог. Каталог нельзя считать подтверждённым остатком: проверьте связи и измените классификацию или использование приложения.",
        "Confirmed leftovers require former application, operator name, decision date and date of checked links": "Для подтверждённого остатка укажите прежнее закрытое приложение, имя подтвердившего оператора, дату решения и дату проверки текущих ссылок. Основание должно быть «Решение оператора».",
        "Directory classification requires an explicit reason and basis": "Объясните, почему каталог так классифицирован, и выберите известное основание.",
        "Retained directories require a storage purpose": "Укажите, зачем сохраняется каталог, в поле «Зачем сохраняется / срок хранения».",
        "Directory requires an absolute path and context": "Путь каталога должен начинаться с /; укажите его том или контейнер, если он известен.",
        "Verified automation/deployment requires dated probe evidence for that field": "Подтверждённый результат продления или применения требует отдельной прямой проверки с датой. Выберите «Настроено; результат не проверен», пока такая проверка не записана.",
        "Certificate warnings require 1..3 distinct days in 1..366": "Предупреждения о сертификатах: введите от одного до трёх разных целых чисел от 1 до 366 через запятую.",
        "Freshness intervals must be positive integers up to 8760": "Интервалы давности: введите целое число от 1 до 8760 для каждого интервала.",
        "Policy requires integer intervals": "Интервалы и дни предупреждения должны быть целыми числами.",
        "Numeric inventory fields require integer values": "В числовом поле введите целое число или оставьте его пустым.",
        "Ports require integers, one per line": "Порты: укажите целые числа от 1 до 65535, по одному на строку.",
        "Invalid port list": "Порты: укажите целые числа от 1 до 65535, по одному на строку.",
        "Invalid DNS name": "Укажите допустимое доменное имя, например shop.example.test. Шаблон начинается с *.",
        "Name, non-secret source and known evidence basis are required": "Укажите понятное название, несекретный источник и допустимое основание.",
        "Object ID already exists; choose a different explicit ID": "Этот постоянный ID уже занят. Укажите другой ID новой записи.",
        "Observed TLS versions require address, SNI, port and observation time": "Для версий из ответа HTTPS укажите проверенный адрес, имя SNI, порт и дату наблюдения.",
        "Certificate validity dates must increase": "Дата окончания действия сертификата должна быть позже даты начала.",
        "Link no longer exists": "Эта связь уже удалена. Обновите страницу, чтобы увидеть текущие связи.",
    }
    message = messages.get(error.rstrip("."))
    if message:
        return message + " Введённые данные сохранены в форме."
    if fields.get("action", "").startswith("server_map_"):
        return "Сведения карты не сохранены. Проверьте обязательные поля, допустимые состояния и размеры списков; введённые данные сохранены в форме."
    return error + ". Данные формы сохранены."


def render_servers_content(
    database: Path,
    identity: str | None,
    filters: dict[str, str],
    *,
    error: str = "",
    draft: dict[str, str] | None = None,
    saved: bool = False,
) -> str:
    fields = draft or {}
    status = '<p role="status" class="server-status">Сохранено</p>' if saved else ""
    if error:
        status = (
            '<p role="alert" tabindex="-1" data-server-error-summary class="server-error">'
            + escape(server_error_message(error, fields))
            + "</p>"
        )
    connection = connect_database(database)
    connection.execute("SAVEPOINT server_dashboard_read")
    try:
        card = (
            None
            if identity is None
            else get_server(
                connection,
                identity,
                history_limit=20,
                before=int(filters["before"]) if filters.get("before") else None,
                sites_query=filters.get("sites_q", ""),
                sites_offset=int(filters.get("sites_offset", "0")),
                site_id=filters.get("site_id")
                or (fields.get("site_key") if error and fields.get("site_key") != "@new" else None),
                profiles_offset=int(filters.get("profiles_offset", "0")),
                profile_id=filters.get("profile_id")
                or (
                    fields.get("profile_key")
                    if error and fields.get("profile_key") != "@new"
                    else None
                ),
            )
        )
        if card is not None:
            assert identity is not None
            adjusted = False
            for collection, page_key, offset_key, selection, limit in (
                ("sites", "sites_page", "sites_offset", "site_id", 5),
                ("platform_profiles", "profiles_page", "profiles_offset", "profile_id", 4),
            ):
                page = card[page_key]
                if (
                    not card["metadata"][collection]
                    and page["offset"] > 0
                    and not filters.get(selection)
                ):
                    offset = ((page["total"] - 1) // limit) * limit if page["total"] else 0
                    filters[offset_key] = str(offset)
                    adjusted = True
            if adjusted:
                card = get_server(
                    connection,
                    identity,
                    expected_revision=card["revision"],
                    history_limit=20,
                    before=int(filters["before"]) if filters.get("before") else None,
                    sites_query=filters.get("sites_q", ""),
                    sites_offset=int(filters.get("sites_offset", "0")),
                    site_id=filters.get("site_id")
                    or (
                        fields.get("site_key")
                        if error and fields.get("site_key") != "@new"
                        else None
                    ),
                    profiles_offset=int(filters.get("profiles_offset", "0")),
                    profile_id=filters.get("profile_id")
                    or (
                        fields.get("profile_key")
                        if error and fields.get("profile_key") != "@new"
                        else None
                    ),
                )
        if card is None:
            result = list_servers(
                connection,
                filters.get("q", ""),
                archived=filters.get("archived") == "1",
                environment=filters.get("environment", ""),
                limit=50,
                offset=int(filters.get("offset", "0")),
            )
            return _render_directory(result, filters, fields, error, status)
        from harness.dashboard_server_map import (
            DEFAULT_KINDS,
            VIEWS,
            render_collection,
            render_summary,
        )
        from harness.server_map import KINDS

        metadata = card["metadata"]
        if error.startswith("Site preparation profile must exist") and fields.get("profile_key"):
            from harness.server_map import all_objects

            users = [
                obj["name"]
                for obj in all_objects(connection, card["server_id"], "application")
                if obj.get("profile_id") == fields["profile_key"]
            ]
            if users:
                status = (
                    '<p role="alert" tabindex="-1" data-server-error-summary class="server-error">'
                    + escape(
                        server_error_message(error, fields)
                        + " Связанные приложения: "
                        + ", ".join(users[:5])
                        + (" и другие" if len(users) > 5 else "")
                        + "."
                    )
                    + "</p>"
                )
        initial = filters.get("view", "overview")
        if initial not in {"overview", "profiles", "history", *DEFAULT_KINDS}:
            raise ServerError("Invalid server section")
        if filters.get("sites_q") or filters.get("sites_offset") or filters.get("site_id"):
            initial = "sites"
        elif filters.get("profiles_offset") or filters.get("profile_id"):
            initial = "profiles"
        elif filters.get("before"):
            initial = "history"
        if error:
            action = fields.get("action", "")
            if action in {"server_map_link", "server_map_unlink"}:
                from harness.dashboard_server_map import link_error_context

                initial = link_error_context(connection, card["server_id"], filters, fields)
            else:
                initial = (
                    VIEWS[fields["map_kind"]]
                    if action.startswith("server_map_") and fields.get("map_kind") in KINDS
                    else "sites"
                    if action.startswith("server_site_")
                    else "profiles"
                    if action.startswith("server_profile_")
                    else "history"
                    if action == "server_record"
                    else "overview"
                )
        page_url = (
            "/servers/" + card["server_id"] + "/" + ("?" + urlencode(filters) if filters else "")
        )
        content = (
            '<div class="server-workspace" data-server-workspace data-server-initial-view="'
            + initial
            + '"'
            + ' data-server-revision="'
            + str(card["revision"])
            + '"'
            + (' data-server-error="true"' if error else "")
            + '><header class="server-heading" data-server-page-url="'
            + escape(page_url, quote=True)
            + '"><div><a class="text-link" href="/servers/">← Все серверы</a><h1>'
            + escape(metadata["name"])
            + "</h1><p>"
            + escape(metadata["role"] or "Назначение не указано")
            + '</p><div class="server-heading-meta">'
            + _badge(metadata["environment"])
            + _badge("В архиве" if card["archived"] else "В реестре", muted=True)
            + '</div></div><a class="btn" href="#server-edit" data-server-open="server-edit">Редактировать сервер</a></header>'
            + status
        )
        content += '<nav class="server-tabs" aria-label="Разделы карточки сервера">'
        for key, label, count in [
            ("overview", "Обзор", None),
            ("sites", "Приложения", card["sites_page"]["full_total"]),
            ("domains", "Домены", None),
            ("directories", "Каталоги", None),
            ("certificates", "Сертификаты", None),
            ("services", "Службы", None),
            ("profiles", "Подготовка", card["profiles_page"]["full_total"]),
            ("history", "История", None),
        ]:
            content += (
                '<a data-server-navigation href="?view='
                + key
                + "#server-"
                + key
                + '" data-server-tab="'
                + key
                + '">'
                + label
                + ("<span>" + str(count) + "</span>" if count is not None else "")
                + "</a>"
            )
        content += "</nav>"
        content += _render_overview(
            card, fields, error, render_summary(connection, card) if initial == "overview" else ""
        )
        if initial in DEFAULT_KINDS:
            if initial == "sites" and (
                filters.get("site_id")
                or filters.get("sites_offset")
                or (error and fields.get("action", "").startswith("server_site_"))
            ):
                content += _render_sites(card, fields, error, filters)
            else:
                content += render_collection(connection, card, initial, filters, fields, error)
        content += _render_profiles(card, fields, error, filters)
        content += _render_history(card, fields, error, filters)
        return content + "</div>"
    finally:
        connection.close()
