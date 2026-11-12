"""Pure, bounded configuration compilation and adaptive path selection."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
from dataclasses import dataclass
from typing import Any

from harness.tunnels import TunnelError, identity

TUN_NAME = "harness-tun"
SOCKS_PORT = 17893
METRICS_PORT = 17894
NAIVE_PORT = 17895
REALITY_PORT = 17896
NAIVE_BACKEND_PORT = 17897
HTTPS_BACKEND_PORT = 17898
TUNNEL_DNS_SERVER = "1.1.1.1"
TUNNEL_DNS_NAME = "cloudflare-dns.com"
FAKEIP_IPV4 = "198.18.0.0/15"
FAKEIP_IPV6 = "fc00::/18"
FAKEIP_CACHE_ID = "harness-fakeip-v1"


def runtime_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate a stored profile without mistaking normalized rules for raw UI input."""
    from harness.tunnels import validate_profile

    if not isinstance(settings, dict) or not isinstance(settings.get("exceptions", []), list):
        raise TunnelError("Некорректные настройки сетевой службы")
    if any(
        not isinstance(rule, dict)
        or set(rule) != {"kind", "value"}
        or rule["kind"] not in {"domain", "cidr"}
        for rule in settings.get("exceptions", [])
    ):
        raise TunnelError("Некорректные исключения сетевой службы")
    raw = {**settings, "exceptions": [rule["value"] for rule in settings.get("exceptions", [])]}
    return validate_profile(raw)


def secret_text(value: object, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > maximum:
        raise TunnelError("Некорректный закрытый параметр транспорта")
    if any(ord(char) < 33 or char in '"\\' for char in value):
        raise TunnelError("Некорректный закрытый параметр транспорта")
    return value


def connected_lan_routes(routes: object) -> list[str]:
    """Only physical, directly attached private/link-local subnets qualify.

    DHCP-installed default/public routes must never become a bypass merely by
    appearing in the main routing table.
    """
    if not isinstance(routes, list) or len(routes) > 256:
        raise TunnelError("Некорректный список локальных интерфейсов")
    result: set[str] = set()
    for route in routes:
        if (
            not isinstance(route, dict)
            or route.get("gateway")
            or route.get("type", "unicast") != "unicast"
        ):
            continue
        device = route.get("dev", "")
        if (
            not isinstance(device, str)
            or device in {"lo", TUN_NAME}
            or device.startswith(("tun", "tap", "wg", "docker", "br-", "veth"))
        ):
            continue
        if not device or route.get("protocol") not in {"kernel", "static"}:
            continue
        try:
            network = ipaddress.ip_network(route.get("dst", ""), strict=False)
        except ValueError:
            continue
        # iproute2 omits scope on directly attached IPv6 kernel routes. A
        # gateway-free kernel route still supplies connected-link evidence;
        # static routes without explicit link scope do not.
        if route.get("scope") != "link" and not (
            network.version == 6 and "scope" not in route and route.get("protocol") == "kernel"
        ):
            continue
        if network.prefixlen == 0 or network.num_addresses > 2**64:
            continue
        allowed = [
            ipaddress.ip_network(prefix)
            for prefix in (
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "169.254.0.0/16",
                "fc00::/7",
                "fe80::/10",
            )
        ]
        if any(
            (
                isinstance(network, ipaddress.IPv4Network)
                and isinstance(private, ipaddress.IPv4Network)
                and network.subnet_of(private)
            )
            or (
                isinstance(network, ipaddress.IPv6Network)
                and isinstance(private, ipaddress.IPv6Network)
                and network.subnet_of(private)
            )
            for private in allowed
        ):
            result.add(str(network))
    return sorted(result)[:32]


def compile_client(
    settings: dict[str, Any],
    secrets: dict[str, Any],
    *,
    endpoint_index: int,
    tcp_transport: str,
    udp_transport: str,
    lan_routes: list[str],
    metrics_token: str,
    fakeip_cache_path: str,
    standalone_naive: bool = True,
    underlay_interface: str | None = None,
    linux_routing: bool = False,
    ipv6_available: bool = True,
) -> dict[str, Any]:
    settings = runtime_settings(settings)
    if not 0 <= endpoint_index < len(settings["endpoints"]):
        raise TunnelError("Неизвестный VPS")
    if tcp_transport not in {"naive", "reality", "hysteria2"} or udp_transport not in {
        "hysteria2",
        "reality",
    }:
        raise TunnelError("Неподдерживаемая комбинация транспортов")
    private = secrets["endpoints"][endpoint_index]
    host = settings["endpoints"][endpoint_index]["host"]
    cover = secret_text(private["reality_server_name"])
    proxy_outbounds: list[dict[str, Any]] = [
        {
            "type": "vless",
            "tag": "reality",
            "server": host,
            "server_port": 443,
            "uuid": secret_text(private["uuid"]),
            "flow": "xtls-rprx-vision",
            "packet_encoding": "xudp",
            "tls": {
                "enabled": True,
                "server_name": cover,
                "utls": {"enabled": True, "fingerprint": "chrome"},
                "reality": {
                    "enabled": True,
                    "public_key": secret_text(private["reality_public_key"]),
                    "short_id": secret_text(private["reality_short_id"]),
                },
            },
        },
        {
            "type": "hysteria2",
            "tag": "hysteria2",
            "server": host,
            "server_port": 443,
            "password": secret_text(private["hysteria_password"]),
            "tls": {
                "enabled": True,
                "server_name": host
                if private.get("public_certificate")
                else private["certificate_server_name"],
                **(
                    {}
                    if private.get("public_certificate")
                    else {"certificate": [private["certificate"]]}
                ),
            },
            "obfs": {"type": "salamander", "password": secret_text(private["obfs_password"])},
        },
    ]
    if underlay_interface is not None:
        if not underlay_interface or len(underlay_interface.encode()) > 15:
            raise TunnelError("Некорректный исходный интерфейс VPS")
        for outbound in proxy_outbounds:
            outbound["bind_interface"] = underlay_interface
    if standalone_naive:
        proxy_outbounds.append(
            {
                "type": "socks",
                "tag": "naive",
                "server": "127.0.0.1",
                "server_port": NAIVE_PORT,
                "version": "5",
            }
        )
    else:
        # Embedded Cronet admission is deliberately independent of sing-box's
        # own release number; the bundle auditor must approve that exact build.
        proxy_outbounds.append(
            {
                "type": "naive",
                "tag": "naive",
                "server": host,
                "server_port": 443,
                "username": secret_text(private["naive_username"]),
                "password": secret_text(private["naive_password"]),
                "tls": {"server_name": host},
            }
        )
    domains = [rule["value"] for rule in settings["exceptions"] if rule["kind"] == "domain"]
    cidrs = [rule["value"] for rule in settings["exceptions"] if rule["kind"] == "cidr"]
    rules: list[dict[str, Any]] = [
        {"inbound": ["probe-in"], "network": "udp", "outbound": udp_transport},
        {"inbound": ["probe-in"], "outbound": tcp_transport},
    ]
    if not ipv6_available:
        # Reject an unusable numeric IPv6 destination at SYN/pre-match, before
        # sniffing accepts TCP. Otherwise browsers select the apparently ready
        # IPv6 socket and fail TLS instead of trying the working IPv4 address.
        # Keep DNS and explicit numeric direct/LAN exceptions usable. FakeIP
        # destinations have already become domains and resolve over IPv4 below.
        rules.append({"inbound": ["tun-in"], "port": 53, "action": "hijack-dns"})
        direct_v6 = [
            prefix
            for prefix in cidrs + (lan_routes if settings["lan_bypass"] else [])
            if ipaddress.ip_network(prefix).version == 6
        ]
        if direct_v6:
            rules.append({"inbound": ["tun-in"], "ip_cidr": direct_v6, "outbound": "direct"})
        rules.append(
            {
                "inbound": ["tun-in"],
                "ip_version": 6,
                "action": "reject",
                "method": "default",
                "no_drop": True,
            }
        )
    rules.extend(
        [
            {"inbound": ["tun-in"], "action": "sniff", "sniffer": ["tls", "http", "quic", "dns"]},
            {"protocol": "dns", "action": "hijack-dns"},
        ]
    )
    if domains:
        rules.append({"domain_suffix": domains, "outbound": "direct"})
    direct_cidrs = cidrs + (lan_routes if settings["lan_bypass"] else [])
    if direct_cidrs:
        # FakeIP restores the domain, not its real addresses. Domain bypasses
        # above retain their direct resolver; all other resolution stays private.
        rules.append({"action": "resolve", "server": "tunnel-dns"})
        rules.append({"ip_cidr": direct_cidrs, "outbound": "direct"})
    rules.append({"network": "udp", "outbound": udp_transport})
    dns_rules = [{"inbound": ["tun-in"], "query_type": ["A", "AAAA"], "server": "fakeip"}]
    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {
                    "type": "fakeip",
                    "tag": "fakeip",
                    "inet4_range": FAKEIP_IPV4,
                    "inet6_range": FAKEIP_IPV6,
                },
                {
                    "type": "https",
                    "tag": "tunnel-dns",
                    "server": TUNNEL_DNS_SERVER,
                    "server_port": 443,
                    "path": "/dns-query",
                    "tls": {"enabled": True, "server_name": TUNNEL_DNS_NAME},
                    "detour": tcp_transport,
                },
                {
                    "type": "https",
                    "tag": "direct-dns",
                    "server": "9.9.9.9",
                    "server_port": 443,
                    "path": "/dns-query",
                    "tls": {"enabled": True, "server_name": "dns.quad9.net"},
                    "detour": "direct",
                },
            ],
            "rules": dns_rules,
            "final": "tunnel-dns",
            "independent_cache": True,
            **({"strategy": "ipv4_only"} if not ipv6_available else {}),
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": TUN_NAME,
                "address": ["172.31.255.1/30", "fdfe:dcba:9876::1/126"],
                "mtu": 1400,
                "auto_route": True,
                "strict_route": True,
                # Linux checks this table with suppress_prefixlength=0 before
                # main. An ordinary /0 is skipped, letting existing VPN /1
                # routes capture app/FakeIP traffic outside our guarded TUN.
                **(
                    {"route_address": ["0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1"]}
                    if linux_routing
                    else {}
                ),
            },
            {"type": "socks", "tag": "probe-in", "listen": "127.0.0.1", "listen_port": SOCKS_PORT},
        ],
        "outbounds": [
            *proxy_outbounds,
            {"type": "direct", "tag": "direct", "domain_resolver": "direct-dns"},
        ],
        "route": {
            "auto_detect_interface": True,
            "default_domain_resolver": "tunnel-dns",
            "rules": rules,
            "final": tcp_transport,
        },
        "experimental": {
            "cache_file": {
                "enabled": True,
                "path": fakeip_cache_path,
                "cache_id": FAKEIP_CACHE_ID,
                "store_fakeip": True,
            },
            "clash_api": {
                "external_controller": f"127.0.0.1:{METRICS_PORT}",
                "secret": secret_text(metrics_token),
                "access_control_allow_origin": ["http://127.0.0.1.invalid"],
            },
        },
    }


def compile_naive_client(
    settings: dict[str, Any], secrets: dict[str, Any], endpoint_index: int
) -> dict[str, Any]:
    host = settings["endpoints"][endpoint_index]["host"]
    if ipaddress.ip_address(host).version == 6:
        host = f"[{host}]"
    private = secrets["endpoints"][endpoint_index]
    return {
        "listen": f"socks://127.0.0.1:{NAIVE_PORT}",
        "proxy": f"https://{secret_text(private['naive_username'])}:{secret_text(private['naive_password'])}@{host}:443",
    }


def compile_server(
    private: dict[str, Any], *, https_front: bool = True, listen_address: str = "::"
) -> dict[str, Any]:
    if listen_address not in {"::", "0.0.0.0"}:
        raise TunnelError("Некорректный интерфейс серверного туннеля")
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            *(
                [
                    {
                        "type": "naive",
                        "tag": "naive-in",
                        "listen": "127.0.0.1",
                        "listen_port": NAIVE_BACKEND_PORT,
                        "network": "tcp",
                        "users": [
                            {
                                "username": secret_text(private["naive_username"]),
                                "password": secret_text(private["naive_password"]),
                            }
                        ],
                    }
                ]
                if https_front
                else []
            ),
            {
                "type": "vless",
                "tag": "reality-in",
                "listen": "127.0.0.1" if https_front else listen_address,
                "listen_port": REALITY_PORT if https_front else 443,
                "users": [{"uuid": secret_text(private["uuid"]), "flow": "xtls-rprx-vision"}],
                "tls": {
                    "enabled": True,
                    "server_name": secret_text(private["reality_server_name"]),
                    "reality": {
                        "enabled": True,
                        "handshake": {
                            "server": secret_text(private["reality_server_name"]),
                            "server_port": 443,
                        },
                        "private_key": secret_text(private["reality_private_key"]),
                        "short_id": [secret_text(private["reality_short_id"])],
                    },
                },
            },
            {
                "type": "hysteria2",
                "tag": "hysteria-in",
                "listen": listen_address,
                "listen_port": 443,
                "users": [{"password": secret_text(private["hysteria_password"])}],
                "obfs": {"type": "salamander", "password": secret_text(private["obfs_password"])},
                "tls": {
                    "enabled": True,
                    "certificate_path": "/etc/harness-tunnel/https.pem",
                    "key_path": "/etc/harness-tunnel/https.pem",
                },
            },
        ],
        "outbounds": [{"type": "direct", "tag": "internet"}],
        "route": {"rules": [{"ip_is_private": True, "action": "reject"}], "final": "internet"},
    }


def compile_haproxy(private: dict[str, Any]) -> str:
    server_name = secret_text(private["reality_server_name"])
    # Validate the interpolated token independently of the JSON compiler.
    if any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
        for char in server_name
    ):
        raise TunnelError("Некорректное имя HTTPS-маскировки")
    authorization = base64.b64encode(
        f"{secret_text(private['naive_username'])}:{secret_text(private['naive_password'])}".encode()
    ).decode()
    return f"""global
  maxconn 1024
  tune.bufsize 65536
defaults
  timeout connect 10s
  timeout client 300s
  timeout server 300s
frontend public_tls
  bind :443
  mode tcp
  tcp-request inspect-delay 5s
  tcp-request content accept if {{ req.ssl_hello_type 1 }}
  use_backend reality if {{ req.ssl_sni -i {server_name} }}
  default_backend ordinary_https
backend reality
  mode tcp
  server reality 127.0.0.1:{REALITY_PORT}
backend ordinary_https
  mode tcp
  server https 127.0.0.1:{HTTPS_BACKEND_PORT}
frontend https
  bind 127.0.0.1:{HTTPS_BACKEND_PORT} ssl crt /etc/harness-tunnel/https.pem alpn h2,http/1.1
  mode http
  acl authorized req.hdr(Proxy-Authorization) -m str "Basic {authorization}"
  use_backend naive if authorized
  http-request return status 200 content-type text/html string "<!doctype html><title>Welcome</title><h1>Welcome</h1>" unless authorized
backend naive
  mode http
  server naive 127.0.0.1:{NAIVE_BACKEND_PORT} proto h2
"""


@dataclass(frozen=True)
class PathResult:
    endpoint: int
    transport: str
    tcp_ok: bool
    dns_ok: bool
    bulk_ok: bool
    udp_ok: bool = False
    latency_ms: float | None = None
    ipv6_ok: bool = False

    @property
    def usable(self) -> bool:
        return self.tcp_ok and self.dns_ok and self.bulk_ok


class AdaptivePaths:
    """Stable ordered failover; latency noise never evicts a healthy path."""

    def __init__(self) -> None:
        self.current: tuple[int, str] | None = None
        self.failures = 0
        self.retry_after: dict[tuple[int, str], float] = {}

    def choose(self, results: list[PathResult], clock: float) -> PathResult | None:
        if not math.isfinite(clock):
            raise TunnelError("Некорректное время проверки")
        by_path = {(item.endpoint, item.transport): item for item in results}
        if self.current in by_path and by_path[self.current].usable:
            self.failures = 0
            return by_path[self.current]
        if self.current is not None:
            self.failures += 1
            if self.failures < 2:
                return None
            self.retry_after[self.current] = clock + 120
        for item in sorted(
            results,
            key=lambda entry: (entry.endpoint, {"naive": 0, "reality": 1}.get(entry.transport, 2)),
        ):
            key = item.endpoint, item.transport
            if item.usable and self.retry_after.get(key, 0) <= clock:
                self.current, self.failures = key, 0
                return item
        return None


def profile_fingerprint(profile_id: str, settings: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([identity(profile_id), runtime_settings(settings)], sort_keys=True).encode()
    ).hexdigest()
