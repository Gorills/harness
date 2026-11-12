# ruff: noqa: RUF001
"""Daemon-side operation owner and narrow authenticated network-service client."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from harness.storage import connect_database
from harness.tunnels import (
    PROTECTION,
    STATES,
    TunnelError,
    begin_operation,
    finish_operation,
    get_profile,
    list_profiles,
    observations,
    observe_runtime,
    sanitize_snapshot,
    unavailable_snapshot,
    validate_credentials,
)

MAX_SERVICE_BYTES = 64 * 1024


class ServiceUnavailable(TunnelError):
    pass


def service_endpoint() -> Path:
    if os.environ.get("HARNESS_DEV_ROOT"):
        return Path(os.environ["HARNESS_DEV_ROOT"]) / ".harness/runtime/harness-tunnel/control.sock"
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "HarnessTunnel" / "control"
    return Path("/run/harness-tunnel/control.sock")


def _receive(sock: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        block = sock.recv(count - len(result))
        if not block:
            raise ServiceUnavailable("Служба подключения прервала ответ")
        result.extend(block)
    return bytes(result)


class NetworkServiceClient:
    def __init__(self, endpoint: Path | None = None) -> None:
        self.endpoint = service_endpoint() if endpoint is None else endpoint

    def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in {
            "prepare",
            "connect",
            "disconnect",
            "check",
            "remove",
            "status",
            "phone_export",
        }:
            raise TunnelError("Неподдерживаемая команда службы")
        data = json.dumps({"version": 1, "operation": operation, "payload": payload}).encode()
        if len(data) > MAX_SERVICE_BYTES:
            raise TunnelError("Запрос к службе превышает допустимый размер")
        try:
            if os.name == "nt":
                return self._windows_request(
                    data, 3 if operation == "status" else 360, phone=operation == "phone_export"
                )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(
                    360
                    if operation in {"prepare", "remove"}
                    else 3
                    if operation == "status"
                    else 120
                )
                peer.connect(str(self.endpoint))
                # Socket path ownership is not a substitute for authenticating
                # the process on the other end after pathname races.
                _, uid, _ = struct.unpack(
                    "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != 0:
                    raise ServiceUnavailable("Служба сети не имеет доверенного владельца")
                peer.sendall(struct.pack("!I", len(data)) + data)
                size = struct.unpack("!I", _receive(peer, 4))[0]
                if not 0 < size <= MAX_SERVICE_BYTES:
                    raise ServiceUnavailable("Служба вернула слишком большой ответ")
                result = json.loads(_receive(peer, size))
        except (OSError, ValueError) as exc:
            raise ServiceUnavailable(
                "Сетевая служба недоступна. Установите проверенный сетевой компонент Harness."
            ) from exc
        if operation == "phone_export" and isinstance(result, dict) and result.get("ok") is True:
            from harness.tunnel_phone import MAX_PHONE_BYTES, PHONE_PREFIX

            value = result.get("phone")
            if (
                set(result) != {"ok", "phone"}
                or not isinstance(value, str)
                or not value.startswith(PHONE_PREFIX)
                or len(value) > MAX_PHONE_BYTES
            ):
                raise ServiceUnavailable("Некорректный профиль телефона")
            return result
        return self._result(result)

    def _windows_request(
        self, data: bytes, timeout: float, *, phone: bool = False
    ) -> dict[str, Any]:
        # The installed service uses the owner's existing authenticated named
        # pipe transport; its endpoint and key must be provisioned by installer.
        from harness.tunnel_windows_ipc import connect

        with connect(self.endpoint.parent, timeout=timeout) as peer:
            peer.sendall(struct.pack("!I", len(data)) + data)
            size = struct.unpack("!I", _receive(peer, 4))[0]
            if not 0 < size <= MAX_SERVICE_BYTES:
                raise ServiceUnavailable("Служба вернула слишком большой ответ")
            result = json.loads(_receive(peer, size))
        if phone and isinstance(result, dict) and result.get("ok") is True:
            from harness.tunnel_phone import MAX_PHONE_BYTES, PHONE_PREFIX

            value = result.get("phone")
            if (
                set(result) != {"ok", "phone"}
                or not isinstance(value, str)
                or not value.startswith(PHONE_PREFIX)
                or len(value) > MAX_PHONE_BYTES
            ):
                raise ServiceUnavailable("Некорректный профиль телефона")
            return result
        return self._result(result)

    @staticmethod
    def _result(result: object) -> dict[str, Any]:
        if not isinstance(result, dict) or set(result) - {
            "ok",
            "code",
            "state",
            "protection",
            "snapshot",
        }:
            raise ServiceUnavailable("Некорректный ответ сетевой службы")
        if result.get("ok") is not True:
            codes = {
                "bundle_unapproved": "Сетевой пакет ещё не прошёл проверку зависимостей и платформы",
                "credentials_required": "Для подготовки VPS нужны SSH-доступы",
                "ssh_failed": "Не удалось подключиться к VPS. Проверьте SSH-доступ и ключ сервера",
                "ports_busy": "Порт 443 занят. Нужен свободный порт или отдельный IP для туннеля",
                "not_prepared": "Сначала подготовьте VPS",
                "removal_unconfirmed": "Удаление с VPS не подтверждено. Повторите; при потере служебного доступа введите свежие SSH-доступы и завершите удаление в форме VPS.",
                "guard_failed": "Защита трафика не подтверждена. Подключение не включено",
                "vpn_conflict": "Другое VPN или его блокировка сети мешает подключению. Отключите Amnezia/другое VPN и его Kill Switch, затем подключите туннель. Текущая сеть не изменена.",
                "health_failed": "Трафик через туннель не прошёл проверку; прямой выход запрещён",
                "platform_unverified": "Сетевой компонент для этой платформы ещё не подтверждён",
                "busy": "Другая сетевая операция ещё выполняется",
                "phone_export_failed": "Не удалось подготовить QR-код. Проверьте готовность VPS и версию сетевой службы.",
            }
            raise ServiceUnavailable(
                codes.get(str(result.get("code", "")), "Служба не выполнила операцию")
            )
        if result.get("state") not in STATES or result.get("protection") not in PROTECTION:
            raise ServiceUnavailable("Некорректное состояние сетевой службы")
        result["snapshot"] = sanitize_snapshot(result.get("snapshot", {}))
        if result["state"] == "connected" and (
            result["protection"] != "active"
            or not all(result["snapshot"].get(key) for key in ("tcp_ok", "dns_ok", "bulk_ok"))
        ):
            raise ServiceUnavailable("Служба не подтвердила проверку соединения")
        return result


class TunnelController:
    def __init__(self, database: Path, service: NetworkServiceClient | None = None) -> None:
        self.database = database
        self.service = NetworkServiceClient() if service is None else service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-tunnels")
        self._lock = Lock()
        self._started = False
        self._stop = Event()
        self._monitor: Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            connection = connect_database(self.database)
            try:
                interrupted = connection.execute(
                    "SELECT id FROM tunnel_operations WHERE state='pending'"
                ).fetchall()
                for row in interrupted:
                    finish_operation(
                        connection,
                        row[0],
                        success=False,
                        message="Управление перезапущено. Проверьте фактическое состояние службы перед повтором.",
                    )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    rows = connection.execute(
                        "SELECT profile_id,desired,snapshot FROM tunnel_runtime"
                    ).fetchall()
                    for row in rows:
                        connection.execute(
                            "UPDATE tunnel_runtime SET snapshot=?,"
                            "state=CASE WHEN desired=1 THEN 'reconnecting' ELSE state END,"
                            "protection=CASE WHEN desired=1 THEN 'unknown' ELSE protection END "
                            "WHERE profile_id=?",
                            (json.dumps(unavailable_snapshot(json.loads(row[2]))), row[0]),
                        )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
            self._started = True
            self._monitor = Thread(
                target=self._poll, name="harness-tunnel-observations", daemon=True
            )
            self._monitor.start()

    def close(self) -> None:
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _poll(self) -> None:
        while not self._stop.wait(1):
            connection = connect_database(self.database)
            try:
                rows = connection.execute(
                    "SELECT profile_id,desired FROM tunnel_runtime"
                ).fetchall()
                for row in rows:
                    try:
                        result = self.service.request("status", {"profile_id": row[0]})
                        observe_runtime(
                            connection,
                            row[0],
                            result["state"],
                            result["protection"],
                            result.get("snapshot", {}),
                        )
                    except TunnelError:
                        current = get_profile(connection, row[0])["runtime"]
                        observe_runtime(
                            connection,
                            row[0],
                            "reconnecting" if row[1] else current["state"],
                            "unknown" if row[1] else current["protection"],
                            unavailable_snapshot(current["snapshot"]),
                        )
            except (OSError, ValueError, sqlite3.DatabaseError):
                pass
            finally:
                connection.close()

    def state(self, profile_id: str | None = None) -> dict[str, Any]:
        connection = connect_database(self.database)
        try:
            return {
                "profiles": list_profiles(connection),
                "selected": None if profile_id is None else observations(connection, profile_id),
            }
        finally:
            connection.close()

    def export_phone(self, profile_id: str, expected_revision: int) -> dict[str, Any]:
        from harness.tunnel_phone import phone_qr
        from harness.tunnels import TunnelConflict

        with self._lock:
            connection = connect_database(self.database)
            try:
                profile = get_profile(connection, profile_id)
                if profile["revision"] != expected_revision:
                    raise TunnelConflict("Подключение изменилось. Обновите данные и повторите.")
                if not profile["runtime"]["snapshot"].get("prepared"):
                    raise TunnelError("Сначала завершите подготовку VPS")
                if any(
                    item["state"] == "pending"
                    for item in observations(connection, profile_id)["operations"]
                ):
                    raise TunnelError("Дождитесь завершения сетевой операции")
                response = self.service.request("phone_export", {"profile_id": profile_id})
                return {
                    "qr": phone_qr(response["phone"]),
                    "name": profile["settings"]["name"],
                    "shared": True,
                }
            finally:
                connection.close()

    def submit(
        self,
        profile_id: str,
        expected_revision: int,
        action: str,
        credentials: object = None,
    ) -> dict[str, Any]:
        clean_credentials = validate_credentials(credentials)
        with self._lock:
            connection = connect_database(self.database)
            try:
                operation = begin_operation(connection, profile_id, expected_revision, action)
            finally:
                connection.close()
            try:
                self._executor.submit(self._run, operation, clean_credentials)
            except RuntimeError:
                connection = connect_database(self.database)
                try:
                    finish_operation(
                        connection,
                        operation["id"],
                        success=False,
                        message="Управление завершает работу. Повторите после запуска.",
                    )
                finally:
                    connection.close()
        return operation

    def _run(self, operation: dict[str, Any], credentials: dict[str, str]) -> None:
        connection = connect_database(self.database)
        try:
            profile = get_profile(connection, operation["profile_id"])
            if operation["action"] == "prepare":
                try:
                    self.service.request("status", {"profile_id": profile["id"]})
                except ServiceUnavailable:
                    if not os.environ.get("HARNESS_DEV_ROOT"):
                        from harness.tunnel_installer import request_install

                        request_install()
            result = self.service.request(
                operation["action"],
                {
                    "profile_id": profile["id"],
                    "revision": profile["revision"],
                    "settings": profile["settings"],
                    "credentials": credentials,
                },
            )
            success = operation["action"] != "connect" or result.get("state") == "connected"
            message = (
                {
                    "prepare": "Подготовка VPS завершена",
                    "connect": "Подключение проверено",
                    "disconnect": "Туннель отключён, обычная сеть восстановлена",
                    "check": "Проверка завершена",
                    "remove": "Серверная часть удалена",
                }[operation["action"]]
                if success
                else "Передача данных не подтверждена; защита сохраняется"
            )
            finish_operation(
                connection,
                operation["id"],
                success=success,
                message=message,
                state=result.get("state"),
                protection=result.get("protection"),
                snapshot=result.get("snapshot", {}),
            )
        except TunnelError as exc:
            observation: dict[str, Any] = {}
            try:
                observation = self.service.request(
                    "status", {"profile_id": operation["profile_id"]}
                )
            except TunnelError:
                pass
            finish_operation(
                connection,
                operation["id"],
                success=False,
                message=str(exc),
                state=observation.get("state"),
                protection=observation.get("protection"),
                snapshot=observation.get("snapshot", {}),
            )
        except Exception:
            # Never reflect command output, private paths or credentials into
            # SQLite or HTTP responses, including unexpected provider failures.
            finish_operation(
                connection,
                operation["id"],
                success=False,
                message="Операция прервана. Проверьте состояние сетевой службы.",
            )
        finally:
            credentials.clear()
            connection.close()
