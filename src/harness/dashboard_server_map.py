# ruff: noqa: RUF001
"""Human-readable linked inventory and progressively enhanced, typed forms."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from harness.server_map import (
    BASES,
    COMMON,
    ENUMS,
    FIELDS,
    KINDS,
    InventoryView,
    all_objects,
    certificate_context,
    collection_rows,
    effective_evidence,
    freshness,
    links_for,
    mutate_map,
    object_detail,
    problems,
    server_policy,
    status_of,
    validate_object,
    validate_policy,
)
from harness.servers import ServerError

LABELS = {
    "application": "Приложения",
    "domain": "Домены",
    "route": "Маршруты",
    "directory": "Каталоги и технические пути",
    "service": "Службы",
    "certificate": "Сертификаты",
    "certificate_version": "Версии сертификатов",
    "certificate_use": "Использование HTTPS",
    "probe_scope": "Границы HTTP-проверки",
    "probe_url": "Проверенный URL",
    "expected_result": "Ожидаемый ответ",
    "entry_type": "Тип пути",
    "consumer_checks": "Полностью проверенные источники потребителей",
    "current": "Есть текущие потребители",
    "unconfirmed": "Работа приложения не проверена",
    "transport": "Транспортный ответ; работа приложения не проверена",
    "functional": "Проверен ожидаемый результат",
    "file": "Файл",
    "socket": "Сокет",
    "symlink": "Символическая ссылка",
    "other": "Другой тип пути",
    "non_directory_unconfirmed": "Не подтверждён как каталог",
    "docker": "Docker / Compose",
    "systemd": "systemd",
    "cron": "cron",
    "symlinks": "Файловые ссылки",
    "mount": "Подключение пути",
    "dependency": "Зависимость",
    "name": "Название",
    "status": "Состояние",
    "source": "Источник",
    "checked_at": "Дата проверки",
    "reason": "Почему такое состояние",
    "basis": "Основание",
    "purpose": "Для чего нужно",
    "environment": "Окружение",
    "owner": "Ответственный",
    "description": "Как устроено",
    "usage": "Использование",
    "availability": "Доступность",
    "access_policy": "Доступ и границы проверки",
    "profile_id": "Профиль подготовки",
    "domains": "Прежние ссылки на домены",
    "paths": "Прежние ссылки на пути",
    "runtime": "Прежние ссылки на запуск",
    "dependencies": "Несекретные зависимости",
    "ports": "Порты",
    "stack": "Стек (название | версия)",
    "hostname": "Домен / Host / SNI",
    "pattern": "Шаблон имени",
    "dns": "DNS-ответы A / AAAA / CNAME",
    "discovery_sources": "Все источники обнаружения",
    "destination": "Куда ведёт",
    "https": "Проверка HTTPS",
    "public_result": "Что отвечает посетителю",
    "origin_result": "Что отвечает этот сервер",
    "protocol": "Протокол",
    "port": "Порт",
    "path": "Путь",
    "config_reference": "Файл конфигурации / ссылка",
    "target": "Куда передаёт запрос",
    "context": "Том / контейнер / контекст пути",
    "role": "Роль",
    "size_bytes": "Размер, байты",
    "size_method": "Как измерен размер",
    "exists": "Наличие каталога",
    "target_path": "Цель символической ссылки",
    "retention": "Зачем сохраняется / срок хранения",
    "closed_application": "Прежнее закрытое приложение",
    "decision_owner": "Кто подтвердил ненужность",
    "decision_at": "Когда принято решение",
    "links_checked_at": "Когда проверены текущие ссылки",
    "manager": "Способ запуска",
    "identifier": "Unit / контейнер / процесс",
    "state": "Состояние",
    "launch": "Когда и как запускается",
    "issuer": "Кто выдал",
    "client": "Клиент / поставщик",
    "issuance": "Выпуск",
    "challenge": "Подтверждение владения",
    "renewal": "Продление",
    "renewal_state": "Проверка продления",
    "deployment": "Применение нового сертификата",
    "deployment_state": "Проверка применения",
    "issued_at": "Последний выпуск",
    "last_attempt": "Последняя попытка",
    "last_success": "Последний успешный результат",
    "next_check": "Следующая проверка",
    "automation_reference": "Timer / cron / обработчик",
    "issuance_location": "Где выпускается",
    "procedure": "Порядок ручных действий",
    "failure": "Последняя известная ошибка",
    "fingerprint": "Отпечаток публичного сертификата",
    "sans": "Покрываемые имена (SAN)",
    "not_before": "Действует с",
    "not_after": "Действует до",
    "key_type": "RSA / ECDSA / другое",
    "certificate_reference": "Ссылка на публичный сертификат",
    "trust": "Доверие к сертификату",
    "endpoint": "Место проверки",
    "address": "Проверенный адрес IP / узла",
    "sni": "Проверенное имя SNI",
    "observed_at": "Когда получен ответ HTTPS",
    "trust_context": "Для какого клиента / центра доверия",
    "termination": "Где обрабатывается HTTPS",
    "certificate_file": "Файл сертификата",
    "key_reference": "Путь к ключу (без содержимого)",
    "configured_fingerprint": "Ожидаемая версия из конфигурации",
    "file_fingerprint": "Версия в файле",
    "observed_fingerprints": "Версии, выдаваемые посетителю",
    "reload": "Как применяется / перезагрузка",
    "unknown": "Не выяснено",
    "active": "Используется",
    "closed": "Закрыто",
    "replaced": "Заменено",
    "available": "Открывается в границах проверки",
    "login": "Страница входа доступна",
    "redirect": "Перенаправление",
    "error": "Ошибка",
    "this_server": "На этот сервер",
    "external_proxy": "Через внешний прокси",
    "other_server": "На другой сервер",
    "missing": "Не найдено",
    "valid": "Проверка без ошибки",
    "expired": "Истёк",
    "untrusted": "Нет публичного доверия",
    "mismatch": "Не подходит имени",
    "internal": "Внутренний сертификат",
    "code": "Код",
    "uploads": "Загрузки",
    "data": "Данные",
    "release": "Релиз",
    "backup": "Резервная копия",
    "cache": "Кэш",
    "shared": "Общий",
    "retained": "Хранится намеренно",
    "candidate": "Возможный остаток",
    "leftover": "Подтверждённый остаток",
    "present": "Есть",
    "running": "Запущена",
    "stopped": "Остановлена",
    "failed": "Ошибка проверки / запуска",
    "scheduled": "По расписанию",
    "automatic": "Автоматически",
    "manual": "Вручную",
    "mixed": "Частично автоматически",
    "external": "Внешний поставщик",
    "configured": "Настроено; результат не проверен",
    "verified": "Успешный результат подтверждён",
    "pending": "Ожидает применения",
    "supplier": "Поставщик",
    "server": "Сервер",
    "proxy": "Прокси",
    "cdn": "CDN",
    "operator": "Решение оператора",
    "configuration": "Конфигурация",
    "probe": "Прямая проверка",
    "process": "Процесс",
    "import": "Прежний импорт",
    "reference": "Документированная ссылка",
    "complete_scan": "Полная проверка указанных источников",
    "incomplete": "Есть невыясненные сведения",
    "attention": "Требует внимания",
    "stale": "Проверка устарела",
    "recent": "Проверено в пределах выбранного интервала",
    "expires_soon": "До окончания 30 дней или меньше",
    "different_certificate": "Файл, настройка и ответ HTTPS различаются",
    "documented": "Прежняя ссылка; использование не проверено",
    "serves": "Обслуживает",
    "uses": "Использует",
    "covers": "Покрывает",
    "managed_by": "Управляется",
    "version": "Версия",
    "current_version": "Текущая наблюдавшаяся версия",
    "new_version": "Новый выпуск; применение отдельно",
    "served_version": "Версия из ответа HTTPS",
    "rollback": "Для отката",
    "complete": "Полная в указанных источниках",
    "partial": "Частичная",
    "not_checked": "Не проверялось",
    "all": "Все",
    "asc": "По возрастанию",
    "desc": "По убыванию",
    "false": "Нет",
    "true": "Да",
    "draft": "Черновик; текущую карту не меняет",
    "published": "Опубликована",
}
VIEWS = {
    "application": "sites",
    "domain": "domains",
    "route": "domains",
    "directory": "directories",
    "service": "services",
    "certificate": "certificates",
    "certificate_version": "certificates",
    "certificate_use": "certificates",
}
DEFAULT_KINDS = {
    "sites": "application",
    "domains": "domain",
    "directories": "directory",
    "services": "service",
    "certificates": "certificate",
}


def label(value: Any) -> str:
    if value is None or value == "":
        return "Не выяснено"
    if type(value) is bool:
        return "Да" if value else "Нет"
    return LABELS.get(str(value), str(value))


def enum_label(field: str, value: Any) -> str:
    if field == "probe_scope" and value == "endpoint":
        return "Проверен ожидаемый адрес"
    if field == "entry_type" and value == "directory":
        return "Каталог"
    if field == "protocol":
        return {"https": "HTTPS", "http": "HTTP", "tcp": "TCP"}.get(str(value), label(value))
    if field == "termination" and value == "application":
        return "Приложение"
    if field == "basis" and value == "certificate":
        return "Публичный сертификат"
    return label(value)


def display_value(value: Any, field: str = "", field_type: str = "") -> str:
    if isinstance(value, list):
        return (
            "\n".join(
                (item["name"] + " · " + (item["version"] or "версия не выяснена"))
                if isinstance(item, dict) and "version" in item
                else label(item)
                if field == "consumer_checks"
                else str(item)
                for item in value
            )
            or "Не выяснено"
        )
    if field_type in ENUMS:
        return enum_label(field, value)
    return label(value) if value is None or value == "" or type(value) is bool else str(value)


def display_date(value: str | None) -> str:
    if not value:
        return "Не выяснено"
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(UTC)
        .strftime("%d.%m.%Y %H:%M UTC")
    )


def problem_label(issue: str, obj: dict[str, Any]) -> str:
    descriptions = {
        "availability": "Ошибка проверки доступности; точка и причина указаны в основании",
        "https": "Ошибка HTTPS: " + label(obj.get("https")),
        "trust": "Доверие к сертификату: " + label(obj.get("trust")),
        "state": "Служба сообщает об ошибке запуска",
        "renewal_state": "Продление: " + label(obj.get("renewal_state")),
        "deployment_state": "Применение нового файла: " + label(obj.get("deployment_state")),
        "different_certificate": "Файл, конфигурация и ответ HTTPS различаются; нужна сверка в указанном месте проверки",
        "expired": "Срок наблюдавшейся версии истёк",
        "expires_soon": "Срок наблюдавшейся версии скоро заканчивается",
    }
    text = descriptions.get(issue, label(issue))
    if freshness(obj) == "stale":
        text += "; проверка устарела"
    if issue in {"expired", "expires_soon"} and freshness(obj) != "recent":
        text += "; возможно уже заменён — нужна проверка"
    return text


def list_context(filters: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in filters.items() if k.startswith("map_") or k == "app_id"}


def href(identity: str, kind: str, key: str = "", **filters: str) -> str:
    values = {"view": VIEWS[kind], "collection": kind, **filters}
    if key:
        values["object_id"] = key
    return "/servers/" + identity + "/?" + urlencode(values) + "#server-" + VIEWS[kind]


def link(text: str, url: str) -> str:
    return (
        '<a data-server-navigation href="' + escape(url, quote=True) + '">' + escape(text) + "</a>"
    )


def select(
    name: str,
    title: str,
    selected: str,
    values: list[str] | tuple[str, ...],
    draft: dict[str, str],
    *,
    blank: str = "",
    field: str = "",
) -> str:
    chosen = draft.get(name, selected)
    choices = ([""] if blank else []) + list(values)
    return (
        "<label>"
        + escape(title)
        + '<select name="'
        + name
        + '"'
        + (' data-recovered-draft="true"' if name in draft else "")
        + ' data-server-default="'
        + escape(selected, quote=True)
        + '"'
        + ">"
        + "".join(
            '<option value="'
            + escape(value, quote=True)
            + '"'
            + (" selected" if value == chosen else "")
            + ">"
            + escape(blank if value == "" else enum_label(field, value))
            + "</option>"
            for value in choices
        )
        + "</select></label>"
    )


def mutate_form(
    connection: sqlite3.Connection, identity: str, revision: int, fields: dict[str, str]
) -> None:
    action = fields["action"]
    if action == "server_map_policy_save":
        from harness.servers import edit_server

        if set(fields) != {
            "action",
            "server_id",
            "expected_revision",
            "health_hours",
            "inventory_hours",
            "passport_days",
            "expiry_days",
        }:
            raise ServerError("Invalid map policy form")
        try:
            policy: dict[str, Any] = {
                field: int(fields[field])
                for field in ("health_hours", "inventory_hours", "passport_days")
            }
            policy["expiry_days"] = [
                int(day.strip()) for day in fields["expiry_days"].split(",") if day.strip()
            ]
        except ValueError as exc:
            raise ServerError("Policy requires integer intervals") from exc
        edit_server(
            connection, identity, revision, metadata={"map_policy": validate_policy(policy)}
        )
        return
    basic = {"action", "server_id", "expected_revision", "map_kind", "object_key"}
    kind = fields.get("map_kind", "")
    if action in {"server_map_save", "server_map_delete"}:
        key = fields.get("object_key", "")
        if kind not in KINDS:
            raise ServerError("Invalid map kind")
        if action == "server_map_delete":
            if set(fields) - basic:
                raise ServerError("Invalid delete form fields")
            mutate_map(
                connection, identity, revision, {"action": "delete", "kind": kind, "object_id": key}
            )
            return
        if set(fields) - (
            basic
            | {"object_id"}
            | {"map_" + k for k in COMMON - {"evidence"}}
            | {"map_" + k for k in FIELDS[kind]}
        ):
            raise ServerError("Unknown map form fields")
        existing: dict[str, Any] = {}
        if key != "@new":
            existing = object_detail(connection, identity, kind, key)
            if fields.get("object_id") != key:
                raise ServerError("Existing object identity cannot change")
        else:
            try:
                object_detail(connection, identity, kind, fields.get("object_id", ""))
            except ServerError:
                pass
            else:
                raise ServerError("Object ID already exists; choose a different explicit ID")
        baseline = existing or values_for_new(kind)
        data: dict[str, Any] = {
            k: fields.get("map_" + k, baseline.get(k, ""))
            for k in COMMON - {"evidence", "checked_at"}
        }
        data["checked_at"] = fields.get("map_checked_at", baseline.get("checked_at")) or None
        for name, field_type in FIELDS[kind].items():
            if "map_" + name not in fields:
                data[name] = baseline.get(name)
                continue
            raw = fields.get("map_" + name, "")
            if field_type in {"number", "port"}:
                try:
                    data[name] = int(raw) if raw else None
                except ValueError as exc:
                    raise ServerError("Numeric inventory fields require integer values") from exc
            elif field_type == "date":
                data[name] = raw or None
            elif field_type == "bool":
                data[name] = raw == "true"
            elif field_type in {"list", "ports", "stack", "consumer_checks"}:
                lines = [line.strip() for line in raw.splitlines() if line.strip()]
                if field_type in {"list", "consumer_checks"}:
                    data[name] = lines
                elif field_type == "ports":
                    try:
                        data[name] = [int(line) for line in lines]
                    except ValueError as exc:
                        raise ServerError("Ports require integers, one per line") from exc
                else:
                    data[name] = [
                        {
                            "name": line.partition("|")[0].strip(),
                            "version": line.partition("|")[2].strip(),
                        }
                        for line in lines
                    ]
            else:
                data[name] = raw
        evidence = effective_evidence(kind, existing)
        # Unchanged fields retain their own dated evidence. New assertions share the form's stated source.
        for name in FIELDS[kind]:
            if data[name] != baseline.get(name):
                evidence[name] = {
                    "field": name,
                    "source": data["source"],
                    "checked_at": data["checked_at"],
                    "reason": data["reason"],
                    "basis": data["basis"],
                }
        data["evidence"] = list(evidence.values())
        mutate_map(
            connection,
            identity,
            revision,
            {
                "action": "upsert",
                "kind": kind,
                "object_id": fields.get("object_id", ""),
                "data": data,
            },
        )
    elif action in {"server_map_link", "server_map_unlink"}:
        if action == "server_map_unlink":
            if set(fields) - {"action", "server_id", "expected_revision", "link_key"}:
                raise ServerError("Invalid unlink form")
            mutate_map(
                connection,
                identity,
                revision,
                {"action": "unlink", "link_id": fields.get("link_key")},
            )
        else:
            allowed = {
                "action",
                "server_id",
                "expected_revision",
                "link_key",
                "from_kind",
                "from_id",
                "to_kind",
                "to_id",
                "to_object",
                "link_role",
                "link_source",
                "link_checked_at",
                "link_basis",
                "link_reason",
            }
            if set(fields) - allowed:
                raise ServerError("Invalid link form")
            value: dict[str, Any] = {
                k: fields.get(k, "") for k in ("from_kind", "from_id", "to_kind", "to_id")
            }
            if fields.get("to_object"):
                target_kind, separator, target_id = fields["to_object"].partition(":")
                if not separator or target_kind not in KINDS:
                    raise ServerError("Select a linked inventory object")
                value["to_kind"], value["to_id"] = target_kind, target_id
            value.update(
                {
                    "link_id": uuid4().hex
                    if fields.get("link_key") == "@new"
                    else fields.get("link_key", ""),
                    **{
                        k: fields.get("link_" + k, "")
                        for k in ("role", "source", "basis", "reason")
                    },
                    "checked_at": fields.get("link_checked_at") or None,
                }
            )
            mutate_map(connection, identity, revision, {"action": "link", "link": value})
    else:
        raise ServerError("Unknown inventory form action")


def values_for_new(kind: str) -> dict[str, Any]:
    return validate_object(
        kind,
        {
            "name": "New",
            **(
                {"path": "/"}
                if kind == "directory"
                else {"hostname": "example.test"}
                if kind == "domain"
                else {}
            ),
        },
    )


def _editor(
    card: dict[str, Any], kind: str, obj: dict[str, Any] | None, draft: dict[str, str], error: str
) -> str:
    from harness.dashboard_servers import _field, _group, _post

    key = obj["object_id"] if obj else "@new"
    failed = bool(error and draft.get("object_key") == key and draft.get("map_kind") == kind)
    selected = draft if failed and draft.get("action") == "server_map_save" else {}
    values = obj or validate_object(
        kind,
        {
            "name": "Новое приложение" if kind == "application" else "Новая запись",
            **(
                {"path": "/"}
                if kind == "directory"
                else {"hostname": "example.test"}
                if kind == "domain"
                else {}
            ),
        },
    )
    revision = (
        draft.get("expected_revision", str(card["revision"])) if failed else str(card["revision"])
    )
    content = (
        '<details class="server-panel server-edit-disclosure" id="map-editor-'
        + kind
        + "-"
        + escape(key, quote=True)
        + '"'
        + (" open" if failed or obj is None else "")
        + "><summary>"
        + ("Добавить запись" if obj is None else "Редактировать сведения")
        + "</summary>"
    )
    content += _post("server_map_save", card["server_id"], revision, key_name="object_key", key=key)
    content += '<input type="hidden" name="map_kind" value="' + kind + '">'
    content += _group(
        "Основные сведения",
        _field(
            "object_id",
            "Постоянный ID",
            obj["object_id"] if obj else uuid4().hex,
            selected,
            required=True,
            readonly=obj is not None,
        )
        + _field(
            "map_name", "Понятное название", values["name"] if obj else "", selected, required=True
        ),
    )
    rendered = ""
    for name, field_type in FIELDS[kind].items():
        value = values.get(name)
        if field_type in ENUMS:
            rendered += select(
                "map_" + name,
                label(name),
                str(value or "unknown"),
                ENUMS[field_type],
                selected,
                field=name,
            )
        elif field_type == "bool":
            rendered += select(
                "map_" + name,
                label(name),
                "true" if value else "false",
                ("false", "true"),
                selected,
            )
        else:
            rendered += _field(
                "map_" + name,
                label(name),
                value,
                selected,
                multiline=field_type in {"long", "list", "ports", "stack", "consumer_checks"},
                help_text="ISO 8601 с часовым поясом; пусто — неизвестно"
                if field_type == "date"
                else "По одному на строку"
                if field_type in {"list", "ports"}
                else "docker / systemd / cron / proxy / symlinks; только полностью проверенные источники, по одному на строку"
                if field_type == "consumer_checks"
                else "",
            )
    content += _group("Устройство и состояние", rendered)
    content += _group(
        "Основания и проверка",
        _field("map_reason", label("reason"), values.get("reason", ""), selected, multiline=True)
        + select(
            "map_basis",
            label("basis"),
            values.get("basis", "unknown"),
            BASES,
            selected,
            field="basis",
        )
        + _field(
            "map_source", label("source"), values.get("source", "operator"), selected, required=True
        )
        + _field(
            "map_checked_at",
            label("checked_at"),
            values.get("checked_at"),
            selected,
            help_text="ISO 8601 с часовым поясом. Дата относится к изменённым полям; неизменённые сохраняют свои основания.",
        ),
    )
    content += '<div class="server-form-footer"><p class="server-help">Ссылки и пути допускаются. Содержимое ключей, пароли, токены и сырые конфигурации не сохраняйте.</p><button class="btn btn-primary">Сохранить сведения</button><button type="reset" class="btn" data-server-cancel>Отменить изменения</button></div></form></details>'
    if obj:
        content += (
            '<details class="server-panel server-secondary"><summary>Удаление записи из реестра</summary><p class="server-help">Объекты со связями удалить нельзя. Это не удаляет файлы или приложения на сервере.</p>'
            + _post(
                "server_map_delete", card["server_id"], revision, key_name="object_key", key=key
            )
            + '<input type="hidden" name="map_kind" value="'
            + kind
            + '"><button class="btn danger">Удалить запись</button></form></details>'
        )
    return content


def role_label(value: str) -> str:
    roles = {
        "deployment": "Размещение",
        "runtime": "Запуск приложения",
        "endpoint": "Адрес проверки",
    }
    return ", ".join(roles.get(part.strip(), label(part.strip())) for part in value.split(","))


def _understanding(identity: str, obj: dict[str, Any]) -> str:
    """Explain the observed placement without substituting business intent."""
    content = '<section class="server-understanding"><h4>Что известно сейчас</h4><p><strong>'
    content += escape(label(obj["observed_status"])) + "</strong> · "
    content += escape(obj["observed_reason"] or "Основание текущего состояния не описано.") + "</p>"
    if obj["kind"] == "directory":
        content += "<p>Тип: " + escape(enum_label("entry_type", obj["entry_type_effective"]))
        content += "; роль: " + escape(role_label(obj["effective_role"])) + "; решение оператора: "
        content += escape(label(obj["usage"])) + ".</p>"
    for related_kind, title in (
        ("application", "Связанные приложения"),
        ("domain", "Доменные имена"),
        ("route", "Маршруты запросов"),
        ("service", "Запуск и зависимости"),
        ("directory", "Размещение и данные"),
    ):
        group = obj["related"][related_kind]
        if not group["total"]:
            continue
        content += "<p><strong>" + title + " (" + str(group["total"]) + ")</strong></p><ul>"
        for item in group["items"]:
            content += "<li>" + link(item["name"], href(identity, related_kind, item["object_id"]))
            content += " · " + escape(role_label(item["role"]))
            content += (
                " · "
                + ("связь проверена" if item["verified"] else "связь не подтверждена сейчас")
                + "</li>"
            )
        if group["total"] > len(group["items"]):
            content += (
                "<li>В обзоре показано "
                + str(len(group["items"]))
                + " из "
                + str(group["total"])
                + " связей; полный список ниже.</li>"
            )
        content += "</ul>"
    if obj["gaps"]:
        content += "<p><strong>Что ещё выяснить</strong></p><ul>"
        content += "".join("<li>" + escape(gap) + "</li>" for gap in obj["gaps"])
        content += "</ul>"
    return content + "</section>"


def _detail(
    connection: sqlite3.Connection,
    card: dict[str, Any],
    kind: str,
    key: str,
    draft: dict[str, str],
    error: str,
    filters: dict[str, str],
) -> str:
    from harness.dashboard_servers import _checked_time, _facts, _post

    obj = InventoryView(connection, card["server_id"]).enrich(
        object_detail(connection, card["server_id"], kind, key)
    )
    identity = card["server_id"]
    content = (
        '<article class="server-panel server-map-detail" id="map-'
        + kind
        + "-"
        + escape(key, quote=True)
        + '" tabindex="-1">'
        + link("← К списку " + LABELS[kind].lower(), href(identity, kind, **list_context(filters)))
        + "<h3>"
        + escape(obj["name"])
        + "</h3>"
    )
    content += _understanding(identity, obj)
    if kind == "application":
        content += (
            '<p class="server-map-description">'
            + escape(
                obj["description"]
                or "Устройство ещё не описано. Ниже перечислены известные связи; их актуальность указана отдельно."
            )
            + "</p>"
        )
        content += (
            '<nav class="server-map-related-collections" aria-label="Связанные списки приложения">'
            + " · ".join(
                link(LABELS[related_kind], href(identity, related_kind, app_id=key))
                for related_kind in (
                    "domain",
                    "route",
                    "directory",
                    "service",
                    "certificate",
                    "certificate_use",
                )
            )
            + "</nav>"
        )
    if kind == "certificate":
        context = certificate_context(connection, identity, key)
        content += _facts(
            [
                ("Срок текущей связанной версии", display_date(context["not_after"])),
                ("Связанные приложения", "\n".join(context["applications"])),
            ]
        )
    changed_availability = (
        "availability" in FIELDS[kind] and obj["effective_availability"] != obj["availability"]
    )
    if changed_availability:
        assessment = effective_evidence(kind, obj).get("availability", {})
        content += _facts(
            [
                ("Текущий вывод о доступности", label(obj["effective_availability"])),
                ("Сохранённая оценка доступности", label(obj["availability"])),
                ("Источник сохранённой оценки", assessment.get("source")),
                ("Дата сохранённой оценки", display_date(assessment.get("checked_at"))),
            ]
        )
        content += (
            '<p class="server-help">Текущий вывод учитывает границы проверки и связи; '
            "его причина указана в «Что известно сейчас». Сохранённая оценка из отчёта "
            "не изменена.</p>"
        )
    content += _facts(
        [
            (
                label(name),
                display_date(value)
                if FIELDS[kind].get(name) == "date"
                else display_value(value, name, FIELDS[kind].get(name, "")),
            )
            for name, value in ((name, obj.get(name)) for name in FIELDS[kind])
            if name
            not in {
                "kind",
                "object_id",
                "evidence",
                "name",
                "source",
                "checked_at",
                "basis",
                "reason",
            }
            and not (changed_availability and name == "availability")
        ]
    )
    content += (
        '<p class="server-list-note">'
        + escape(label(freshness(obj, server_policy(connection, identity))))
        + " · "
        + _checked_time(obj["checked_at"])
        + "</p>"
    )
    if problems(obj, server_policy(connection, identity)):
        content += (
            '<p class="server-error">'
            + escape(
                "; ".join(
                    problem_label(x, obj)
                    for x in problems(obj, server_policy(connection, identity))
                )
            )
            + "</p>"
        )
    content += '<details class="server-evidence-source"><summary>Источники, причины и даты отдельных сведений</summary>'
    content += _facts(
        [
            ("Общий источник", obj["source"]),
            ("Основание записи", label(obj["basis"])),
            ("Объяснение", obj["reason"]),
        ]
    )
    for e in obj["evidence"]:
        content += (
            "<p><strong>"
            + escape(label(e["field"]))
            + "</strong>: "
            + escape(
                label(e["basis"])
                + " · "
                + e["source"]
                + " · "
                + (e["checked_at"] or "Не проверено")
                + " · "
                + e["reason"]
            )
            + "</p>"
        )
    content += "</details><h4>Связи и назначение</h4>"
    relations = links_for(connection, identity, kind, key)
    if not relations:
        content += '<p class="server-help">Связи ещё не установлены. Отсутствие связи не доказывает, что объект не используется.</p>'
    for relation in relations:
        own_from = relation["from_kind"] == kind and relation["from_id"] == key
        related_kind, related_id = (
            (relation["to_kind"], relation["to_id"])
            if own_from
            else (relation["from_kind"], relation["from_id"])
        )
        related = object_detail(connection, identity, related_kind, related_id)
        content += (
            '<div class="server-map-relation">'
            + link(related["name"], href(identity, related_kind, related_id))
            + "<span>"
            + escape(role_label(relation["role"]))
            + "</span><small>"
            + escape(relation["reason"] or label(relation["basis"]))
            + " · "
            + escape(relation["source"])
            + " · "
            + _checked_time(relation["checked_at"])
            + "</small>"
        )
        content += (
            "<details"
            + (
                " open"
                if error
                and draft.get("action") == "server_map_link"
                and draft.get("link_key") == relation["link_id"]
                else ""
            )
            + "><summary>Изменить связь</summary>"
            + _link_editor(connection, card, kind, key, draft, error, relation)
            + "</details>"
        )
        content += (
            _post(
                "server_map_unlink",
                identity,
                draft.get("expected_revision", str(card["revision"]))
                if error
                and draft.get("action") == "server_map_unlink"
                and draft.get("link_key") == relation["link_id"]
                else str(card["revision"]),
                key_name="link_key",
                key=relation["link_id"],
            )
            + '<button class="btn">Убрать связь</button></form></div>'
        )
    content += (
        '<details class="server-secondary"'
        + (
            " open"
            if error
            and draft.get("action") == "server_map_link"
            and draft.get("link_key") == "@new"
            and (draft.get("from_kind"), draft.get("from_id")) == (kind, key)
            else ""
        )
        + "><summary>Добавить связь с приложением, каталогом, службой или HTTPS</summary>"
        + _link_editor(connection, card, kind, key, draft, error)
        + "</details>"
    )
    if kind == "certificate":
        content += '<p class="server-help">Выпуск, продление и применение проверяются отдельно. Наличие certbot или timer не подтверждает успешное продление и выдачу нового сертификата посетителю.</p>'
    if kind == "certificate_use":
        content += '<p class="server-help">Сравните версии в файле, конфигурации и фактическом ответе HTTPS. У внешнего прокси может быть другой сертификат.</p>'
    content += "</article>" + _editor(card, kind, obj, draft, error)
    return content


def _link_editor(
    connection: sqlite3.Connection,
    card: dict[str, Any],
    kind: str,
    key: str,
    draft: dict[str, str],
    error: str,
    existing: dict[str, Any] | None = None,
) -> str:
    from harness.dashboard_servers import _field, _post

    value = existing or {
        "link_id": "@new",
        "from_kind": kind,
        "from_id": key,
        "to_kind": "application" if kind != "application" else "domain",
        "to_id": "",
        "role": "uses",
        "source": "operator",
        "basis": "operator",
        "checked_at": None,
        "reason": "",
    }
    selected = (
        draft
        if error
        and draft.get("action") == "server_map_link"
        and draft.get("link_key") == value["link_id"]
        and draft.get("from_kind") == value["from_kind"]
        and draft.get("from_id") == value["from_id"]
        else {}
    )
    content = _post(
        "server_map_link",
        card["server_id"],
        selected.get("expected_revision", str(card["revision"])),
        key_name="link_key",
        key=value["link_id"],
    )
    content += (
        '<input type="hidden" name="from_kind" value="'
        + escape(value["from_kind"], quote=True)
        + '"><input type="hidden" name="from_id" value="'
        + escape(value["from_id"], quote=True)
        + '"><div class="server-form-grid">'
    )
    target = selected.get(
        "to_object", value["to_kind"] + ":" + value["to_id"] if value["to_id"] else ""
    )
    content += (
        '<label>Связанный объект<select name="to_object" required'
        + (' data-recovered-draft="true"' if "to_object" in selected else "")
        + ' data-server-default="'
        + escape(value["to_kind"] + ":" + value["to_id"] if value["to_id"] else "", quote=True)
        + '"'
        + '><option value=""'
        + (" selected" if not target else "")
        + ">Выберите запись</option>"
    )
    for target_kind in KINDS:
        available = all_objects(connection, card["server_id"], target_kind)
        if available:
            content += '<optgroup label="' + LABELS[target_kind] + '">'
            for item in available:
                option = target_kind + ":" + item["object_id"]
                content += (
                    '<option value="'
                    + escape(option, quote=True)
                    + '"'
                    + (" selected" if option == target else "")
                    + ">"
                    + escape(item["name"])
                    + "</option>"
                )
            content += "</optgroup>"
    content += "</select></label>"
    content += (
        _field(
            "link_role",
            "Для чего эта связь (например: код, загрузки, обслуживает)",
            value["role"],
            selected,
            required=True,
        )
        + _field("link_source", "Источник связи", value["source"], selected, required=True)
        + select("link_basis", "Основание связи", value["basis"], BASES, selected, field="basis")
        + _field("link_checked_at", "Дата проверки связи", value["checked_at"], selected)
        + _field(
            "link_reason", "Почему связь установлена", value["reason"], selected, multiline=True
        )
        + '</div><button class="btn btn-primary">Сохранить связь</button><button type="reset" class="btn" data-server-cancel>Отменить изменения связи</button>'
    )
    return content + "</form>"


def link_error_context(
    connection: sqlite3.Connection,
    identity: str,
    filters: dict[str, str],
    draft: dict[str, str],
) -> str:
    """Recover a rejected link form only on an endpoint in this server."""
    relation = connection.execute(
        "SELECT from_kind, from_id, to_kind, to_id FROM server_map_links "
        "WHERE server_id=? AND link_id=?",
        (identity, draft.get("link_key", "")),
    ).fetchone()
    endpoints = (
        [(relation[0], relation[1]), (relation[2], relation[3])]
        if relation
        else [(draft.get("from_kind", ""), draft.get("from_id", ""))]
    )
    selected = (filters.get("collection", ""), filters.get("object_id", ""))
    if selected in endpoints:
        endpoints = [selected]
    for kind, key in endpoints:
        if kind not in KINDS or not key:
            continue
        try:
            object_detail(connection, identity, kind, key)
        except ServerError:
            continue
        filters.update(view=VIEWS[kind], collection=kind, object_id=key)
        return VIEWS[kind]
    # Invalid input cannot select an unrelated or another server's object.
    filters.pop("object_id", None)
    return filters.get("view", "overview")


def _related_line(identity: str, obj: dict[str, Any]) -> str:
    kinds = ("application",) if obj["kind"] != "application" else ("domain", "service", "directory")
    content = ""
    for related_kind in kinds:
        group = obj["related"][related_kind]
        if not group["total"]:
            continue
        content += "<small>" + LABELS[related_kind] + ": "
        content += ", ".join(
            link(item["name"], href(identity, related_kind, item["object_id"]))
            for item in group["items"][:3]
        )
        if group["total"] > len(group["items"][:3]):
            content += " · всего " + str(group["total"])
        content += "</small>"
    return content


def render_collection(
    connection: sqlite3.Connection,
    card: dict[str, Any],
    view: str,
    filters: dict[str, str],
    draft: dict[str, str],
    error: str,
) -> str:
    from harness.dashboard_servers import _checked_time

    kind = filters.get("collection", DEFAULT_KINDS[view])
    if kind not in KINDS or VIEWS[kind] != view:
        kind = DEFAULT_KINDS[view]
    if error and draft.get("map_kind") in KINDS:
        kind = draft["map_kind"]
    identity = card["server_id"]
    content = (
        '<section class="server-view" id="server-'
        + view
        + '" data-server-view="'
        + view
        + '"><div class="server-section-heading"><div><h2>'
        + LABELS[kind]
        + "</h2><p>Все известные записи. Источники и даты показывают границы нашей картины.</p></div>"
        + link("Добавить запись", href(identity, kind, "@new", **list_context(filters)))
        + "</div>"
    )
    if view in {"certificates", "domains"}:
        subtypes = (
            ("domain", "route")
            if view == "domains"
            else ("certificate", "certificate_version", "certificate_use")
        )
        content += (
            '<nav class="server-list-tabs" aria-label="Тип записи">'
            + "".join(link(LABELS[subtype], href(identity, subtype)) for subtype in subtypes)
            + "</nav>"
        )
    query = filters.get("map_q", filters.get("sites_q", "") if kind == "application" else "")
    status = filters.get("map_status", "")
    sort = filters.get("map_sort", "name")
    order = filters.get("map_order", "asc")
    size = filters.get("map_size", "all")
    if size not in {"all", "25", "50", "100"} or order not in {"asc", "desc"}:
        raise ServerError("Invalid map display option")
    rows, total = collection_rows(
        connection,
        identity,
        kind,
        query=query,
        status=status,
        application_id=filters.get("app_id", ""),
        sort=sort,
        descending=order == "desc",
    )
    offset = int(filters.get("map_offset", "0"))
    if not 0 <= offset <= 10000:
        raise ServerError("Invalid map offset")
    limit = len(rows) if size == "all" else int(size)
    if size == "all":
        offset = 0
    elif offset >= len(rows):
        offset = max(0, ((len(rows) - 1) // limit) * limit)
        filters["map_offset"] = str(offset)
    selected = rows[offset : offset + limit]
    content += (
        '<form method="get" data-server-search class="server-filters" action="#server-'
        + view
        + '"><input type="hidden" name="view" value="'
        + view
        + '"><input type="hidden" name="collection" value="'
        + kind
        + '"><label>Поиск по всему списку<input type="search" name="map_q" value="'
        + escape(query, quote=True)
        + '"></label>'
    )
    states = tuple(
        dict.fromkeys(
            [
                "attention",
                "incomplete",
                "unknown",
                "stale",
                *(["current", "configured"] if kind in {"directory", "application"} else []),
                *(["running"] if kind == "application" else []),
                *(["unconfirmed"] if kind in {"application", "domain", "route"} else []),
                *(
                    ["directory", "file", "socket", "symlink", "other", "non_directory_unconfirmed"]
                    if kind == "directory"
                    else []
                ),
                *[
                    x
                    for e in FIELDS[kind].values()
                    if e in ENUMS
                    for x in ENUMS[e]
                    if e
                    in {
                        "usage",
                        "directory_usage",
                        "availability",
                        "service_state",
                        "renewal_state",
                        "tls",
                    }
                ],
            ]
        )
    )
    content += (
        select(
            "map_status",
            "Состояние / тип пути" if kind == "directory" else "Состояние",
            status,
            states,
            {},
            blank="Все состояния",
        )
        + select(
            "map_sort",
            "Сортировать по",
            sort,
            ("name", "status", "environment", "size_bytes", "not_after", "checked_at"),
            {},
        )
        + select("map_order", "Порядок", order, ("asc", "desc"), {})
        + select("map_size", "Показать", size, ("all", "25", "50", "100"), {})
    )
    if filters.get("app_id"):
        content += (
            '<input type="hidden" name="app_id" value="'
            + escape(filters["app_id"], quote=True)
            + '">'
            + link(
                "Снять фильтр приложения",
                href(
                    identity,
                    kind,
                    **{k: v for k, v in list_context(filters).items() if k != "app_id"},
                ),
            )
        )
    content += '<button class="btn">Применить</button><p role="status" class="server-filter-status" data-server-filter-status></p></form>'
    content += (
        '<p class="server-list-note" data-map-total="'
        + str(total)
        + '" data-map-matched="'
        + str(len(rows))
        + '">'
        + ("Все " if size == "all" else "Показано ")
        + str(len(selected))
        + " · совпадений: "
        + str(len(rows))
        + " · всего записей: "
        + str(total)
        + "</p>"
    )
    key = (
        filters.get("object_id")
        or (draft.get("object_key") if error else None)
        or filters.get("site_id")
        if kind == "application"
        else filters.get("object_id") or (draft.get("object_key") if error else None)
    )
    if key:
        if key == "@new":
            content += _editor(card, kind, None, draft, error)
        else:
            content += _detail(connection, card, kind, key, draft, error, filters)
    content += '<div class="server-directory-panel server-map-list"><div class="server-table"><table><thead><tr><th>Объект и назначение</th><th>Состояние</th><th>Контекст</th><th>Основание и проверка</th></tr></thead><tbody>'
    for obj in selected:
        context = (
            obj.get("environment")
            or obj.get("destination")
            or obj.get("role")
            or obj.get("renewal")
            or obj.get("termination")
            or obj.get("not_after")
            or obj.get("manager")
            or "unknown"
        )
        if kind == "directory":
            context = (
                enum_label("entry_type", obj["entry_type_effective"])
                + " · "
                + role_label(obj["effective_role"])
            )
            context += " · " + (
                str(obj["size_bytes"]) + " байт"
                if obj["size_bytes"] is not None
                else "размер не измерен"
            )
        if kind == "certificate":
            context = (
                "Продление: " + label(obj["renewal"]) + "; применение: " + label(obj["deployment"])
            )
        state = "; ".join(problem_label(x, obj) for x in problems(obj)) or label(status_of(obj))
        if kind == "application":
            state += " · Доступность: " + label(obj["effective_availability"])
        if kind == "certificate":
            context += "; срок текущей версии: " + display_date(obj.get("not_after"))
        content += (
            '<tr><td data-label="Объект">'
            + link(obj["name"], href(identity, kind, obj["object_id"], **list_context(filters)))
            + "<small>"
            + escape(
                obj.get("purpose")
                or obj.get("hostname")
                or obj.get("path")
                or obj.get("identifier")
                or ""
            )
            + "</small>"
            + _related_line(identity, obj)
            + '</td><td data-label="Состояние">'
            + escape(state)
            + '</td><td data-label="Контекст">'
            + escape(label(context))
            + '</td><td data-label="Основание">'
            + escape(obj["observed_reason"] or obj["reason"] or label(obj["basis"]))
            + "<small>"
            + _checked_time(obj["checked_at"])
            + " · "
            + escape(label(freshness(obj, server_policy(connection, identity))))
            + "</small></td></tr>"
        )
    content += "</tbody></table></div></div>"
    if not selected:
        content += '<div class="server-empty"><h3>Записей по этим условиям нет</h3><p>Измените фильтр или добавьте сведения. Отсутствие записи не означает, что объект отсутствует на сервере.</p></div>'
    if size != "all":
        content += '<nav class="server-pagination" aria-label="Страницы списка">'
        for position, title in ((max(0, offset - limit), "← Назад"), (offset + limit, "Далее →")):
            if (title.startswith("←") and offset) or (
                not title.startswith("←") and position < len(rows)
            ):
                content += link(
                    title,
                    href(
                        identity,
                        kind,
                        **{
                            k: v
                            for k, v in filters.items()
                            if k not in {"view", "collection", "object_id", "map_offset"}
                        },
                        map_offset=str(position),
                    ),
                )
        content += "</nav>"
    return content + "</section>"


def render_summary(connection: sqlite3.Connection, card: dict[str, Any]) -> str:
    from harness.dashboard_servers import _checked_time

    identity = card["server_id"]
    inventory = InventoryView(connection, identity)
    collections = {
        kind: [
            inventory.enrich(obj)
            for (obj_kind, _), obj in inventory.objects.items()
            if obj_kind == kind
        ]
        for kind in KINDS
    }
    applications = collections["application"]
    objects = [obj for rows in collections.values() for obj in rows]
    policy = server_policy(connection, identity)
    issues = [obj for obj in objects if problems(obj, policy)]
    unknowns = [
        obj
        for obj in objects
        if status_of(obj) == "unknown" or not obj["checked_at"] or obj.get("gaps")
    ]
    content = (
        '<section class="server-map-summary"><div class="server-section-heading"><div>'
        "<h2>Что размещено и что требует внимания</h2><p>Это известная картина реестра. "
        "Открытие карточки не запускает проверку сервера.</p></div></div>"
        '<div class="server-map-summary-grid"><section class="server-panel server-summary-placement">'
        '<nav class="server-inventory-counts" aria-label="Объекты на сервере">'
    )
    for kind in ("application", "domain", "route", "directory", "service", "certificate"):
        content += (
            '<a data-server-navigation href="'
            + escape(href(identity, kind), quote=True)
            + '"><span>'
            + LABELS[kind]
            + "</span><strong>"
            + str(len(collections[kind]))
            + "</strong></a>"
        )
    content += (
        '</nav><div class="server-summary-heading"><h3>Приложения</h3>'
        + link("Все приложения →", href(identity, "application"))
        + '</div><ul class="server-summary-apps">'
    )
    for obj in applications[:6]:
        purpose = obj["purpose"] or "Назначение не выяснено"
        content += (
            '<li><div class="server-summary-app-heading">'
            + link(obj["name"], href(identity, "application", obj["object_id"]))
            + '<span class="server-summary-usage">'
            + escape(label(status_of(obj)))
            + '</span></div><p title="'
            + escape(purpose, quote=True)
            + '">'
            + escape(purpose)
            + "</p>"
            + _related_line(identity, obj)
            + "</li>"
        )
    content += "</ul>"
    if not applications:
        content += '<p class="server-summary-empty">Приложения ещё не описаны.</p>'
    elif len(applications) > 6:
        content += (
            '<p class="server-summary-list-note">Первые 6 из '
            + str(len(applications))
            + " · "
            + link("Открыть полный список", href(identity, "application"))
            + "</p>"
        )
    content += (
        '</section><div class="server-summary-sidebar"><section class="server-panel server-summary-attention">'
        '<div class="server-summary-heading"><h3>Требует внимания</h3><span class="server-summary-count">'
        + str(len(issues))
        + "</span></div>"
    )
    content += (
        '<ul class="server-summary-issues">'
        + "".join(
            "<li>"
            + link(obj["name"], href(identity, obj["kind"], obj["object_id"]))
            + "<p>"
            + escape("; ".join(problem_label(x, obj) for x in problems(obj, policy)))
            + ".</p></li>"
            for obj in issues
        )
        + "</ul>"
        if issues
        else '<p class="server-summary-empty">В известных результатах проблем не отмечено. Непроверенное не означает исправное.</p>'
    )
    content += (
        '</section><section class="server-panel server-summary-unknown">'
        '<div class="server-summary-heading"><h3>Что ещё не выяснено</h3><span class="server-summary-count">'
        + str(len(unknowns))
        + '</span></div><ul class="server-summary-unknowns">'
        + "".join(
            "<li>"
            + link(LABELS[kind], href(identity, kind, map_status="incomplete"))
            + "<strong>"
            + str(sum(obj["kind"] == kind for obj in unknowns))
            + "</strong></li>"
            for kind in KINDS
            if any(obj["kind"] == kind for obj in unknowns)
        )
        + "</ul>"
        + (
            '<p class="server-summary-empty">'
            + (
                "Состояние и дата проверки указаны для всех записей."
                if objects
                else "В реестре пока нет объектов для оценки."
            )
            + "</p>"
            if not unknowns
            else ""
        )
        + '</section></div></div><div class="server-panel server-summary-settings">'
    )
    checks = connection.execute(
        "SELECT check_id,state,data FROM server_map_checks WHERE server_id=? ORDER BY created_at DESC LIMIT 5",
        (identity,),
    ).fetchall()
    content += '<details class="server-evidence-source"><summary>Охват последних проверок</summary>'
    if not checks:
        content += "<p>Структурированная инвентаризация ещё не записана. Старые ссылки не подтверждают актуальное размещение.</p>"
    import json

    for key, state, raw in checks:
        data = json.loads(raw)
        content += (
            "<p><strong>"
            + escape(key)
            + "</strong> · "
            + escape(label(state))
            + " · "
            + _checked_time(data.get("finished_at") or data.get("started_at"))
            + "</p><ul>"
            + "".join(
                "<li>"
                + escape(
                    LABELS[item["area"]]
                    + ": "
                    + label(item["state"])
                    + " · "
                    + item["source"]
                    + " · "
                    + item["reason"]
                )
                + "</li>"
                for item in data["coverage"]
            )
            + "</ul>"
        )
    from harness.dashboard_servers import _field, _post

    content += '</details><details class="server-secondary"><summary>Когда сведения считаются устаревшими и когда предупреждать о сроке</summary>'
    content += (
        '<p class="server-help">Доступность, DNS и HTTPS: '
        + str(policy["health_hours"])
        + " ч; размещение и связи: "
        + str(policy["inventory_hours"])
        + " ч; паспорт: "
        + str(policy["passport_days"])
        + " дней. Сроки предупреждений: "
        + ", ".join(map(str, policy["expiry_days"]))
        + " дней. Это правила отображения; удалённые проверки не запускаются.</p>"
    )
    content += (
        _post("server_map_policy_save", identity, str(card["revision"]))
        + '<div class="server-form-grid">'
    )
    for field, title in (
        ("health_hours", "Доступность / DNS / HTTPS, часы"),
        ("inventory_hours", "Размещение и связи, часы"),
        ("passport_days", "Паспорт, дни"),
    ):
        content += _field(field, title, policy[field], {})
    content += _field(
        "expiry_days",
        "Предупреждать за дни (через запятую)",
        ", ".join(map(str, policy["expiry_days"])),
        {},
    )
    return (
        content
        + '</div><button class="btn">Сохранить правила давности</button></form></details></div></section>'
    )
