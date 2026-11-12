"""Explicit, offline sharing of client transport access; never administrative secrets."""

from __future__ import annotations

import base64
import ipaddress
import json
from typing import Any

from harness.tunnels import TunnelError

PHONE_PREFIX = "HT1:"
MAX_PHONE_BYTES = 2800


def phone_profile(profile: dict[str, Any]) -> str:
    """Copy an allowlist from privileged applied state, without changing that state."""
    endpoints = profile.get("secrets", {}).get("endpoints", [])
    settings = profile.get("settings", {})
    targets = settings.get("endpoints", [])
    if profile.get("removing") or not endpoints or len(endpoints) != len(targets):
        raise TunnelError("Сначала завершите подготовку VPS")
    if not all(endpoint.get("prepared") for endpoint in endpoints):
        raise TunnelError("Сначала завершите подготовку VPS")
    private = endpoints[0]
    host = str(ipaddress.ip_address(targets[0]["host"]))
    # Keep a deliberately separate public transport schema. Do not serialize secrets.
    value = {
        "v": 1,
        "name": settings["name"],
        "host": host,
        "r": {
            "u": private["uuid"],
            "k": private["reality_public_key"],
            "s": private["reality_server_name"],
            "i": private["reality_short_id"],
        },
        "h": {
            "p": private["hysteria_password"],
            "o": private["obfs_password"],
            "s": host if private.get("public_certificate") else private["certificate_server_name"],
            "c": "" if private.get("public_certificate") else private["certificate"],
        },
    }
    encoded = PHONE_PREFIX + base64.urlsafe_b64encode(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    if len(encoded) > MAX_PHONE_BYTES:
        raise TunnelError("Профиль слишком большой для одного QR-кода")
    return encoded


def phone_qr(value: str) -> list[list[bool]]:
    """Return pixels only to the explicit POST response, never an observation."""
    import qrcode

    if (
        not isinstance(value, str)
        or not value.startswith(PHONE_PREFIX)
        or len(value) > MAX_PHONE_BYTES
    ):
        raise TunnelError("Некорректный профиль телефона")
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=4)
    code.add_data(value, optimize=0)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.get_matrix()]
