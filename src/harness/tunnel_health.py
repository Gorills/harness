"""Actual data-path checks through SOCKS; no success from a TCP handshake alone."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from harness.tunnel_config import SOCKS_PORT, TUNNEL_DNS_NAME, TUNNEL_DNS_SERVER, PathResult
from harness.tunnels import TunnelError

BULK_BYTES = 256 * 1024
STUN_HOST = "stun.cloudflare.com"
STUN_PORT = 3478

# Isolated stdlib interpreter, fixed destinations, no environment proxies or
# user-site imports. Running as the desktop owner exercises the actual DNS,
# routes and guard; the engine's allowed UID/SOCKS path alone cannot prove this.
SYSTEM_PATH_PROBE = """
import http.client, ipaddress, json, socket, sys
expected = ipaddress.ip_address(sys.argv[1])
dns_ok = tcp_ok = False
for host in ('www.cloudflare.com', 'one.one.one.one'):
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        dns_ok = True
        peer = http.client.HTTPSConnection(host, timeout=6)
        try:
            peer.request('GET', '/cdn-cgi/trace', headers={'Connection': 'close'})
            response = peer.getresponse()
            body = response.read(8193)
            if response.status == 200 and len(body) <= 8192:
                fields = dict(line.split('=', 1) for line in body.decode().splitlines() if '=' in line)
                tcp_ok = ipaddress.ip_address(fields.get('ip', '')) == expected
        finally:
            peer.close()
        if tcp_ok:
            break
    except (OSError, ValueError, http.client.HTTPException):
        pass
print(json.dumps([dns_ok, tcp_ok]))
"""


def system_probe(owner_uid: int, expected_ip: str) -> tuple[bool, bool]:
    """Prove ordinary Linux application DNS/HTTPS reaches the selected exit."""
    import pwd

    if type(owner_uid) is not int or owner_uid <= 0:
        raise TunnelError("Нужен непривилегированный владелец туннеля")
    try:
        ipaddress.ip_address(expected_ip)
        owner = pwd.getpwuid(owner_uid)
        result = subprocess.run(
            [
                "/usr/bin/setpriv",
                f"--reuid={owner_uid}",
                f"--regid={owner.pw_gid}",
                "--clear-groups",
                "--no-new-privs",
                "/usr/bin/python3",
                "-I",
                "-S",
                "-c",
                SYSTEM_PATH_PROBE,
                expected_ip,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            timeout=25,
            check=False,
        )
        if result.returncode == 0 and len(result.stdout) <= 64:
            value = json.loads(result.stdout)
            if isinstance(value, list) and len(value) == 2 and all(type(x) is bool for x in value):
                return value[0], value[1]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        pass
    return False, False


def receive(peer: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = peer.recv(size - len(data))
        if not part:
            raise OSError("incomplete proxy response")
        data.extend(part)
    return bytes(data)


def socks_connect(host: str, port: int, *, proxy_port: int = SOCKS_PORT) -> socket.socket:
    peer = socket.create_connection(("127.0.0.1", proxy_port), timeout=8)
    try:
        peer.sendall(b"\x05\x01\x00")
        if receive(peer, 2) != b"\x05\x00":
            raise OSError("proxy authentication failed")
        domain = host.encode("idna")
        if not 0 < len(domain) < 256:
            raise OSError("invalid proxy destination")
        peer.sendall(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + struct.pack("!H", port))
        header = receive(peer, 4)
        if header[:2] != b"\x05\x00":
            raise OSError("proxy connection failed")
        address_length = {1: 4, 4: 16}.get(header[3])
        if header[3] == 3:
            address_length = receive(peer, 1)[0]
        if address_length is None:
            raise OSError("invalid proxy address")
        receive(peer, address_length + 2)
        return peer
    except BaseException:
        peer.close()
        raise


def request(
    url: str,
    *,
    body: bytes | None = None,
    maximum: int = BULK_BYTES + 1,
    accept: str = "*/*",
    server_name: str | None = None,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None or parsed.username or parsed.password:
        raise TunnelError("Проверка требует обычного HTTPS-адреса")
    peer = socks_connect(parsed.hostname, parsed.port or 443)
    try:
        context = ssl.create_default_context()
        with context.wrap_socket(peer, server_hostname=server_name or parsed.hostname) as secure:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            method = "GET" if body is None else "POST"
            headers = f"{method} {target} HTTP/1.1\r\nHost: {server_name or parsed.hostname}\r\nAccept: {accept}\r\nConnection: close\r\n"
            if body is not None:
                headers += (
                    f"Content-Type: application/octet-stream\r\nContent-Length: {len(body)}\r\n"
                )
            secure.sendall((headers + "\r\n").encode("ascii"))
            if body is not None:
                secure.sendall(body)
            response = http.client.HTTPResponse(secure)
            response.begin()
            data = response.read(maximum)
            return response.status, data
    finally:
        peer.close()


def dns_check() -> bool:
    """Query the configured Cloudflare DoH upstream through forced probe-in.

    A system/TUN DNS query would only prove the local FakeIP responder. This
    exchange requires the selected TCP path and a matching upstream DNS reply.
    """
    transaction = secrets.token_bytes(2)
    question = b"\x0acloudflare\x03com\x00" + struct.pack("!HH", 1, 1)
    query = transaction + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0) + question
    encoded = base64.urlsafe_b64encode(query).rstrip(b"=").decode("ascii")
    status, reply = request(
        f"https://{TUNNEL_DNS_SERVER}/dns-query?dns={encoded}",
        maximum=4096,
        accept="application/dns-message",
        server_name=TUNNEL_DNS_NAME,
    )
    if not (
        status == 200
        and len(reply) <= 4096
        and len(reply) >= 12
        and reply[:2] == transaction
        and reply[2] & 0xFA == 0x80
        and reply[3] & 0x0F == 0
        and reply[4:6] == b"\x00\x01"
        and reply[6:8] != b"\x00\x00"
        and reply[12 : 12 + len(question)] == question
    ):
        return False
    offset = 12 + len(question)
    for _ in range(struct.unpack("!H", reply[6:8])[0]):
        # Skip an owner name, without following compression pointers. Only a
        # complete IN A record with a real upstream address constitutes proof.
        while offset < len(reply) and reply[offset]:
            length = reply[offset]
            offset += 1
            if length & 0xC0 == 0xC0:
                offset += 1
                break
            if length > 63:
                return False
            offset += length
        else:
            offset += 1
        if offset + 10 > len(reply):
            return False
        kind, cls, _, size = struct.unpack("!HHIH", reply[offset : offset + 10])
        offset += 10
        if offset + size > len(reply):
            return False
        if kind == cls == 1 and size == 4:
            address = ipaddress.ip_address(reply[offset : offset + size])
            return address not in ipaddress.ip_network("198.18.0.0/15")
        offset += size
    return False


def udp_check() -> bool:
    """STUN Binding through the SOCKS UDP association exercises the selected UDP path."""
    control = socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=5)
    try:
        control.sendall(b"\x05\x01\x00")
        if receive(control, 2) != b"\x05\x00":
            return False
        control.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        header = receive(control, 4)
        if header[:2] != b"\x05\x00":
            return False
        if header[3] == 1:
            host = socket.inet_ntop(socket.AF_INET, receive(control, 4))
        elif header[3] == 4:
            host = socket.inet_ntop(socket.AF_INET6, receive(control, 16))
        else:
            return False
        port = struct.unpack("!H", receive(control, 2))[0]
        if ipaddress.ip_address(host).is_unspecified:
            host = "127.0.0.1"
        transaction = secrets.token_bytes(12)
        stun = struct.pack("!HHI", 1, 0, 0x2112A442) + transaction
        destination = STUN_HOST.encode("ascii")
        frame = (
            b"\x00\x00\x00\x03"
            + bytes([len(destination)])
            + destination
            + struct.pack("!H", STUN_PORT)
            + stun
        )
        with socket.socket(
            socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM
        ) as peer:
            peer.settimeout(5)
            peer.sendto(frame, (host, port))
            reply, _ = peer.recvfrom(4096)
        # SOCKS address length is variable; the STUN header is matched by its
        # full unpredictable transaction ID rather than an arbitrary UDP reply.
        if len(reply) < 4 or reply[:3] != b"\x00\x00\x00":
            return False
        offset = 4
        if reply[3] == 1:
            offset += 4
        elif reply[3] == 4:
            offset += 16
        elif reply[3] == 3 and len(reply) > offset:
            offset += 1 + reply[offset]
        else:
            return False
        offset += 2
        answer = reply[offset:]
        if len(answer) < 20:
            return False
        kind, length, cookie = struct.unpack("!HHI", answer[:8])
        return (
            kind == 0x0101
            and cookie == 0x2112A442
            and answer[8:20] == transaction
            and len(answer) == length + 20
        )
    finally:
        control.close()


def ipv6_check() -> bool:
    """Require real IPv6 TLS/HTTP through probe-in, bypassing DNS preference.

    A successful IPv4 trace or an assigned server IPv6 address is not evidence
    that numeric AAAA/HTTPS-hint destinations used by browsers are reachable.
    """
    try:
        status, body = request(
            "https://[2606:4700:4700::1111]/cdn-cgi/trace",
            server_name="one.one.one.one",
            maximum=8192,
        )
        fields = dict(
            line.split("=", 1) for line in body.decode("ascii").splitlines() if "=" in line
        )
        return status == 200 and ipaddress.ip_address(fields.get("ip", "")).version == 6
    except (OSError, ValueError, http.client.HTTPException):
        return False


def probe(endpoint_index: int, transport: str, *, bulk: bool = True) -> tuple[PathResult, str]:
    def identity() -> tuple[bool, str, float | None]:
        for url in ("https://www.cloudflare.com/cdn-cgi/trace", "https://api.ipify.org/"):
            started = monotonic()
            try:
                status, body = request(url, maximum=8192)
                if status != 200:
                    continue
                candidate = body.decode("ascii").strip()
                if "cdn-cgi" in url:
                    candidate = next(
                        (line[3:] for line in candidate.splitlines() if line.startswith("ip=")), ""
                    )
                address = str(ipaddress.ip_address(candidate))
                return True, address, (monotonic() - started) * 1000
            except (OSError, ValueError, http.client.HTTPException):
                continue
        return False, "", None

    def dns() -> bool:
        try:
            return dns_check()
        except (OSError, http.client.HTTPException):
            return False

    def download() -> bool:
        try:
            status, data = request(f"https://speed.cloudflare.com/__down?bytes={BULK_BYTES}")
            return status == 200 and len(data) >= BULK_BYTES
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def upload() -> bool:
        try:
            status, _ = request(
                "https://speed.cloudflare.com/__up",
                body=secrets.token_bytes(BULK_BYTES),
                maximum=8192,
            )
            return 200 <= status < 300
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def udp() -> bool:
        try:
            return udp_check()
        except OSError:
            return False

    # Each exchange owns its sockets/timeouts. Joining the bounded pool, even
    # when a worker raises, is mandatory before callers may change the engine.
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="harness-health") as pool:
        identity_result = pool.submit(identity)
        dns_result, udp_result = pool.submit(dns), pool.submit(udp)
        ipv6_result = pool.submit(ipv6_check)
        tcp_ok, external_ip, latency = identity_result.result()
        down_result, up_result = (
            (pool.submit(download), pool.submit(upload)) if bulk and tcp_ok else (None, None)
        )
        dns_ok, udp_ok = dns_result.result(), udp_result.result()
        down_ok = down_result.result() if down_result is not None else True
        up_ok = up_result.result() if up_result is not None else True
        bulk_ok = tcp_ok and down_ok and up_ok
        ipv6_ok = ipv6_result.result()
    return PathResult(
        endpoint_index, transport, tcp_ok, dns_ok, bulk_ok, udp_ok, latency, ipv6_ok
    ), external_ip


def metrics(token: str, port: int) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/connections", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        data = response.read(2 * 1024 * 1024 + 1)
        if response.status != 200 or len(data) > 2 * 1024 * 1024:
            raise TunnelError("Счётчики движка недоступны")
        try:
            parsed = json.loads(data)
        except ValueError:
            raise TunnelError("Счётчики движка недоступны") from None
        if not isinstance(parsed, dict) or any(
            type(parsed.get(key)) is not int or not 0 <= parsed[key] <= 2**63 - 1
            for key in ("downloadTotal", "uploadTotal")
        ):
            raise TunnelError("Счётчики движка недоступны")
        # Connection destinations are never returned or persisted.
        return {"rx_bytes": int(parsed["downloadTotal"]), "tx_bytes": int(parsed["uploadTotal"])}
    except (OSError, http.client.HTTPException):
        raise TunnelError("Счётчики движка недоступны") from None
    finally:
        connection.close()
