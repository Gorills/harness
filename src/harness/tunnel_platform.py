# ruff: noqa: RUF001
"""Privileged OS policy: owned rules, dedicated runtime identity, bounded commands."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from harness.tunnel_config import FAKEIP_IPV4, FAKEIP_IPV6, TUN_NAME, connected_lan_routes
from harness.tunnels import TunnelError

TABLE = "harness_tunnel"
TABLE_OWNER = "Harness tunnel guard v1"
ENGINE_USER = "harness-tunnel"


class PlatformError(TunnelError):
    pass


class VPNConflict(PlatformError):
    """Another system VPN would be blocked by our guard or block our traffic."""


def run(command: list[str], *, data: bytes | None = None, timeout: int = 30) -> bytes:
    """Never return stderr or command arguments into public error paths."""
    try:
        result = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlatformError("Системная операция не выполнена") from exc
    if result.returncode:
        raise PlatformError("Системная операция не выполнена")
    if len(result.stdout) > 1024 * 1024:
        raise PlatformError("Системный ответ превышает допустимый размер")
    return result.stdout


def require_owned(path: Path, *, directory: bool = False, owner: int = 0) -> None:
    """Check all existing ancestors; do not follow a user-owned import/symlink tree."""
    if os.name == "nt":
        from harness.tunnel_windows_ipc import require_admin_path

        require_admin_path(path, directory=directory)
        return
    if not path.is_absolute():
        raise PlatformError("Службе нужен абсолютный защищённый путь")
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            # /tmp is allowed only as an ancestor in explicit isolated tests,
            # never a production service installation root.
            raise PlatformError("Путь сетевой службы не защищён от изменения")
    info = path.stat()
    if (directory and not stat.S_ISDIR(info.st_mode)) or (
        not directory and not stat.S_ISREG(info.st_mode)
    ):
        raise PlatformError("Некорректный тип файла сетевой службы")
    if not directory and info.st_nlink != 1:
        raise PlatformError("Недопустимая ссылка на файл службы")


def write_private(path: Path, data: bytes, *, owner: int | None = None) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        if owner is not None and os.name != "nt":
            os.fchown(descriptor, owner, -1)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise PlatformError("Файл службы заменён посторонней ссылкой")
        os.replace(temporary, path)
        if os.name == "nt":
            from harness.tunnel_windows_ipc import secure_admin_path

            secure_admin_path(path, directory=False)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def fakeip_cache_root(state_root: Path) -> Path:
    """Linux engines cannot traverse the separate root-only credential tree."""
    return (
        state_root / "fakeip-cache"
        if os.name == "nt"
        else state_root.with_name(state_root.name + "-cache")
    )


def _cache_owner(state_root: Path, configuration: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "state_root": str(state_root),
        "owner_uid": configuration["owner_uid"],
        "owner_sid": configuration.get("owner_sid"),
        "runtime_uid": configuration["runtime_uid"],
        "runtime_gid": configuration["runtime_gid"],
        "pools": [FAKEIP_IPV4, FAKEIP_IPV6],
    }


def create_fakeip_cache(state_root: Path, configuration: dict[str, Any]) -> None:
    """First-install transaction only; never adopt or repair an existing path."""
    directory = fakeip_cache_root(state_root)
    require_owned(directory.parent, directory=True)
    directory.mkdir(mode=0o700)
    try:
        if os.name == "nt":
            from harness.tunnel_windows_ipc import secure_admin_path

            secure_admin_path(directory, directory=True)
        else:
            os.chown(directory, 0, configuration["runtime_gid"])
            directory.chmod(0o750)
        write_private(
            directory / "owner.json", json.dumps(_cache_owner(state_root, configuration)).encode()
        )
    except BaseException:
        with contextlib.suppress(OSError):
            (directory / "owner.json").unlink(missing_ok=True)
            directory.rmdir()
        raise


def fakeip_cache_file(state_root: Path, configuration: dict[str, Any]) -> Path:
    """Share mappings, not policy or credentials, across this owner's profiles.

    The root-owned parent cannot be renamed or written by the engine. Root reads
    only its own marker and never reads or repairs the engine-writable database.
    """
    directory = fakeip_cache_root(state_root)
    require_owned(directory, directory=True)
    marker = directory / "owner.json"
    require_owned(marker)
    if marker.stat().st_size > 4096 or json.loads(marker.read_bytes()) != _cache_owner(
        state_root, configuration
    ):
        raise PlatformError("Кеш DNS принадлежит другой установке или владельцу")
    if os.name == "nt":
        from harness.tunnel_windows_ipc import require_admin_path

        # No configured desktop-user read grant: mappings are SYSTEM/admin-only.
        require_admin_path(directory, directory=True, reader_sid="S-1-5-18")
        path = directory / "maps.db"
        if path.exists() or path.is_symlink():
            require_admin_path(path, directory=False, reader_sid="S-1-5-18")
            if path.stat().st_nlink != 1:
                raise PlatformError("Недопустимая ссылка на кеш DNS")
        return path
    info = directory.stat()
    if stat.S_IMODE(info.st_mode) != 0o750 or info.st_gid != configuration["runtime_gid"]:
        raise PlatformError("Неверные права каталога кеша DNS")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(directory, flags)
    try:
        try:
            descriptor = os.open(
                "maps.db",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            pass
        else:
            try:
                os.fchown(descriptor, configuration["runtime_uid"], configuration["runtime_gid"])
                os.fsync(descriptor)
                os.fsync(parent)
            except BaseException:
                os.unlink("maps.db", dir_fd=parent)
                raise
            finally:
                os.close(descriptor)
        info = os.stat("maps.db", dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != configuration["runtime_uid"]
            or info.st_gid != configuration["runtime_gid"]
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PlatformError("Неверные права или ссылка на кеш DNS")
    finally:
        os.close(parent)
    return directory / "maps.db"


def nft_rules(runtime_uid: int | None, lan_routes: list[str], *, exists: bool = False) -> str:
    import ipaddress

    if runtime_uid is not None and (type(runtime_uid) is not int or runtime_uid <= 0):
        raise PlatformError("Для движка нужен отдельный непривилегированный пользователь")
    if len(lan_routes) > 32:
        raise PlatformError("Слишком много локальных сетей")
    local: dict[int, list[str]] = {4: [], 6: []}
    for raw in lan_routes:
        network = ipaddress.ip_network(raw, strict=True)
        if network.prefixlen == 0 or not network.is_private:
            raise PlatformError("Недопустимое локальное исключение")
        local[network.version].append(str(network))
    rules = [
        f"flush table inet {TABLE}\n" if exists else "",
        f'table inet {TABLE} {{\n comment "{TABLE_OWNER}"\n chain output {{\n type filter hook output priority -200; policy drop;',
        'oifname "lo" accept',
        f'oifname "{TUN_NAME}" accept',
        *([f"meta skuid {runtime_uid} accept"] if runtime_uid is not None else []),
        "ip daddr 255.255.255.255 udp sport 68 udp dport 67 accept",
        "ip6 daddr ff02::1:2 udp sport 546 udp dport 547 accept",
        "ip6 hoplimit 255 icmpv6 type { nd-router-solicit, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept",
    ]
    for version, values in local.items():
        if values:
            rules.append(
                f"{'ip' if version == 4 else 'ip6'} daddr {{ {', '.join(values)} }} accept"
            )
    rules.append("counter drop\n }\n}\n")
    return "\n".join(rules)


class LinuxGuard:
    def __init__(self, runtime_uid: int | None, *, nft: str = "/usr/sbin/nft") -> None:
        if runtime_uid is not None and runtime_uid <= 0:
            raise PlatformError("Нельзя разрешать обход защиты всем root-процессам")
        self.runtime_uid = runtime_uid
        self.nft = nft

    def active(self) -> bool:
        try:
            result = json.loads(run([self.nft, "-j", "list", "table", "inet", TABLE]))
        except (PlatformError, ValueError):
            return False
        tables = [item.get("table", {}) for item in result.get("nftables", [])]
        if not any(table.get("comment") == TABLE_OWNER for table in tables):
            return False
        chains = [item.get("chain", {}) for item in result.get("nftables", [])]
        return any(
            chain.get("name") == "output"
            and chain.get("hook") == "output"
            and chain.get("policy") == "drop"
            for chain in chains
        )

    def arm(self, lan_routes: list[str]) -> None:
        # A familiar name alone never grants ownership of an existing policy.
        listing = json.loads(run([self.nft, "-j", "list", "tables"]))
        exists = any(
            entry.get("table", {}).get("family") == "inet"
            and entry.get("table", {}).get("name") == TABLE
            for entry in listing.get("nftables", [])
        )
        if exists and not self.active():
            raise PlatformError("Имя сетевой политики занято посторонними правилами")
        rules = nft_rules(self.runtime_uid, lan_routes, exists=exists).encode()
        # nft applies the complete batch atomically. No delete/add exposure gap.
        run([self.nft, "--check", "--file", "-"], data=rules)
        run([self.nft, "--file", "-"], data=rules)
        if not self.active():
            raise PlatformError("Не удалось подтвердить сетевую защиту")

    def disarm(self) -> None:
        if self.active():
            run([self.nft, "delete", "table", "inet", TABLE])

    def lan_routes(self) -> list[str]:
        values = json.loads(run(["/usr/sbin/ip", "-j", "route", "show", "table", "main"]))
        values += json.loads(run(["/usr/sbin/ip", "-j", "-6", "route", "show", "table", "main"]))
        return connected_lan_routes(values)

    def engine_command(self, executable: Path, arguments: list[str], gid: int) -> list[str]:
        require_owned(executable)
        if gid <= 0 or self.runtime_uid is None:
            raise PlatformError("Нужна отдельная группа сетевого движка")
        return [
            "/usr/bin/setpriv",
            f"--reuid={self.runtime_uid}",
            f"--regid={gid}",
            "--clear-groups",
            "--inh-caps=+net_admin",
            "--ambient-caps=+net_admin",
            str(executable),
            *arguments,
        ]

    def configure_dns(self) -> None:
        """Set only our link, as root; the engine UID cannot configure resolved.

        Plain resolv.conf systems use TUN's DNS interception. With resolved,
        its upstream sockets can be bound to another VPN, bypassing TUN routes.
        Never silently accept a failed update of an active resolver.
        """
        ctl = "/usr/bin/resolvectl"
        if not Path(ctl).exists():
            return
        try:
            run([ctl, "status"], timeout=5)
        except PlatformError:
            if Path("/run/systemd/resolve/resolv.conf").exists():
                raise PlatformError("Не удалось связаться с системной службой DNS") from None
            return
        try:
            run([ctl, "dns", TUN_NAME, "172.31.255.2", "fdfe:dcba:9876::2"], timeout=5)
            run([ctl, "domain", TUN_NAME, "~."], timeout=5)
            run([ctl, "default-route", TUN_NAME, "true"], timeout=5)
            run([ctl, "flush-caches"], timeout=5)
        except PlatformError:
            with contextlib.suppress(PlatformError):
                run([ctl, "revert", TUN_NAME], timeout=5)
            raise

    def underlay_interface(self, host: str) -> str:
        # This is a standalone VPN, never implicitly a tunnel through another
        # VPN. Its root-owned carrier packets would be blocked by our UID guard.
        address = ipaddress.ip_address(host)
        arguments = ["/usr/sbin/ip", "-j"]
        if address.version == 6:
            arguments.append("-6")
        values = json.loads(
            run([*arguments, "route", "get", str(address), "uid", str(self.runtime_uid)])
        )
        device = (
            values[0].get("dev")
            if isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict)
            else None
        )
        if (
            not isinstance(device, str)
            or not device
            or device == TUN_NAME
            or len(device.encode()) > 15
        ):
            raise PlatformError("Не найден исходный маршрут до VPS")
        links = json.loads(run(["/usr/sbin/ip", "-j", "-d", "link", "show", "dev", device]))
        if not isinstance(links, list) or len(links) != 1:
            raise PlatformError("Не найден исходный сетевой интерфейс")
        link = links[0]
        if link.get("link_type") == "none" or link.get("linkinfo", {}).get("info_kind") in {
            "tun",
            "wireguard",
            "amneziawg",
            "gre",
            "gretap",
            "ipip",
            "sit",
            "vti",
            "vti6",
        }:
            raise VPNConflict("Другое VPN-подключение управляет исходным маршрутом")
        return device

    def check_conflicts(self) -> None:
        """Reject reachable Amnezia blocking rules before arming our own guard.

        A disconnected VPN can leave its kill switch enabled. Never remove or
        override another application's firewall policy to make our check pass.
        """
        data = json.loads(run([self.nft, "-j", "list", "ruleset"]))
        chains: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        roots = []
        for item in data.get("nftables", []):
            chain = item.get("chain")
            if chain and chain.get("hook") == "output":
                roots.append((chain["family"], chain["table"], chain["name"]))
            rule = item.get("rule")
            if rule:
                key = (rule["family"], rule["table"], rule["chain"])
                chains.setdefault(key, []).extend(rule.get("expr", []))
        seen = set()
        while roots:
            key = roots.pop()
            if key in seen:
                continue
            seen.add(key)
            for expression in chains.get(key, []):
                if key[2].startswith("amnvpn.") and any(
                    verdict in expression for verdict in ("drop", "reject")
                ):
                    raise VPNConflict("Активна блокировка сети другого VPN")
                for verdict in ("jump", "goto"):
                    target = expression.get(verdict, {}).get("target")
                    if isinstance(target, str):
                        roots.append((key[0], key[1], target))


def systemd_units(python: Path, launcher: Path, state_root: Path) -> dict[str, str]:
    if any(
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)) for path in (python, launcher, state_root)
    ):
        raise PlatformError("Некорректный путь установщика службы")
    return {
        "harness-tunnel-guard.service": f"""[Unit]
Description=Harness tunnel persistent traffic guard
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
After=local-fs.target
[Service]
Type=oneshot
ExecStart={python} -I -S {launcher} restore-guard
RemainAfterExit=yes
UMask=0077
[Install]
WantedBy=network-pre.target
""",
        "harness-tunnel.service": f"""[Unit]
Description=Harness tunnel network service
Requires=harness-tunnel-guard.service
After=harness-tunnel-guard.service network.target
[Service]
Type=simple
ExecStart={python} -I -S {launcher} serve
Restart=always
RestartSec=2
UMask=0077
RuntimeDirectory=harness-tunnel
RuntimeDirectoryMode=0711
StateDirectory=harness-tunnel
StateDirectoryMode=0700
ProtectSystem=strict
ReadWritePaths={state_root} {fakeip_cache_root(state_root)} /run/harness-tunnel
ProtectHome=yes
NoNewPrivileges=no
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
""",
    }
