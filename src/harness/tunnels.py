# ruff: noqa: RUF001
"""Human-operated tunnel profiles and operations, independent of server inventory.

Only non-secret settings and bounded observations enter SQLite. The network service
owns credentials, applied configurations and kernel policy; a requested connection
is never evidence that its protection is active.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

MAX_PROFILES = 32
MAX_RULES = 256
MAX_EVENTS = 200
MAX_JSON_BYTES = 32 * 1024
TRANSPORTS = ("naive", "reality", "hysteria2")
STATES = ("disconnected", "preparing", "connecting", "connected", "reconnecting", "blocked")
PROTECTION = ("inactive", "active", "unavailable", "unknown")
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class TunnelError(ValueError):
    """Bounded operator-facing error without rejected input or subprocess output."""


class TunnelConflict(TunnelError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def identity(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TunnelError("Некорректный идентификатор подключения")
    return value


def text(value: object, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise TunnelError("Проверьте текстовые поля")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise TunnelError("Проверьте текстовые поля") from exc
    if size > maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise TunnelError("Превышен размер поля или недопустимые символы")
    return value.strip()


def revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TunnelError("Нужна актуальная ревизия подключения")
    return value


def endpoint(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"host", "user", "port"}:
        raise TunnelError("Проверьте адрес и параметры SSH")
    try:
        host = str(ipaddress.ip_address(text(value.get("host"), 64)))
    except ValueError as exc:
        raise TunnelError("Укажите IPv4 или IPv6 сервера") from exc
    address = ipaddress.ip_address(host)
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
    ):
        raise TunnelError("Нужен маршрутизируемый IP сервера")
    user = value.get("user", "root")
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise TunnelError("Проверьте имя пользователя SSH")
    port = value.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise TunnelError("Порт SSH должен быть от 1 до 65535")
    return {"host": host, "user": user, "port": port}


def normalize_rules(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise TunnelError("Допускается до 256 исключений")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, str):
            raise TunnelError("Каждое исключение должно быть адресом или доменом")
        raw = text(item, 256).lower().rstrip(".")
        if not raw:
            continue
        try:
            address = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            # A bare domain deliberately includes its subdomains. Paths, regex,
            # ports and URL schemes are not silently reinterpreted as domains.
            if raw.startswith("*."):
                raw = raw[2:]
            try:
                domain = raw.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise TunnelError("Проверьте доменные исключения") from exc
            if (
                len(domain) > 253
                or "." not in domain
                or not all(_DOMAIN.fullmatch(label) for label in domain.split("."))
            ):
                raise TunnelError("Укажите домен без протокола, пути и порта") from None
            kind, normalized = "domain", domain
        else:
            if address.is_multicast or address.is_unspecified or address.prefixlen == 0:
                raise TunnelError("Исключение не должно отключать весь туннель")
            kind, normalized = "cidr", str(address)
        key = kind, normalized
        if key not in seen:
            result.append({"kind": kind, "value": normalized})
            seen.add(key)
    return result


def validate_profile(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "name",
        "endpoints",
        "exceptions",
        "lan_bypass",
    }:
        raise TunnelError("Неизвестные поля подключения")
    name = text(value.get("name"))
    if not name:
        raise TunnelError("Укажите название подключения")
    endpoints = value.get("endpoints")
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 2:
        raise TunnelError("Укажите основной и, при необходимости, резервный VPS")
    normalized = [endpoint(item) for item in endpoints]
    if len({item["host"] for item in normalized}) != len(normalized):
        raise TunnelError("Основной и резервный VPS должны иметь разные адреса")
    lan = value.get("lan_bypass", True)
    if type(lan) is not bool:
        raise TunnelError("Проверьте исключение локальной сети")
    rules = normalize_rules(value.get("exceptions", []))
    result = {"name": name, "endpoints": normalized, "exceptions": rules, "lan_bypass": lan}
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_JSON_BYTES:
        raise TunnelError("Подключение превышает допустимый размер")
    return result


def migrate(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE tunnel_profiles (
            id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision>0),
            data TEXT NOT NULL CHECK(json_valid(data)), created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE tunnel_runtime (
            profile_id TEXT PRIMARY KEY REFERENCES tunnel_profiles(id) ON DELETE CASCADE,
            state TEXT NOT NULL, protection TEXT NOT NULL, desired INTEGER NOT NULL DEFAULT 0,
            snapshot TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)""",
        """CREATE TABLE tunnel_operations (
            id TEXT PRIMARY KEY, profile_id TEXT NOT NULL
            REFERENCES tunnel_profiles(id) ON DELETE CASCADE,
            action TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT NOT NULL, finished_at TEXT)""",
        """CREATE UNIQUE INDEX tunnel_one_pending_operation
            ON tunnel_operations((1)) WHERE state='pending'""",
        """CREATE TABLE tunnel_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL REFERENCES tunnel_profiles(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE tunnel_metrics (
            profile_id TEXT NOT NULL REFERENCES tunnel_profiles(id) ON DELETE CASCADE,
            minute TEXT NOT NULL, rx_bytes INTEGER NOT NULL CHECK(rx_bytes>=0),
            tx_bytes INTEGER NOT NULL CHECK(tx_bytes>=0), latency_ms REAL,
            PRIMARY KEY(profile_id,minute))""",
        """CREATE TABLE tunnel_machine (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL,
            active_profile TEXT REFERENCES tunnel_profiles(id))""",
        "INSERT INTO tunnel_machine VALUES(1,0,NULL)",
    )
    for statement in statements:
        connection.execute(statement)


def _event(connection: sqlite3.Connection, profile_id: str, kind: str, message: str) -> None:
    connection.execute(
        "INSERT INTO tunnel_events(profile_id,kind,message,created_at) VALUES(?,?,?,?)",
        (profile_id, kind, text(message, 512), now()),
    )
    connection.execute(
        "DELETE FROM tunnel_events WHERE profile_id=? AND sequence NOT IN "
        "(SELECT sequence FROM tunnel_events WHERE profile_id=? ORDER BY sequence DESC LIMIT ?)",
        (profile_id, profile_id, MAX_EVENTS),
    )


def get_profile(connection: sqlite3.Connection, profile_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT id,revision,data,created_at,updated_at FROM tunnel_profiles WHERE id=?",
        (identity(profile_id),),
    ).fetchone()
    if row is None:
        raise TunnelError("Подключение не найдено")
    runtime = connection.execute(
        "SELECT state,protection,desired,snapshot,updated_at FROM tunnel_runtime WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    return {
        "id": row[0],
        "revision": row[1],
        "settings": json.loads(row[2]),
        "created_at": row[3],
        "updated_at": row[4],
        "runtime": {
            "state": runtime[0],
            "protection": runtime[1],
            "desired": bool(runtime[2]),
            "snapshot": json.loads(runtime[3]),
            "updated_at": runtime[4],
        },
    }


def list_profiles(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        get_profile(connection, row[0])
        for row in connection.execute(
            "SELECT id FROM tunnel_profiles ORDER BY created_at,id LIMIT ?", (MAX_PROFILES,)
        ).fetchall()
    ]


def save_profile(
    connection: sqlite3.Connection,
    profile_id: str,
    expected_revision: int,
    settings: object,
) -> dict[str, Any]:
    profile_id, expected_revision = identity(profile_id), revision(expected_revision)
    data = validate_profile(settings)
    stamp = now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT revision,data FROM tunnel_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        if (0 if row is None else row[0]) != expected_revision:
            raise TunnelConflict("Подключение изменилось. Обновите данные перед сохранением.")
        pending = connection.execute(
            "SELECT 1 FROM tunnel_operations WHERE profile_id=? AND state='pending'", (profile_id,)
        ).fetchone()
        if pending:
            raise TunnelConflict("Дождитесь завершения операции")
        if row is None:
            if (
                connection.execute("SELECT count(*) FROM tunnel_profiles").fetchone()[0]
                >= MAX_PROFILES
            ):
                raise TunnelError("Допускается до 32 подключений")
            connection.execute(
                "INSERT INTO tunnel_profiles VALUES(?,?,?,?,?)",
                (profile_id, 1, json.dumps(data), stamp, stamp),
            )
            connection.execute(
                "INSERT INTO tunnel_runtime(profile_id,state,protection,updated_at) VALUES(?,?,?,?)",
                (profile_id, "disconnected", "inactive", stamp),
            )
        else:
            runtime = connection.execute(
                "SELECT state,protection,snapshot FROM tunnel_runtime WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if runtime[0] != "disconnected" or runtime[1] != "inactive":
                raise TunnelConflict("Отключите туннель перед изменением его настроек")
            if (
                any(
                    json.loads(runtime[2]).get(key)
                    for key in ("prepared", "setup_incomplete", "removal_pending")
                )
                and json.loads(row[1])["endpoints"] != data["endpoints"]
            ):
                raise TunnelConflict(
                    "Удалите серверную часть перед изменением адресов VPS или SSH-доступа"
                )
            connection.execute(
                "UPDATE tunnel_profiles SET revision=revision+1,data=?,updated_at=? WHERE id=?",
                (json.dumps(data), stamp, profile_id),
            )
        _event(connection, profile_id, "settings", "Настройки подключения сохранены")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return get_profile(connection, profile_id)


def delete_profile(connection: sqlite3.Connection, profile_id: str, expected_revision: int) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = get_profile(connection, profile_id)
        if current["revision"] != revision(expected_revision):
            raise TunnelConflict("Подключение изменилось. Обновите данные.")
        runtime = current["runtime"]
        if runtime["state"] != "disconnected" or runtime["protection"] != "inactive":
            raise TunnelConflict("Сначала отключите туннель")
        if connection.execute(
            "SELECT 1 FROM tunnel_operations WHERE profile_id=? AND state='pending'", (profile_id,)
        ).fetchone():
            raise TunnelConflict("Дождитесь завершения операции")
        if any(
            runtime["snapshot"].get(key)
            for key in ("prepared", "setup_incomplete", "removal_pending")
        ):
            raise TunnelConflict("Сначала удалите серверную часть кнопкой «Удалить с VPS»")
        connection.execute("DELETE FROM tunnel_profiles WHERE id=?", (profile_id,))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def begin_operation(
    connection: sqlite3.Connection,
    profile_id: str,
    expected_revision: int,
    action: str,
) -> dict[str, Any]:
    if action not in {"prepare", "connect", "disconnect", "check", "remove"}:
        raise TunnelError("Неизвестная операция туннеля")
    operation_id = uuid4().hex
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = get_profile(connection, profile_id)
        if current["revision"] != revision(expected_revision):
            raise TunnelConflict("Подключение изменилось. Обновите данные.")
        if connection.execute("SELECT 1 FROM tunnel_operations WHERE state='pending'").fetchone():
            raise TunnelConflict("Дождитесь завершения текущей сетевой операции")
        if action in {"remove", "prepare"} and current["runtime"]["state"] != "disconnected":
            raise TunnelConflict("Сначала отключите туннель")
        connection.execute(
            "INSERT INTO tunnel_operations VALUES(?,?,?,?,?,?,NULL)",
            (operation_id, profile_id, action, "pending", "Операция принята", now()),
        )
        connection.execute(
            "UPDATE tunnel_profiles SET revision=revision+1,updated_at=? WHERE id=?",
            (now(), profile_id),
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return {"id": operation_id, "profile_id": profile_id, "action": action}


_OBSERVATIONS = {
    "endpoint",
    "transport",
    "udp_transport",
    "external_ip",
    "latency_ms",
    "rx_bytes",
    "tx_bytes",
    "rx_bps",
    "tx_bps",
    "connected_at",
    "reconnects",
    "prepared",
    "setup_incomplete",
    "removal_pending",
    "last_error",
    "lan_routes",
    "tcp_ok",
    "udp_ok",
    "dns_ok",
    "bulk_ok",
    "applied_revision",
    "bundle_version",
}


def sanitize_snapshot(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or set(snapshot) - _OBSERVATIONS:
        raise TunnelError("Служба вернула неподдерживаемые показатели")
    result: dict[str, Any] = {}
    for key, value in snapshot.items():
        if key in {"rx_bytes", "tx_bytes", "rx_bps", "tx_bps", "reconnects", "applied_revision"}:
            if type(value) is not int or not 0 <= value <= 2**63 - 1:
                raise TunnelError("Некорректные счётчики службы")
        elif key == "latency_ms":
            if value is not None and (type(value) not in {int, float} or not 0 <= value <= 120000):
                raise TunnelError("Некорректная задержка службы")
        elif key in {
            "prepared",
            "setup_incomplete",
            "removal_pending",
            "tcp_ok",
            "udp_ok",
            "dns_ok",
            "bulk_ok",
        }:
            if type(value) is not bool:
                raise TunnelError("Некорректный результат проверки службы")
        elif key == "lan_routes":
            if not isinstance(value, list) or len(value) > 32:
                raise TunnelError("Некорректные локальные маршруты")
            try:
                value = [str(ipaddress.ip_network(item, strict=False)) for item in value]
            except (ValueError, TypeError) as exc:
                raise TunnelError("Некорректные локальные маршруты") from exc
        elif key in {"endpoint", "external_ip"}:
            try:
                value = str(ipaddress.ip_address(value)) if value else ""
            except ValueError as exc:
                raise TunnelError("Некорректный адрес в показателях") from exc
        elif key in {"transport", "udp_transport"}:
            if value not in {*TRANSPORTS, "reality-udp-over-tcp", "naive-udp-over-tcp", ""}:
                raise TunnelError("Некорректный транспорт службы")
        else:
            value = text(value, 512)
        result[key] = value
    return result


def unavailable_snapshot(snapshot: object) -> dict[str, Any]:
    """Keep last-known aggregates, but never present an unavailable current rate."""
    clean = sanitize_snapshot(snapshot)
    for key in ("rx_bps", "tx_bps"):
        clean.pop(key, None)
    return clean


def finish_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    success: bool,
    message: str,
    state: str | None = None,
    protection: str | None = None,
    snapshot: object = None,
) -> None:
    clean = sanitize_snapshot({} if snapshot is None else snapshot)
    if (state is not None and state not in STATES) or (
        protection is not None and protection not in PROTECTION
    ):
        raise TunnelError("Некорректное состояние службы")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT profile_id,action,state FROM tunnel_operations WHERE id=?",
            (identity(operation_id),),
        ).fetchone()
        if row is None or row[2] != "pending":
            raise TunnelConflict("Операция уже завершена")
        profile_id, action = row[0], row[1]
        current = get_profile(connection, profile_id)["runtime"]
        clean = (
            unavailable_snapshot(current["snapshot"])
            if state is None and protection is None
            else clean
        )
        actual_state = current["state"] if state is None else state
        actual_protection = current["protection"] if protection is None else protection
        if actual_state == "connected" and (
            actual_protection != "active"
            or not all(clean.get(key) for key in ("tcp_ok", "dns_ok", "bulk_ok"))
        ):
            raise TunnelError("Соединение ещё не прошло проверку трафика и защиты")
        connection.execute(
            "UPDATE tunnel_operations SET state=?,message=?,finished_at=? WHERE id=?",
            ("succeeded" if success else "failed", text(message, 512), now(), operation_id),
        )
        desired = current["desired"]
        if action in {"connect", "prepare"} and actual_protection == "active":
            desired = True
            connection.execute(
                "UPDATE tunnel_runtime SET state='disconnected',protection='inactive',desired=0 "
                "WHERE profile_id<>?",
                (profile_id,),
            )
            connection.execute(
                "UPDATE tunnel_machine SET active_profile=?,generation=generation+1 WHERE singleton=1",
                (profile_id,),
            )
        elif success and action == "disconnect":
            if actual_state != "disconnected" or actual_protection != "inactive":
                raise TunnelError("Отключение ещё не подтверждено службой")
            desired = False
            connection.execute(
                "UPDATE tunnel_machine SET active_profile=NULL,generation=generation+1 WHERE singleton=1"
            )
        connection.execute(
            "UPDATE tunnel_runtime SET state=?,protection=?,desired=?,snapshot=?,updated_at=? WHERE profile_id=?",
            (actual_state, actual_protection, int(desired), json.dumps(clean), now(), profile_id),
        )
        connection.execute(
            "DELETE FROM tunnel_operations WHERE profile_id=? AND state<>'pending' AND id NOT IN "
            "(SELECT id FROM tunnel_operations WHERE profile_id=? ORDER BY created_at DESC,rowid DESC LIMIT 50)",
            (profile_id, profile_id),
        )
        _event(connection, profile_id, action, message)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def observations(connection: sqlite3.Connection, profile_id: str) -> dict[str, Any]:
    profile = get_profile(connection, profile_id)
    events = connection.execute(
        "SELECT kind,message,created_at FROM tunnel_events WHERE profile_id=? "
        "ORDER BY sequence DESC LIMIT 50",
        (profile_id,),
    ).fetchall()
    operations = connection.execute(
        "SELECT id,action,state,message,created_at,finished_at FROM tunnel_operations "
        "WHERE profile_id=? ORDER BY created_at DESC,rowid DESC LIMIT 10",
        (profile_id,),
    ).fetchall()
    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="minutes")
    metrics = connection.execute(
        "SELECT minute,rx_bytes,tx_bytes,latency_ms FROM tunnel_metrics "
        "WHERE profile_id=? AND minute>=? ORDER BY minute",
        (profile_id, since),
    ).fetchall()
    return {
        **profile,
        "events": [{"kind": row[0], "message": row[1], "created_at": row[2]} for row in events],
        "operations": [
            dict(
                zip(
                    ("id", "action", "state", "message", "created_at", "finished_at"),
                    row,
                    strict=True,
                )
            )
            for row in operations
        ],
        "metrics": [
            dict(zip(("minute", "rx_bytes", "tx_bytes", "latency_ms"), row, strict=True))
            for row in metrics
        ],
    }


def validate_credentials(value: object) -> dict[str, str]:
    if value is None:
        return {}
    base = {"password", "sudo_password", "private_key", "key_passphrase"}
    allowed = base | {"backup_" + key for key in base}
    if not isinstance(value, dict) or set(value) - allowed:
        raise TunnelError("Проверьте поля SSH-доступа")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise TunnelError("Проверьте поля SSH-доступа")
        try:
            size = len(item.encode())
        except UnicodeError as exc:
            raise TunnelError("Проверьте поля SSH-доступа") from exc
        if size > (16 * 1024 if key.endswith("private_key") else 1024) or "\x00" in item:
            raise TunnelError("SSH-доступ превышает допустимый размер")
        if not key.endswith("private_key") and ("\n" in item or "\r" in item):
            raise TunnelError("Пароль SSH или sudo должен занимать одну строку")
        if item:
            result[key] = item
    return result


def observe_runtime(
    connection: sqlite3.Connection,
    profile_id: str,
    state: str,
    protection: str,
    snapshot: object,
) -> None:
    clean = sanitize_snapshot(snapshot)
    if state not in STATES or protection not in PROTECTION:
        raise TunnelError("Некорректное состояние сетевой службы")
    if state == "connected" and (
        protection != "active" or not all(clean.get(key) for key in ("tcp_ok", "dns_ok", "bulk_ok"))
    ):
        raise TunnelError("Служба не подтвердила проверку соединения")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = get_profile(connection, profile_id)["runtime"]
        previous = current["snapshot"]
        merged = clean
        connection.execute(
            "UPDATE tunnel_runtime SET state=?,protection=?,desired=?,snapshot=?,updated_at=? WHERE profile_id=?",
            (
                state,
                protection,
                int(state != "disconnected" or protection == "active"),
                json.dumps(merged),
                now(),
                profile_id,
            ),
        )
        if state != "disconnected" or protection == "active":
            connection.execute(
                "UPDATE tunnel_machine SET active_profile=? WHERE singleton=1", (profile_id,)
            )
            connection.execute(
                "UPDATE tunnel_runtime SET state='disconnected',protection='inactive',desired=0 WHERE profile_id<>? AND desired=1",
                (profile_id,),
            )
        else:
            connection.execute(
                "UPDATE tunnel_machine SET active_profile=NULL WHERE active_profile=?",
                (profile_id,),
            )
        if state != current["state"] or clean.get("transport") != previous.get("transport"):
            protection_message = {
                "active": "защита сохраняется",
                "unknown": "защита не подтверждена",
                "inactive": "защита неактивна",
                "unavailable": "защита недоступна",
            }[protection]
            _event(
                connection,
                profile_id,
                "connection",
                {
                    "connected": "Соединение и защита трафика подтверждены",
                    "reconnecting": f"Соединение восстанавливается; {protection_message}",
                    "blocked": "Рабочий путь не подтверждён; "
                    + (
                        "защищённый трафик заблокирован"
                        if protection == "active"
                        else protection_message
                    ),
                    "connecting": "Проверка соединения",
                    "preparing": "Подготовка VPS",
                    "disconnected": "Соединение отключено",
                }[state],
            )
        if state == "connected" and "rx_bytes" in clean and "tx_bytes" in clean:
            minute = datetime.now(UTC).isoformat(timespec="minutes")
            delta_rx = max(0, clean["rx_bytes"] - previous.get("rx_bytes", 0))
            delta_tx = max(0, clean["tx_bytes"] - previous.get("tx_bytes", 0))
            connection.execute(
                "INSERT INTO tunnel_metrics VALUES(?,?,?,?,?) ON CONFLICT(profile_id,minute) "
                "DO UPDATE SET rx_bytes=rx_bytes+excluded.rx_bytes,tx_bytes=tx_bytes+excluded.tx_bytes,latency_ms=excluded.latency_ms",
                (profile_id, minute, delta_rx, delta_tx, clean.get("latency_ms")),
            )
            cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="minutes")
            connection.execute("DELETE FROM tunnel_metrics WHERE minute<?", (cutoff,))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
