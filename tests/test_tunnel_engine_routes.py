"""Actual admitted engine route/DNS regressions using only loopback fixtures, no TUN."""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import os
import socket
import ssl
import struct
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import harness.tunnel_health as health
from harness.tunnel_config import compile_client, runtime_settings
from harness.tunnel_service import ServiceRuntime

ENGINE = os.environ.get("HARNESS_TEST_ENGINE")
pytestmark = pytest.mark.skipif(
    not ENGINE, reason="requires admitted HARNESS_TEST_ENGINE scratch build"
)


def free_port() -> int:
    with socket.socket() as peer:
        peer.bind(("127.0.0.1", 0))
        return int(peer.getsockname()[1])


@contextlib.contextmanager
def engine(
    root: Path, name: str, config: dict[str, Any], port: int, *, crash: bool = False
) -> Iterator[None]:
    assert ENGINE
    path = root / (name + ".json")
    path.write_text(json.dumps(config))
    with (root / (name + ".log")).open("w") as output:
        process = subprocess.Popen([ENGINE, "run", "-c", str(path)], stdout=output, stderr=output)
        try:
            for _ in range(100):
                assert process.poll() is None, (root / (name + ".log")).read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("engine listener did not start")
            yield
        finally:
            if crash:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=5)


def client(
    root: Path,
    exceptions: list[str],
    *,
    lan: list[str] | None = None,
    cache: Path | None = None,
    ipv6_available: bool = True,
) -> dict[str, Any]:
    assert ENGINE
    temporary = ServiceRuntime.__new__(ServiceRuntime)
    temporary.engine = Path(ENGINE)
    private = temporary._new_endpoint()
    private.update(
        certificate=subprocess.check_output(
            [ENGINE, "generate", "tls-keypair", "fixture.invalid"]
        ).decode(),
        public_certificate=False,
    )
    settings = runtime_settings(
        {
            "name": "Fixture",
            "endpoints": [{"host": "203.0.113.10"}],
            "exceptions": [
                {"kind": "cidr" if "/" in value else "domain", "value": value}
                for value in exceptions
            ],
            "lan_bypass": bool(lan),
        }
    )
    config = compile_client(
        settings,
        {"endpoints": [private]},
        endpoint_index=0,
        tcp_transport="reality",
        udp_transport="hysteria2",
        lan_routes=lan or [],
        metrics_token="synthetic-runtime-token",
        fakeip_cache_path=str(cache or root / "cache.db"),
        ipv6_available=ipv6_available,
    )
    if cache is None:
        config.pop("experimental")
    else:
        config["experimental"].pop("clash_api")
    config["log"] = {"level": "debug"}
    return config


def test_ipv4_fallback_configuration_is_admitted_and_preserves_direct_ipv6(tmp_path: Path) -> None:
    config = client(tmp_path, ["2001:db8:abcd::/48"], lan=["fd00:1234::/64"], ipv6_available=False)
    path = tmp_path / "checked.json"
    path.write_text(json.dumps(config))
    assert ENGINE
    subprocess.run([ENGINE, "check", "-c", str(path)], check=True, capture_output=True)
    rules = config["route"]["rules"]
    reject = next(i for i, rule in enumerate(rules) if rule.get("ip_version") == 6)
    sniff = next(i for i, rule in enumerate(rules) if rule.get("action") == "sniff")
    assert reject < sniff  # SYN must fail before sniffing accepts a stream.
    assert rules[reject]["no_drop"] is True  # bursts cannot turn refusal into timeout
    assert any(
        rule.get("outbound") == "direct"
        and rule.get("ip_cidr") == ["2001:db8:abcd::/48", "fd00:1234::/64"]
        for rule in rules[:reject]
    )
    assert config["dns"]["strategy"] == "ipv4_only"


def test_dead_candidate_cannot_pass_bypass_health_and_healthy_candidate_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both TCP/UDP candidate tags traverse a real second SOCKS engine when healthy."""
    pem, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(pem),
            "-days",
            "1",
            "-subj",
            "/CN=*.cloudflare.com",
            "-addext",
            "subjectAltName=DNS:*.cloudflare.com,DNS:api.ipify.org,DNS:cloudflare-dns.com,DNS:one.one.one.one,IP:1.1.1.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(pem, key)
    stop = Event()
    errors: list[BaseException] = []
    uploads: list[int] = []
    dns_queries: list[bytes] = []
    with socket.socket() as listener, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stun:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        stun.bind(("127.0.0.1", 0))
        stun.settimeout(0.2)
        tls_port = listener.getsockname()[1]

        def serve() -> None:
            try:
                while not stop.is_set():
                    try:
                        peer, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with peer, tls.wrap_socket(peer, server_side=True) as secure:
                        file = secure.makefile("rb")
                        first = file.readline().decode().split()[1]
                        headers = {}
                        while line := file.readline().strip():
                            k, v = line.split(b":", 1)
                            headers[k.lower()] = v.strip()
                        body = file.read(int(headers.get(b"content-length", b"0")))
                        if first.startswith("/dns-query"):
                            assert headers[b"accept"] == b"application/dns-message"
                            raw = parse_qs(urlsplit(first).query)["dns"][0]
                            query = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                            dns_queries.append(query)
                            answer = (
                                query[:2]
                                + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
                                + query[12:]
                                + b"\xc0\x0c"
                                + struct.pack("!HHIH", 1, 1, 60, 4)
                                + socket.inet_aton("104.16.132.229")
                            )
                        elif first.startswith("/cdn-cgi/trace"):
                            answer = b"ip=198.51.100.99\n"
                        elif first.startswith("/__down"):
                            answer = b"x" * health.BULK_BYTES
                        elif first.startswith("/__up"):
                            uploads.append(len(body))
                            answer = b"ok"
                        else:
                            answer = b"198.51.100.99"
                        secure.sendall(
                            f"HTTP/1.1 200 OK\r\nContent-Length: {len(answer)}\r\nConnection: close\r\n\r\n".encode()
                            + answer
                        )
                        file.close()
            except BaseException as exc:
                errors.append(exc)

        def udp() -> None:
            while not stop.is_set():
                try:
                    data, address = stun.recvfrom(4096)
                except TimeoutError:
                    continue
                stun.sendto(struct.pack("!HHI", 0x0101, 0, 0x2112A442) + data[8:20], address)

        threads = [Thread(target=serve, daemon=True), Thread(target=udp, daemon=True)]
        for thread in threads:
            thread.start()
        original_socks, original_context = health.socks_connect, ssl.create_default_context
        proxy_port, candidate_port = free_port(), free_port()
        monkeypatch.setattr(health, "SOCKS_PORT", proxy_port)
        monkeypatch.setattr(health, "STUN_PORT", stun.getsockname()[1])
        monkeypatch.setattr(
            health,
            "socks_connect",
            lambda host, port: original_socks(
                "127.0.0.1" if host == "1.1.1.1" else host, tls_port, proxy_port=proxy_port
            ),
        )
        monkeypatch.setattr(
            ssl, "create_default_context", lambda: original_context(cafile=str(pem))
        )
        config = client(tmp_path, ["cloudflare.com", "ipify.org", "127.0.0.0/8"])
        config["inbounds"] = [
            {"type": "socks", "tag": "probe-in", "listen": "127.0.0.1", "listen_port": proxy_port}
        ]
        names = {
            name: "127.0.0.1"
            for name in (
                "www.cloudflare.com",
                "api.ipify.org",
                "speed.cloudflare.com",
                "stun.cloudflare.com",
            )
        }
        config["dns"]["servers"] = [
            {"type": "hosts", "tag": tag, "predefined": names}
            for tag in ("direct-dns", "tunnel-dns")
        ]
        config["dns"]["rules"] = []
        config["outbounds"] = [
            {"type": "socks", "tag": tag, "server": "127.0.0.1", "server_port": candidate_port}
            for tag in ("reality", "hysteria2", "naive")
        ] + [config["outbounds"][-1]]
        relay = {
            "dns": {
                "servers": [{"type": "hosts", "tag": "local", "predefined": names}],
                "final": "local",
            },
            "inbounds": [
                {
                    "type": "socks",
                    "tag": "candidate",
                    "listen": "127.0.0.1",
                    "listen_port": candidate_port,
                }
            ],
            "outbounds": [{"type": "direct", "tag": "direct"}],
            "route": {"default_domain_resolver": "local", "final": "direct"},
        }
        try:
            with engine(tmp_path, "dead-health", config, proxy_port):
                result, _ = health.probe(0, "reality")
                assert (
                    not result.usable
                    and not result.tcp_ok
                    and not result.dns_ok
                    and not result.udp_ok
                )
            assert not uploads and not dns_queries
            with (
                engine(tmp_path, "candidate", relay, candidate_port),
                engine(tmp_path, "healthy-health", config, proxy_port),
            ):
                result, external_ip = health.probe(0, "reality")
                assert result.usable and result.udp_ok
                assert external_ip == "198.51.100.99"
            assert uploads == [health.BULK_BYTES] and len(dns_queries) == 1
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        assert not errors


def socks_numeric(
    port: int, command: int, address: str, target_port: int
) -> tuple[socket.socket, str, int]:
    peer = socket.create_connection(("127.0.0.1", port), timeout=3)
    peer.sendall(b"\x05\x01\x00")
    assert health.receive(peer, 2) == b"\x05\x00"
    ip = ipaddress.ip_address(address)
    peer.sendall(
        b"\x05"
        + bytes([command, 0, 1 if ip.version == 4 else 4])
        + ip.packed
        + struct.pack("!H", target_port)
    )
    header = health.receive(peer, 4)
    assert header[:2] == b"\x05\x00"
    size = 4 if header[3] == 1 else 16
    remote = str(ipaddress.ip_address(health.receive(peer, size)))
    return peer, remote, struct.unpack("!H", health.receive(peer, 2))[0]


@pytest.mark.parametrize("case", ["cidr4", "cidr6", "lan", "numeric", "domain"])
def test_fakeip_resolves_before_cidr_or_lan_and_preserves_domain_direct(
    tmp_path: Path, case: str
) -> None:
    version = 6 if case == "cidr6" else 4
    real_address = "::1" if version == 6 else "127.0.0.1"
    with socket.socket(socket.AF_INET6 if version == 6 else socket.AF_INET) as listener:
        listener.bind((real_address, 0))
        listener.listen()
        listener.settimeout(3)
        target_port = listener.getsockname()[1]

        def echo() -> None:
            peer, _ = listener.accept()
            with peer:
                peer.recv(4096)
                peer.sendall(b"DIRECT FIXTURE")

        thread = Thread(target=echo, daemon=True)
        thread.start()
        cidr = "::1/128" if version == 6 else "127.0.0.0/8"
        config = client(
            tmp_path,
            ["cloudflare.com"] if case == "domain" else [] if case == "lan" else [cidr],
            lan=[cidr] if case == "lan" else [],
        )
        port = free_port()
        config["inbounds"] = [
            {"type": "socks", "tag": "tun-in", "listen": "127.0.0.1", "listen_port": port}
        ]
        config["dns"]["servers"] = [config["dns"]["servers"][0]] + [
            {
                "type": "hosts",
                "tag": tag,
                "predefined": {
                    "www.cloudflare.com": "192.0.2.100"
                    if case == "domain" and tag == "tunnel-dns"
                    else real_address
                },
            }
            for tag in ("direct-dns", "tunnel-dns")
        ]
        dead = free_port()
        config["outbounds"] = [
            {"type": "socks", "tag": tag, "server": "127.0.0.1", "server_port": dead}
            for tag in ("reality", "hysteria2", "naive")
        ] + [config["outbounds"][-1]]
        with engine(tmp_path, "fakeip-" + case, config, port):
            address = real_address
            if case != "numeric":
                control, relay, relay_port = socks_numeric(port, 3, "0.0.0.0", 0)
                with control, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
                    peer.settimeout(3)
                    query = (
                        b"\x12\x34"
                        + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)
                        + b"\x03www\x0acloudflare\x03com\x00"
                        + struct.pack("!HH", 28 if version == 6 else 1, 1)
                    )
                    frame = (
                        b"\x00\x00\x00\x01"
                        + socket.inet_aton("1.1.1.1")
                        + struct.pack("!H", 53)
                        + query
                    )
                    peer.sendto(frame, ("127.0.0.1" if relay == "0.0.0.0" else relay, relay_port))
                    answer, _ = peer.recvfrom(4096)
                    address = str(
                        ipaddress.ip_address(answer[-16:] if version == 6 else answer[-4:])
                    )
                    assert address != real_address
            peer, _, _ = socks_numeric(port, 1, address, target_port)
            with peer:
                peer.sendall(b"GET / HTTP/1.1\r\nHost: www.cloudflare.com\r\n\r\n")
                assert peer.recv(4096) == b"DIRECT FIXTURE"
        thread.join(timeout=3)
        assert not thread.is_alive()
