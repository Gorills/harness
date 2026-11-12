# ruff: noqa: RUF001
"""Machine network service; durable applied state is root/admin-owned.

The daemon owns requested business state. This process owns only the last
accepted runtime snapshot, credentials and machine network policy. Its socket
accepts one configured OS user and bounded typed operations, never shell text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any
from uuid import uuid4

from harness.tunnel_artifacts import ArtifactError, component_path, verify_bundle
from harness.tunnel_config import (
    METRICS_PORT,
    AdaptivePaths,
    PathResult,
    compile_client,
    compile_haproxy,
    compile_naive_client,
    compile_server,
    runtime_settings,
)
from harness.tunnel_health import metrics, probe, system_probe
from harness.tunnel_platform import (
    LinuxGuard,
    PlatformError,
    VPNConflict,
    fakeip_cache_file,
    require_owned,
    write_private,
)
from harness.tunnel_ssh import bootstrap, maintain, maintenance_script, reconcile_remove
from harness.tunnels import TunnelError, identity, now, sanitize_snapshot, validate_credentials

MAX_BYTES = 65536
PATH_HINT_TTL = 300.0
MAX_PATH_HINTS = 32
METRICS_UNAVAILABLE = "Счётчики движка временно недоступны; проверка соединения продолжается"
ROOT = (
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "HarnessTunnel"
    if os.name == "nt"
    else Path("/var/lib/harness-tunnel")
)
INSTALL = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "HarnessTunnel"
    if os.name == "nt"
    else Path("/opt/harness-tunnel")
)


def _read_json(path: Path) -> dict[str, Any]:
    require_owned(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise PlatformError("Закрытое состояние службы превышает допустимый размер")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise PlatformError("Некорректное закрытое состояние службы")
    return value


def _receive(peer: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = peer.recv(size - len(result))
        if not chunk:
            raise OSError("incomplete request")
        result.extend(chunk)
    return bytes(result)


def validate_request(value: object) -> tuple[str, dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "operation", "payload"}
        or value["version"] != 1
    ):
        raise TunnelError("Некорректная версия запроса")
    operation, payload = value["operation"], value["payload"]
    if not isinstance(operation, str) or operation not in {
        "prepare",
        "connect",
        "disconnect",
        "check",
        "remove",
        "status",
        "phone_export",
    }:
        raise TunnelError("Неизвестная операция службы")
    if not isinstance(payload, dict) or set(payload) - {
        "profile_id",
        "revision",
        "settings",
        "credentials",
    }:
        raise TunnelError("Недопустимые поля команды службы")
    profile_id = identity(payload.get("profile_id"))
    if operation in {"status", "phone_export"} and set(payload) != {"profile_id"}:
        raise TunnelError("У чтения состояния не должно быть команд или доступов")
    if operation not in {"prepare", "remove"} and payload.get("credentials"):
        raise TunnelError("SSH-доступ нужен только для подготовки или восстановления удаления")
    if operation not in {"status", "phone_export"}:
        if type(payload.get("revision")) is not int or payload["revision"] <= 0:
            raise TunnelError("Нужна ревизия подключения")
        settings = runtime_settings(payload.get("settings", {}))
        payload = {**payload, "settings": settings}
    return operation, {**payload, "profile_id": profile_id}


@dataclass(frozen=True)
class MetricsGeneration:
    process: Any
    token: str = field(repr=False)


@dataclass(frozen=True)
class VerifiedPathHint:
    binding: str
    endpoint: int
    transport: str
    udp_transport: str
    expires: float


class ServiceRuntime:
    def __init__(self, root: Path = ROOT, install: Path = INSTALL) -> None:
        require_owned(root, directory=True)
        require_owned(install, directory=True)
        self.root, self.install = root, install
        self.configuration = _read_json(root / "service.json")
        self.runtime_uid = self.configuration["runtime_uid"]
        self.runtime_gid = self.configuration["runtime_gid"]
        self.owner_uid = self.configuration["owner_uid"]
        self.manifest = verify_bundle(
            install / "bundle", platform=sys.platform, required=("sing-box",)
        )
        self.engine = component_path(install / "bundle", self.manifest, "sing-box")
        self.server_engine = (
            component_path(install / "bundle", self.manifest, "server-engine")
            if os.name == "nt"
            else self.engine
        )
        self.naive = (
            component_path(install / "bundle", self.manifest, "naive")
            if "naive" in self.manifest["components"]
            else None
        )
        if os.name == "nt":
            from harness.tunnel_windows import WindowsGuard

            self.guard: Any = WindowsGuard([self.engine, *([self.naive] if self.naive else [])])
        else:
            self.guard = LinuxGuard(self.runtime_uid)
        self.state = (
            _read_json(root / "applied.json")
            if (root / "applied.json").exists()
            else {
                "profiles": {},
                "active": None,
                "desired": False,
                "state": "disconnected",
                "protection": "inactive",
                "snapshot": {},
            }
        )
        self.processes: list[Any] = []
        self.ipv6_available = False  # per-process proof; never persisted across restarts
        self.metrics_generation: MetricsGeneration | None = None
        if any(
            "metrics_token" in profile["secrets"] for profile in self.state["profiles"].values()
        ):
            self.persist()  # discard legacy profile credentials; never restore them
        self.operations = Lock()
        self.observations = Lock()
        self.stop = Event()
        self.paths = AdaptivePaths()
        self.path_hints: dict[str, VerifiedPathHint] = {}
        self.last_health = 0.0
        self.last_metrics: tuple[float, int, int] | None = None
        self.metric_offsets = (0, 0)
        self.monitor_thread: Thread | None = None
        if os.name == "nt":
            from harness.tunnel_windows_ipc import EngineJob

            self.engine_job = EngineJob()

    def persist(self) -> None:
        for profile in self.state["profiles"].values():
            profile["secrets"].pop("metrics_token", None)
        write_private(self.root / "applied.json", json.dumps(self.state).encode())

    def public(self, profile_id: str) -> dict[str, Any]:
        if profile_id != self.state.get("active"):
            profile = self.state["profiles"].get(profile_id)
            endpoints = profile["secrets"]["endpoints"] if profile else []
            prepared = bool(
                profile
                and not profile.get("removing")
                and len(endpoints) == len(profile["settings"]["endpoints"])
                and endpoints
                and all(private.get("prepared") for private in endpoints)
            )
            return {
                "ok": True,
                "state": "disconnected",
                "protection": "inactive",
                "snapshot": {
                    "prepared": prepared,
                    "setup_incomplete": bool(profile and not prepared),
                    "removal_pending": bool(profile and profile.get("removing")),
                    "applied_revision": profile.get("revision", 0) if profile else 0,
                },
            }
        with self.observations:
            if self.state.get("desired") and not self.guard.active():
                self.state.update(state="blocked", protection="unknown")
                self._invalidate_rates()
            return {
                "ok": True,
                "state": self.state["state"],
                "protection": self.state["protection"],
                "snapshot": sanitize_snapshot(self.state["snapshot"]),
            }

    def execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        profile_id = payload["profile_id"]
        if operation == "status":
            return self.public(profile_id)
        if operation == "phone_export":
            # This path is strictly read-only, including all error paths. It must
            # never enter execute's network/guard recovery handler.
            if not self.operations.acquire(blocking=False):
                return {"ok": False, "code": "busy"}
            try:
                from harness.tunnel_phone import phone_profile

                profile = self.state["profiles"].get(profile_id)
                if not profile:
                    return {"ok": False, "code": "not_prepared"}
                return {"ok": True, "phone": phone_profile(profile)}
            except (KeyError, ValueError, TypeError, TunnelError):
                return {"ok": False, "code": "phone_export_failed"}
            finally:
                self.operations.release()
        if not self.operations.acquire(blocking=False):
            return {"ok": False, "code": "busy"}
        try:
            previous = self.state["profiles"].get(profile_id)
            if previous and payload["revision"] < previous["revision"]:
                raise TunnelError("Ревизия сетевого снимка устарела")
            if operation == "prepare":
                self._prepare(profile_id, payload)
            elif operation == "connect":
                if not previous:
                    return {"ok": False, "code": "not_prepared"}
                if previous.get("removing"):
                    return {"ok": False, "code": "removal_unconfirmed"}
                if previous["settings"]["endpoints"] != payload["settings"]["endpoints"]:
                    raise PlatformError("Удалите прежнюю серверную часть перед сменой VPS")
                previous["settings"] = payload["settings"]
                previous["revision"] = payload["revision"]
                self._connect(profile_id)
            elif operation == "disconnect":
                if self.state.get("active") not in {None, profile_id}:
                    raise TunnelError("Другое подключение управляет сетью")
                self._disconnect()
            elif operation == "check":
                if self.state.get("active") == profile_id and self.state["desired"]:
                    self._check(bulk=True)
                elif previous:
                    for target, private in zip(
                        previous["settings"]["endpoints"],
                        previous["secrets"]["endpoints"],
                        strict=False,
                    ):
                        maintain(
                            self.root, profile_id, target, "status", private.get("maintenance_user")
                        )
            elif operation == "remove":
                if self.state.get("active") == profile_id or self.state["desired"]:
                    raise TunnelError("Сначала отключите туннель")
                self._remove(profile_id, payload)
            return self.public(profile_id)
        except ArtifactError:
            return {"ok": False, "code": "bundle_unapproved"}
        except VPNConflict:
            return {"ok": False, "code": "vpn_conflict"}
        except (PlatformError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            # Unknown failures never convert an armed guard into direct access.
            if self.state.get("desired"):
                self.state.update(
                    state="blocked", protection="active" if self.guard.active() else "unknown"
                )
                self._invalidate_rates()
                self.state["snapshot"]["last_error"] = "Не удалось подтвердить работу туннеля"
                self.persist()
                return self.public(profile_id)
            return {
                "ok": False,
                "code": {"prepare": "ssh_failed", "remove": "removal_unconfirmed"}.get(
                    operation, "guard_failed"
                ),
            }
        finally:
            self.operations.release()

    def _remove(self, profile_id: str, payload: dict[str, Any]) -> None:
        self.path_hints.pop(profile_id, None)
        credentials = validate_credentials(payload.get("credentials"))
        try:
            profile = self.state["profiles"].get(profile_id)
            if not profile:
                return
            profile["removing"] = True
            self.persist()
            for index, (target, private) in enumerate(
                zip(profile["settings"]["endpoints"], profile["secrets"]["endpoints"], strict=False)
            ):
                if private.get("removal") == "removed":
                    continue
                endpoint_credentials = {
                    key: credentials.get("backup_" + key, credentials.get(key, ""))
                    if index
                    else credentials.get(key, "")
                    for key in ("password", "sudo_password", "private_key", "key_passphrase")
                }
                # Persist uncertainty before sending a destructive remote request.
                # Only an acknowledged owned removal advances this endpoint.
                private["removal"] = "uncertain"
                self.persist()
                try:
                    if any(endpoint_credentials.values()):
                        reconcile_remove(self.root, profile_id, target, endpoint_credentials)
                    else:
                        maintain(
                            self.root, profile_id, target, "remove", private.get("maintenance_user")
                        )
                    private["removal"] = "removed"
                    self.persist()
                finally:
                    endpoint_credentials.clear()
            del self.state["profiles"][profile_id]
            self.persist()
        finally:
            credentials.clear()

    def _new_endpoint(self) -> dict[str, Any]:
        output = subprocess.run(
            [str(self.engine), "generate", "reality-keypair"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        ).stdout
        keypair = dict(line.split(":", 1) for line in output.decode().splitlines() if ":" in line)
        private = keypair.get("PrivateKey", keypair.get("Private key", "")).strip()
        public = keypair.get("PublicKey", keypair.get("Public key", "")).strip()
        if not private or not public:
            raise PlatformError("Не удалось создать ключ REALITY")
        return {
            "uuid": str(uuid4()),
            "reality_private_key": private,
            "reality_public_key": public,
            "reality_short_id": secrets.token_hex(8),
            "reality_server_name": secrets.choice(
                ["www.microsoft.com", "www.apple.com", "www.cloudflare.com"]
            ),
            "naive_username": secrets.token_hex(12),
            "naive_password": secrets.token_hex(32),
            "hysteria_password": secrets.token_hex(32),
            "obfs_password": secrets.token_hex(32),
            "certificate_server_name": "node-" + secrets.token_hex(12) + ".invalid",
        }

    def _prepare(self, profile_id: str, payload: dict[str, Any]) -> None:
        if self.state.get("desired"):
            raise PlatformError("Сначала отключите туннель перед подготовкой VPS")
        if profile_id not in self.state["profiles"] and len(self.state["profiles"]) >= 32:
            raise PlatformError("Служба поддерживает до 32 подключений")
        credentials = validate_credentials(payload.get("credentials"))
        packages = self.manifest.get("system_packages")
        https_front = self.naive is not None and isinstance(packages, dict) and bool(packages)
        previous = self.state["profiles"].get(profile_id)
        if previous and previous.get("removing"):
            raise PlatformError("Сначала завершите удаление с VPS со свежим SSH-доступом")
        if previous and previous["settings"]["endpoints"] != payload["settings"]["endpoints"]:
            raise PlatformError("Удалите прежнюю серверную часть перед сменой VPS")
        self.path_hints.pop(profile_id, None)
        profile = previous or {
            "secrets": {"endpoints": []},
        }
        profile.update(settings=payload["settings"], revision=payload["revision"])
        self.state["profiles"][profile_id] = profile
        self.persist()
        try:
            for index, target in enumerate(profile["settings"]["endpoints"]):
                if index < len(profile["secrets"]["endpoints"]):
                    private = profile["secrets"]["endpoints"][index]
                    if private.get("prepared"):
                        continue
                else:
                    private = self._new_endpoint()
                    profile["secrets"]["endpoints"].append(private)
                # Intent and maintenance key identity precede remote writes.
                # An uncertain reply remains removable, never ready to connect.
                private["prepared"] = False
                self.persist()
                endpoint_credentials = {
                    key: credentials.get("backup_" + key, credentials.get(key, ""))
                    if index
                    else credentials.get(key, "")
                    for key in ("password", "sudo_password", "private_key", "key_passphrase")
                }
                reply = bootstrap(
                    self.root,
                    profile_id,
                    target,
                    endpoint_credentials,
                    {
                        "ip": target["host"],
                        "engine_sha256": hashlib.file_digest(
                            self.server_engine.open("rb"), "sha256"
                        ).hexdigest(),
                        "server_config": compile_server(
                            private,
                            https_front=https_front,
                            listen_address="::" if ":" in target["host"] else "0.0.0.0",
                        ),
                        "haproxy_config": compile_haproxy(private) if https_front else "",
                        "control_script": maintenance_script(profile_id),
                        "system_packages": packages if https_front else {},
                        "https_front": https_front,
                        "certificate_server_name": private["certificate_server_name"],
                    },
                    self.server_engine,
                )
                private.update(
                    certificate=reply["certificate"],
                    public_certificate=reply["public_certificate"],
                    maintenance_user=reply["maintenance_user"],
                    prepared=True,
                )
                # Preserve partial preparation so it can be maintained/removed
                # after a second endpoint or network interruption fails.
                self.state["profiles"][profile_id] = profile
                self.persist()
                endpoint_credentials.clear()
        finally:
            credentials.clear()

    def _runtime_file(self, name: str, payload: dict[str, Any]) -> Path:
        directory = Path("/run/harness-tunnel/engine") if os.name != "nt" else self.root / "engine"
        directory.mkdir(mode=0o750, exist_ok=True)
        path = directory / name
        write_private(path, json.dumps(payload).encode())
        if os.name != "nt":
            os.chown(directory, 0, self.runtime_gid)
            os.chmod(directory, 0o750)
            os.chown(path, 0, self.runtime_gid)
            os.chmod(path, 0o640)
        return path

    def _invalidate_rates(self, *, reset_interval: bool = True) -> None:
        for key in ("rx_bps", "tx_bps"):
            self.state["snapshot"].pop(key, None)
        if reset_interval:
            self.last_metrics = None

    def _stop_engines(self) -> None:
        self.metrics_generation = None  # retire before terminate/wait or any replacement listener
        self.ipv6_available = False
        self._invalidate_rates()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.processes.clear()

    def _start_path(
        self,
        profile: dict[str, Any],
        endpoint_index: int,
        transport: str,
        udp_transport: str,
        *,
        ipv6_available: bool = False,
    ) -> None:
        self.metric_offsets = (
            self.state["snapshot"].get("rx_bytes", 0),
            self.state["snapshot"].get("tx_bytes", 0),
        )
        self.last_metrics = None
        self._stop_engines()
        metrics_token = secrets.token_hex(32)
        settings, private = profile["settings"], profile["secrets"]
        routes = self.guard.lan_routes() if settings["lan_bypass"] else []
        config = compile_client(
            settings,
            private,
            endpoint_index=endpoint_index,
            tcp_transport=transport,
            udp_transport=udp_transport,
            lan_routes=routes,
            metrics_token=metrics_token,
            fakeip_cache_path=str(fakeip_cache_file(self.root, self.configuration)),
            underlay_interface=(
                self.guard.underlay_interface(settings["endpoints"][endpoint_index]["host"])
                if os.name != "nt"
                else None
            ),
            linux_routing=os.name != "nt",
            ipv6_available=ipv6_available,
        )
        config_path = self._runtime_file("sing-box.json", config)
        subprocess.run(
            [str(self.engine), "check", "-c", str(config_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        commands = []
        if transport == "naive" and self.naive is not None:
            naive_path = self._runtime_file(
                "naive.json", compile_naive_client(settings, private, endpoint_index)
            )
            commands.append([str(self.naive), str(naive_path)])
        commands.append([str(self.engine), "run", "-c", str(config_path)])
        for command in commands:
            if os.name != "nt":
                command = self.guard.engine_command(Path(command[0]), command[1:], self.runtime_gid)
            process: Any
            if os.name == "nt":
                process = self.engine_job.spawn(command)
            else:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                    start_new_session=True,
                    umask=0o077,
                )
            self.processes.append(process)
        self.metrics_generation = MetricsGeneration(self.processes[-1], metrics_token)
        self.stop.wait(1)
        if os.name == "nt":
            from harness.tunnel_windows_ipc import interface_luid

            luid = interface_luid("harness-tun")
            if luid is None:
                raise PlatformError("Windows ещё не создала интерфейс туннеля")
            self.guard.arm(routes, luid)
        else:
            self.guard.configure_dns()
        self.ipv6_available = ipv6_available
        self.state["snapshot"].update(
            endpoint=settings["endpoints"][endpoint_index]["host"],
            transport=transport,
            udp_transport="hysteria2" if udp_transport == "hysteria2" else "reality-udp-over-tcp",
            lan_routes=routes,
            applied_revision=profile["revision"],
        )

    @staticmethod
    def _path_binding(profile: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(profile["settings"]["endpoints"], sort_keys=True).encode()
        ).hexdigest()

    def _candidates(self, profile_id: str, profile: dict[str, Any]) -> list[tuple[int, str, str]]:
        candidates = [
            (index, transport, "hysteria2")
            for index, private in enumerate(profile["secrets"]["endpoints"])
            for transport in (
                ("naive", "reality", "hysteria2")
                if self.naive is not None and private.get("public_certificate")
                else ("reality", "hysteria2")
            )
        ]
        clock = monotonic()
        for cached_id, cached_hint in list(self.path_hints.items()):
            if cached_hint.expires <= clock or cached_id not in self.state["profiles"]:
                del self.path_hints[cached_id]
        hint = self.path_hints.get(profile_id)
        if hint is not None:
            key = hint.endpoint, hint.transport
            if hint.binding != self._path_binding(profile) or key not in {
                (index, transport) for index, transport, _ in candidates
            }:
                del self.path_hints[profile_id]
            else:
                return [(hint.endpoint, hint.transport, hint.udp_transport)] + [
                    candidate for candidate in candidates if candidate[:2] != key
                ]
        return candidates

    def _remember_path(
        self, profile_id: str, profile: dict[str, Any], endpoint: int, transport: str
    ) -> None:
        if len(self.path_hints) >= MAX_PATH_HINTS and profile_id not in self.path_hints:
            oldest = min(self.path_hints, key=lambda key: self.path_hints[key].expires)
            del self.path_hints[oldest]
        self.path_hints[profile_id] = VerifiedPathHint(
            self._path_binding(profile),
            endpoint,
            transport,
            "hysteria2"
            if self.state["snapshot"].get("udp_transport") == "hysteria2"
            else "reality",
            monotonic() + PATH_HINT_TTL,
        )

    def _connect(self, profile_id: str) -> None:
        profile = self.state["profiles"][profile_id]
        if len(profile["secrets"]["endpoints"]) != len(profile["settings"]["endpoints"]) or not all(
            endpoint.get("prepared") for endpoint in profile["secrets"]["endpoints"]
        ):
            raise PlatformError("Подготовлены не все VPS подключения")
        if os.name != "nt" and not self.state.get("desired"):
            # Complete compatibility checks before changing network policy or
            # activation intent, preserving the user's currently working link.
            self.guard.check_conflicts()
            available = False
            for endpoint in profile["settings"]["endpoints"]:
                try:
                    self.guard.underlay_interface(endpoint["host"])
                except VPNConflict:
                    raise
                except PlatformError:
                    continue  # An unavailable backup must not reject a usable primary.
                available = True
            if not available:
                raise PlatformError("Не найден исходный маршрут до VPS")
        routes = self.guard.lan_routes() if profile["settings"]["lan_bypass"] else []
        # Arm before starting engines/probes. Commit reboot intent before
        # acknowledging activation to the daemon.
        self.guard.arm(routes)
        if not self.guard.active():
            raise PlatformError("Защита не включена")
        self.state.update(active=profile_id, desired=True, state="connecting", protection="active")
        self.state["snapshot"] = {
            "prepared": True,
            "connected_at": "",
            "reconnects": 0,
            "bundle_version": self.manifest["version"],
        }
        self.persist()
        self.paths = AdaptivePaths()
        self._check(bulk=True, first=True)

    def _probe_path(self, endpoint: int, transport: str, *, bulk: bool) -> tuple[PathResult, str]:
        result, public_ip = probe(endpoint, transport, bulk=bulk)
        if result.usable and result.ipv6_ok != getattr(self, "ipv6_available", False):
            # A newly started path is IPv4-safe until its own IPv6 proof passes.
            # Demote a failing active IPv6 path immediately; promotion can wait
            # for a full check so a transient recovery does not churn sessions.
            if bulk or not result.ipv6_ok:
                profile = self.state["profiles"][self.state["active"]]
                udp = (
                    "hysteria2"
                    if self.state["snapshot"].get("udp_transport") == "hysteria2"
                    else "reality"
                )
                self._start_path(profile, endpoint, transport, udp, ipv6_available=result.ipv6_ok)
                result, public_ip = probe(endpoint, transport, bulk=True)
                # Do not approve a configuration whose just-enabled family
                # already failed revalidation. The next candidate starts safe.
                if self.ipv6_available and not result.ipv6_ok:
                    result, public_ip = replace(result, tcp_ok=False), ""
        if os.name != "nt" and result.usable:
            dns_ok, tcp_ok = system_probe(self.owner_uid, public_ip)
            result = replace(
                result, dns_ok=result.dns_ok and dns_ok, tcp_ok=result.tcp_ok and tcp_ok
            )
            if not result.usable:
                public_ip = ""
        return result, public_ip

    def _check(self, *, bulk: bool, first: bool = False) -> None:
        if not self.state["desired"] or not self.state["active"]:
            return
        if not self.guard.active():
            self.state.update(state="blocked", protection="unknown")
            self._stop_engines()
            self.persist()
            return
        profile = self.state["profiles"][self.state["active"]]
        self.last_health = monotonic()
        chosen = None
        full_proof = bulk
        profile_id = self.state["active"]
        public_ip = ""
        if (
            not first
            and self.paths.current is not None
            and all(process.poll() is None for process in self.processes)
        ):
            endpoint_index, transport = self.paths.current
            result, public_ip = self._probe_path(endpoint_index, transport, bulk=bulk)
            if (
                result.usable
                and not result.udp_ok
                and self.state["snapshot"].get("udp_transport") == "hysteria2"
                and transport != "hysteria2"
            ):
                self._start_path(profile, endpoint_index, transport, "reality")
                result, public_ip = self._probe_path(endpoint_index, transport, bulk=True)
                full_proof = True
            if not result.usable or not result.udp_ok:
                self.path_hints.pop(profile_id, None)
            chosen = self.paths.choose([result], monotonic())
            if result.usable:
                self.state["snapshot"].update(
                    tcp_ok=True,
                    dns_ok=True,
                    bulk_ok=True,
                    udp_ok=result.udp_ok,
                    external_ip=public_ip,
                    latency_ms=result.latency_ms,
                )
            elif self.paths.failures < 2:
                self.state["state"] = "reconnecting"
                self._invalidate_rates()
                self.state["snapshot"].update(
                    tcp_ok=result.tcp_ok,
                    dns_ok=result.dns_ok,
                    bulk_ok=result.bulk_ok,
                    udp_ok=result.udp_ok,
                )
                self.persist()
                return
            else:
                self.paths.current = None
        if chosen is None:
            self.state["state"] = "reconnecting" if not first else "connecting"
            self._invalidate_rates()
            self.persist()
            for endpoint_index, transport, udp_transport in self._candidates(profile_id, profile):
                key = endpoint_index, transport
                if self.paths.retry_after.get(key, 0) > monotonic():
                    continue
                try:
                    self._start_path(profile, endpoint_index, transport, udp_transport)
                    result, public_ip = self._probe_path(endpoint_index, transport, bulk=True)
                    if (
                        result.usable
                        and not result.udp_ok
                        and udp_transport == "hysteria2"
                        and transport != "hysteria2"
                    ):
                        self._start_path(profile, endpoint_index, transport, "reality")
                        result, public_ip = self._probe_path(endpoint_index, transport, bulk=True)
                    if result.usable:
                        self.paths.current = (endpoint_index, transport)
                        self.paths.failures = 0
                        chosen = result
                        full_proof = True
                        break
                except (OSError, ValueError, PlatformError, subprocess.SubprocessError):
                    pass
                self.path_hints.pop(profile_id, None)
                self.paths.retry_after[key] = monotonic() + 120
        if chosen is None:
            self.state["state"] = "blocked"
            self.state["snapshot"].update(
                tcp_ok=False,
                bulk_ok=False,
                dns_ok=False,
                last_error="Доступные пути не прошли проверку передачи данных",
            )
        else:
            snapshot = self.state["snapshot"]
            if snapshot.get("connected_at"):
                snapshot["reconnects"] = snapshot.get("reconnects", 0) + (
                    0 if self.state["state"] == "connected" else 1
                )
            else:
                snapshot["connected_at"] = now()
            snapshot.update(
                tcp_ok=True,
                dns_ok=True,
                bulk_ok=True,
                udp_ok=chosen.udp_ok,
                external_ip=public_ip,
                latency_ms=chosen.latency_ms,
                last_error="",
            )
            self.state["state"] = "connected"
            if not chosen.udp_ok:
                self.path_hints.pop(profile_id, None)
            elif full_proof:
                self._remember_path(profile_id, profile, chosen.endpoint, chosen.transport)
        self.last_health = monotonic()
        self.persist()

    def _disconnect(self) -> None:
        self._stop_engines()
        # Engine graceful shutdown owns route/DNS cleanup. Persistent policy
        # remains until that completes; a failure does not fall back to direct.
        self.state.update(desired=False, state="blocked")
        self.persist()
        self.guard.disarm()
        self.state.update(
            active=None, desired=False, state="disconnected", protection="inactive", snapshot={}
        )
        self.persist()

    def restore_guard(self) -> None:
        if self.state.get("desired"):
            profile = self.state["profiles"][self.state["active"]]
            routes = self.guard.lan_routes() if profile["settings"]["lan_bypass"] else []
            self.guard.arm(routes)
            self.state.update(state="reconnecting", protection="active")
            self._invalidate_rates()
            self.persist()
        elif self.guard.active():
            # Complete a persisted explicit disconnect interrupted before
            # kernel policy removal. Never infer this intent from engine exit.
            self.guard.disarm()

    def _metrics_current(self, generation: MetricsGeneration | None) -> bool:
        return (
            generation is not None
            and self.metrics_generation is generation
            and any(process is generation.process for process in self.processes)
            and all(process.poll() is None for process in self.processes)
        )

    def _metrics_process_lost(self, generation: MetricsGeneration | None) -> None:
        if self.metrics_generation is generation:
            self.metrics_generation = None
        self.state["state"] = "reconnecting"
        self.state["snapshot"].update(
            tcp_ok=False,
            dns_ok=False,
            bulk_ok=False,
            udp_ok=False,
            last_error="Процесс туннеля сменился или остановился; соединение проверяется",
        )
        self._invalidate_rates()
        self.last_health = 0.0
        self.persist()

    def _sample_metrics(self) -> None:
        generation = self.metrics_generation
        if not self._metrics_current(generation):
            self._metrics_process_lost(generation)
            return
        assert generation is not None
        try:
            sample = metrics(generation.token, METRICS_PORT)
        except TunnelError:
            if not self._metrics_current(generation):
                self._metrics_process_lost(generation)
                return
            # Counters unavailable is not proof that a healthy data path failed.
            with self.observations:
                self.state["snapshot"]["last_error"] = METRICS_UNAVAILABLE
                self._invalidate_rates(reset_interval=False)
            return
        if not self._metrics_current(generation):
            self._metrics_process_lost(generation)
            return
        sample["rx_bytes"] += self.metric_offsets[0]
        sample["tx_bytes"] += self.metric_offsets[1]
        clock = monotonic()
        if self.last_metrics is not None:
            elapsed = max(0.1, clock - self.last_metrics[0])
            sample.update(
                rx_bps=max(0, int((sample["rx_bytes"] - self.last_metrics[1]) / elapsed)),
                tx_bps=max(0, int((sample["tx_bytes"] - self.last_metrics[2]) / elapsed)),
            )
        else:
            self._invalidate_rates()
        self.last_metrics = clock, sample["rx_bytes"], sample["tx_bytes"]
        with self.observations:
            self.state["snapshot"].update(sample)
            if self.state["snapshot"].get("last_error") == METRICS_UNAVAILABLE:
                self.state["snapshot"]["last_error"] = ""

    def monitor(self) -> None:
        while not self.stop.wait(1):
            if not self.state.get("desired"):
                continue
            if self.operations.acquire(blocking=False):
                try:
                    if monotonic() - self.last_health >= 15:
                        profile = self.state["profiles"][self.state["active"]]
                        routes = (
                            self.guard.lan_routes() if profile["settings"]["lan_bypass"] else []
                        )
                        if routes != self.state["snapshot"].get("lan_routes", []):
                            self.guard.arm(routes)
                            self.paths = AdaptivePaths()
                            self._check(bulk=True, first=True)
                        self._check(bulk=False)
                    if self.state["state"] == "connected":
                        self._sample_metrics()
                except (OSError, ValueError, PlatformError, TunnelError):
                    if self.state.get("desired"):
                        self.state["state"] = "reconnecting"
                        self._invalidate_rates()
                finally:
                    self.operations.release()

    def serve(self) -> None:
        if os.name == "nt":
            raise PlatformError("Windows-служба должна запускаться через диспетчер служб")
        directory = Path("/run/harness-tunnel")
        directory.mkdir(mode=0o711, exist_ok=True)
        require_owned(directory, directory=True)
        import fcntl

        lock_descriptor = os.open(
            directory / "service.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(lock_descriptor)
            raise PlatformError("Сетевая служба уже запущена") from error
        self._service_lock = lock_descriptor
        endpoint = directory / "control.sock"
        if endpoint.exists():
            if not endpoint.is_socket() or endpoint.lstat().st_uid != self.owner_uid:
                raise PlatformError("Путь управления занят посторонним файлом")
            endpoint.unlink()
        self.restore_guard()
        self.monitor_thread = Thread(target=self.monitor, daemon=True)
        self.monitor_thread.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(endpoint))
            os.chown(endpoint, self.owner_uid, -1)
            os.chmod(endpoint, 0o600)
            listener.listen(4)
            listener.settimeout(0.5)
            while not self.stop.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                with peer:
                    peer.settimeout(10)
                    try:
                        _, uid, _ = struct.unpack(
                            "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        )
                        if uid not in {0, self.owner_uid}:
                            continue
                        size = struct.unpack("!I", _receive(peer, 4))[0]
                        if not 0 < size <= MAX_BYTES:
                            continue
                        operation, payload = validate_request(json.loads(_receive(peer, size)))
                        result = self.execute(operation, payload)
                    except (OSError, ValueError, TunnelError):
                        result = {"ok": False, "code": "invalid_request"}
                    encoded = json.dumps(result).encode()
                    if len(encoded) <= MAX_BYTES:
                        with contextlib.suppress(OSError):
                            peer.sendall(struct.pack("!I", len(encoded)) + encoded)
        self._stop_engines()
        # Intentionally retain desired state and guard across service stop.
        endpoint.unlink(missing_ok=True)

    def serve_windows(self) -> None:
        from harness.tunnel_windows_ipc import Listener

        listener = Listener(self.root, self.configuration["owner_sid"])
        self.restore_guard()
        self.monitor_thread = Thread(target=self.monitor, daemon=True)
        self.monitor_thread.start()
        while not self.stop.is_set():
            try:
                peer = listener.accept()
            except OSError:
                if self.stop.wait(0.2):
                    break
                continue
            with peer:
                try:
                    size = struct.unpack("!I", _receive(peer, 4))[0]
                    if not 0 < size <= MAX_BYTES:
                        continue
                    operation, payload = validate_request(json.loads(_receive(peer, size)))
                    result = self.execute(operation, payload)
                except (OSError, ValueError, TunnelError):
                    result = {"ok": False, "code": "invalid_request"}
                encoded = json.dumps(result).encode()
                if len(encoded) <= MAX_BYTES:
                    with contextlib.suppress(OSError):
                        peer.sendall(struct.pack("!I", len(encoded)) + encoded)
        self._stop_engines()


def main() -> int:
    command = sys.argv[1] if len(sys.argv) >= 2 else ""
    if command == "askpass":
        from harness.tunnel_ssh import askpass

        return askpass(Path(os.environ["HARNESS_TUNNEL_ASKPASS"]), " ".join(sys.argv[2:]))
    if os.name != "nt" and os.geteuid() != 0:
        raise PlatformError("Сетевой службе нужны права администратора")
    if command == "emergency-off":
        emergency_off()
        return 0
    if command == "restore-guard" and os.name != "nt":
        restore_linux_guard()
        return 0
    runtime = ServiceRuntime()
    if command == "restore-guard":
        runtime.restore_guard()
    elif command == "windows-service" and os.name == "nt":
        from harness.tunnel_windows_ipc import service_dispatch

        service_dispatch(runtime)
    elif command == "serve":
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signal, _frame: runtime.stop.set())
        runtime.serve()
    else:
        raise PlatformError("Неизвестный режим сетевой службы")
    return 0


def emergency_off() -> None:
    """Recovery works even when engine binaries or their approval are damaged."""
    require_owned(ROOT, directory=True)
    if os.name == "nt":
        import win32service  # type: ignore[import-untyped]
        import win32serviceutil  # type: ignore[import-untyped]

        from harness.tunnel_windows import WindowsGuard

        if win32serviceutil.QueryServiceStatus("HarnessTunnel")[1] != win32service.SERVICE_STOPPED:
            win32serviceutil.StopService("HarnessTunnel")
            win32serviceutil.WaitForServiceStatus("HarnessTunnel", win32service.SERVICE_STOPPED, 20)
        guard: Any = WindowsGuard([])
    else:
        stopped = subprocess.run(
            ["/usr/bin/systemctl", "stop", "harness-tunnel.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if stopped.returncode:
            raise PlatformError("Не подтверждена остановка сетевой службы")
        guard = LinuxGuard(None)
    path = ROOT / "applied.json"
    try:
        state = _read_json(path) if path.exists() else {"profiles": {}}
    except (OSError, ValueError):
        require_owned(ROOT, directory=True)
        if path.is_file() and not path.is_symlink():
            path.rename(ROOT / ("applied.recovery-" + uuid4().hex + ".json"))
        state = {"profiles": {}}
    state.update(
        desired=False, active=None, state="disconnected", protection="inactive", snapshot={}
    )
    write_private(ROOT / "applied.json", json.dumps(state).encode())
    guard.disarm()


def restore_linux_guard() -> None:
    """Boot barrier does not depend on an intact engine/admission manifest."""
    try:
        configuration = _read_json(ROOT / "service.json")
        guard = LinuxGuard(configuration["runtime_uid"])
        if not (ROOT / "applied.json").exists():
            return  # first installation has never accepted an activation
        state = _read_json(ROOT / "applied.json")
        if type(state.get("desired")) is not bool:
            raise ValueError("invalid durable intent")
        if state["desired"]:
            guard.arm([])  # LAN discovery/reconnect follows after networking
        elif guard.active():
            guard.disarm()
    except (OSError, ValueError, KeyError, TypeError):
        # A damaged state is never interpreted as an explicit disconnect.
        LinuxGuard(None).arm([])
        raise PlatformError("Состояние восстановления повреждено; трафик заблокирован") from None


if __name__ == "__main__":
    raise SystemExit(main())
