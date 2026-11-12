# ruff: noqa: RUF001, RUF012
"""Native WFP guard. ABI definitions follow the Microsoft Windows SDK.

Filters live in a dedicated sublayer. Its exception permits are soft permits,
so another firewall provider can still deny traffic. Persistent ALE policy and
separate boot-time transport policy survive process failure and reboot.
"""

from __future__ import annotations

import ctypes as C
import ipaddress
import os
import uuid
from pathlib import Path
from typing import Any

from harness.tunnel_platform import PlatformError

U8, U16, U32, U64 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_uint64
P = C.c_void_p


class Guid(C.Structure):
    _fields_ = [("data1", U32), ("data2", U16), ("data3", U16), ("data4", U8 * 8)]

    @classmethod
    def parse(cls, value: str) -> Guid:
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class Blob(C.Structure):
    _fields_ = [("size", U32), ("data", C.POINTER(U8))]


class ValueUnion(C.Union):
    _fields_ = [("uint8", U8), ("uint16", U16), ("uint32", U32), ("pointer", P)]


class Value(C.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", U32), ("value", ValueUnion)]


class Display(C.Structure):
    _fields_ = [("name", C.c_wchar_p), ("description", C.c_wchar_p)]


class Provider(C.Structure):
    _fields_ = [
        ("key", Guid),
        ("display", Display),
        ("flags", U32),
        ("data", Blob),
        ("service", C.c_wchar_p),
    ]


class Sublayer(C.Structure):
    _fields_ = [
        ("key", Guid),
        ("display", Display),
        ("flags", U32),
        ("provider", C.POINTER(Guid)),
        ("data", Blob),
        ("weight", U16),
    ]


class Condition(C.Structure):
    _fields_ = [("key", Guid), ("match", U32), ("value", Value)]


class Action(C.Structure):
    _fields_ = [("type", U32), ("key", Guid)]


class Context(C.Union):
    _fields_ = [("raw", U64), ("key", Guid)]


class Filter(C.Structure):
    _fields_ = [
        ("key", Guid),
        ("display", Display),
        ("flags", U32),
        ("provider", C.POINTER(Guid)),
        ("data", Blob),
        ("layer", Guid),
        ("sublayer", Guid),
        ("weight", Value),
        ("count", U32),
        ("conditions", C.POINTER(Condition)),
        ("action", Action),
        ("context", Context),
        ("reserved", C.POINTER(Guid)),
        ("id", U64),
        ("effective_weight", Value),
    ]


class V4Mask(C.Structure):
    _fields_ = [("address", U32), ("mask", U32)]


class V6Mask(C.Structure):
    _fields_ = [("address", U8 * 16), ("prefix", U8)]


PROVIDER_KEY = "6cf6a290-75d9-4d57-9f82-03c9e39dd734"
SUBLAYER_KEY = "6262dd72-74c6-4a0d-8675-068735266a83"
LAYERS = {
    ("ale", 4): "c38d57d1-05a7-4c33-904f-7fbceee60e82",
    ("ale", 6): "4a72393b-319f-44bc-84c3-ba54dcb3b6b4",
    ("transport", 4): "09e61aea-d214-46e2-9b21-b26b0b2f28c8",
    ("transport", 6): "e1735bde-013f-4655-b351-a49e15762df0",
}
CONDITIONS = {
    "app": "d78e1e87-8644-4ea5-9437-d809ecefc971",
    "address": "b235ae9a-1d64-49b8-a44c-5ff3d9095045",
    "protocol": "3971ef2b-623e-4f9a-8cb1-6e79b806b9a7",
    "interface": "4cd62a49-59c3-4969-b7f3-bda5d32890a4",
    "flags": "632ce23b-5167-435c-86d7-e903684aa80c",
    "local_port": "0c1ba1af-5765-453f-af22-a8f791ac775b",
    "remote_port": "c35a604d-d22b-4e1a-91b4-68f674ee674b",
}


def filter_key(index: int) -> Guid:
    return Guid.parse(str(uuid.uuid5(uuid.UUID(PROVIDER_KEY), str(index))))


def policy_rules(
    apps: list[str], lan_routes: list[str], interface: int | None
) -> list[dict[str, Any]]:
    """Pure policy description, usable by ABI/policy tests on other platforms."""
    if not apps or len(apps) > 4 or len(lan_routes) > 32:
        raise PlatformError("Некорректная политика Windows")
    rules: list[dict[str, Any]] = []
    for version in (4, 6):
        addresses = [
            str(ipaddress.ip_network(value))
            for value in lan_routes
            if ipaddress.ip_network(value).version == version
        ]
        if any(
            not ipaddress.ip_network(value).is_private or ipaddress.ip_network(value).prefixlen == 0
            for value in addresses
        ):
            raise PlatformError("Недопустимое локальное исключение Windows")
        for family, flags in (("ale", 1), ("transport", 2)):
            base = {"family": family, "version": version, "flags": flags}
            rules.append({**base, "permit": True, "conditions": [("flags", "flags", 1)]})
            for address in addresses:
                rules.append(
                    {**base, "permit": True, "conditions": [("address", "network", address)]}
                )
            dhcp = [
                ("protocol", "u8", 17),
                ("local_port", "u16", 68 if version == 4 else 546),
                ("remote_port", "u16", 67 if version == 4 else 547),
            ]
            rules.append({**base, "permit": True, "conditions": dhcp})
            if family == "ale":
                for app in apps:
                    rules.append({**base, "permit": True, "conditions": [("app", "app", app)]})
                if interface is not None:
                    rules.append(
                        {**base, "permit": True, "conditions": [("interface", "u64", interface)]}
                    )
            if version == 6:
                # ICMP_TYPE aliases IP_LOCAL_PORT in the SDK. NDP must work
                # before the service can recreate its TUN after reboot.
                for icmp_type in (133, 134, 135, 136):
                    rules.append(
                        {
                            **base,
                            "permit": True,
                            "conditions": [
                                ("protocol", "u8", 58),
                                ("local_port", "u16", icmp_type),
                            ],
                        }
                    )
            rules.append({**base, "permit": False, "conditions": []})
        # Runtime transport filters reject unsupported IP protocols while ALE
        # owns app/interface permissions for TCP/UDP. App-id is NOT supported
        # at transport/boot layers.
        base = {"family": "transport", "version": version, "flags": 1}
        rules.append({**base, "permit": True, "conditions": [("flags", "flags", 1)]})
        for protocol in (6, 17):
            rules.append({**base, "permit": True, "conditions": [("protocol", "u8", protocol)]})
        for address in addresses:
            rules.append({**base, "permit": True, "conditions": [("address", "network", address)]})
        if version == 6:
            for icmp_type in (133, 134, 135, 136):
                rules.append(
                    {
                        **base,
                        "permit": True,
                        "conditions": [("protocol", "u8", 58), ("local_port", "u16", icmp_type)],
                    }
                )
        rules.append({**base, "permit": False, "conditions": []})
    if len(rules) > 512:
        raise PlatformError("Политика Windows превышает допустимый размер")
    return rules


class WindowsGuard:
    def __init__(self, applications: list[Path]) -> None:
        if os.name != "nt":
            raise PlatformError("WFP доступен только на Windows")
        self.applications = [str(path) for path in applications]
        self.api = C.WinDLL("fwpuclnt.dll", use_last_error=True)  # type: ignore[attr-defined]
        declarations = {
            "FwpmEngineOpen0": ([C.c_wchar_p, U32, P, P, C.POINTER(P)], U32),
            "FwpmEngineClose0": ([P], U32),
            "FwpmTransactionBegin0": ([P, U32], U32),
            "FwpmTransactionCommit0": ([P], U32),
            "FwpmTransactionAbort0": ([P], U32),
            "FwpmProviderAdd0": ([P, C.POINTER(Provider), P], U32),
            "FwpmSubLayerAdd0": ([P, C.POINTER(Sublayer), P], U32),
            "FwpmFilterAdd0": ([P, C.POINTER(Filter), P, C.POINTER(U64)], U32),
            "FwpmFilterDeleteByKey0": ([P, C.POINTER(Guid)], U32),
            "FwpmFilterGetByKey0": ([P, C.POINTER(Guid), C.POINTER(C.POINTER(Filter))], U32),
            "FwpmGetAppIdFromFileName0": ([C.c_wchar_p, C.POINTER(C.POINTER(Blob))], U32),
            "FwpmFreeMemory0": ([C.POINTER(P)], None),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _check(code: int, *, exists: bool = False, missing: bool = False) -> None:
        if code == 0 or (exists and code == 0x80320009) or (missing and code == 0x80320003):
            return
        raise PlatformError("Windows не применила сетевую защиту")

    def _open(self) -> Any:
        handle = P()
        self._check(self.api.FwpmEngineOpen0(None, 10, None, None, C.byref(handle)))
        return handle

    def _delete_filters(self, handle: Any) -> None:
        for index in range(512):
            self._check(
                self.api.FwpmFilterDeleteByKey0(handle, C.byref(filter_key(index))), missing=True
            )

    def _condition(self, specification: tuple[str, str, Any], retained: list[Any]) -> Condition:
        field, kind, raw = specification
        value = Value()
        target: Any
        match = 0
        if kind in {"u8", "u16", "flags"}:
            value.type = {"u8": 1, "u16": 2, "flags": 3}[kind]
            setattr(value, {"u8": "uint8", "u16": "uint16", "flags": "uint32"}[kind], raw)
            if kind == "flags":
                match = 6
        elif kind == "u64":
            target = U64(raw)
            retained.append(target)
            value.type, value.pointer = 4, C.cast(C.pointer(target), P)
        elif kind == "network":
            network = ipaddress.ip_network(raw)
            if network.version == 4:
                target = V4Mask(int(network.network_address), int(network.netmask))
                value.type = 0x100
            else:
                target = V6Mask(
                    (U8 * 16).from_buffer_copy(network.network_address.packed), network.prefixlen
                )
                value.type = 0x101
            retained.append(target)
            value.pointer = C.cast(C.pointer(target), P)
        elif kind == "app":
            blob = C.POINTER(Blob)()
            self._check(self.api.FwpmGetAppIdFromFileName0(raw, C.byref(blob)))
            retained.append(blob)
            value.type, value.pointer = 12, C.cast(blob, P)
        else:
            raise PlatformError("Неизвестное условие сетевой политики")
        return Condition(Guid.parse(CONDITIONS[field]), match, value)

    def arm(self, lan_routes: list[str], interface: int | None = None) -> None:
        rules = policy_rules(self.applications, lan_routes, interface)
        handle = self._open()
        try:
            self._check(self.api.FwpmTransactionBegin0(handle, 0))
            provider_key = Guid.parse(PROVIDER_KEY)
            provider = Provider(
                provider_key,
                Display("Harness Tunnel", "Owned tunnel network policy"),
                1,
                Blob(),
                "HarnessTunnel",
            )
            self._check(self.api.FwpmProviderAdd0(handle, C.byref(provider), None), exists=True)
            sublayer = Sublayer(
                Guid.parse(SUBLAYER_KEY),
                Display("Harness Tunnel", None),
                1,
                C.pointer(provider_key),
                Blob(),
                0x7000,
            )
            self._check(self.api.FwpmSubLayerAdd0(handle, C.byref(sublayer), None), exists=True)
            self._delete_filters(handle)
            for index, rule in enumerate(rules):
                retained: list[Any] = []
                try:
                    conditions = (Condition * len(rule["conditions"]))(
                        *[
                            self._condition(specification, retained)
                            for specification in rule["conditions"]
                        ]
                    )
                    weight = U64(1000 if rule["permit"] else 1)
                    value = Value(4, ValueUnion(pointer=C.cast(C.pointer(weight), P)))
                    policy = Filter(
                        filter_key(index),
                        Display("Harness Tunnel rule", None),
                        rule["flags"],
                        C.pointer(provider_key),
                        Blob(),
                        Guid.parse(LAYERS[(rule["family"], rule["version"])]),
                        Guid.parse(SUBLAYER_KEY),
                        value,
                        len(conditions),
                        conditions,
                        Action(0x1002 if rule["permit"] else 0x1001, Guid()),
                        Context(),
                        None,
                        0,
                        Value(),
                    )
                    self._check(self.api.FwpmFilterAdd0(handle, C.byref(policy), None, None))
                finally:
                    for target in retained:
                        if isinstance(target, C.POINTER(Blob)):
                            memory = C.cast(target, P)
                            self.api.FwpmFreeMemory0(C.byref(memory))
            self._check(self.api.FwpmTransactionCommit0(handle))
        except BaseException:
            self.api.FwpmTransactionAbort0(handle)
            raise
        finally:
            self.api.FwpmEngineClose0(handle)

    def active(self) -> bool:
        handle = self._open()
        found: set[tuple[bytes, int]] = set()
        try:
            for index in range(512):
                result = C.POINTER(Filter)()
                code = self.api.FwpmFilterGetByKey0(
                    handle, C.byref(filter_key(index)), C.byref(result)
                )
                if code == 0x80320003:
                    break
                if code != 0:
                    return False
                try:
                    policy = result.contents
                    if (
                        policy.action.type == 0x1001
                        and policy.count == 0
                        and bytes(policy.sublayer) == bytes(Guid.parse(SUBLAYER_KEY))
                        and policy.provider
                        and bytes(policy.provider.contents) == bytes(Guid.parse(PROVIDER_KEY))
                    ):
                        found.add((bytes(policy.layer), policy.flags))
                finally:
                    if result:
                        memory = C.cast(result, P)
                        self.api.FwpmFreeMemory0(C.byref(memory))
            expected = {
                (bytes(Guid.parse(LAYERS[(family, version)])), flags)
                for version in (4, 6)
                for family, flags in (("ale", 1), ("transport", 1), ("transport", 2))
            }
            return expected <= found
        finally:
            self.api.FwpmEngineClose0(handle)

    def disarm(self) -> None:
        handle = self._open()
        try:
            self._check(self.api.FwpmTransactionBegin0(handle, 0))
            self._delete_filters(handle)
            self._check(self.api.FwpmTransactionCommit0(handle))
        except BaseException:
            self.api.FwpmTransactionAbort0(handle)
            raise
        finally:
            self.api.FwpmEngineClose0(handle)

    def lan_routes(self) -> list[str]:
        import json
        import subprocess

        from harness.tunnel_config import connected_lan_routes

        executable = (
            Path(os.environ.get("WINDIR", r"C:\Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        script = "$indices=@(Get-NetAdapter -Physical | Where-Object Status -eq Up | Select-Object -ExpandProperty ifIndex); @(Get-NetIPAddress | Where-Object {$_.InterfaceIndex -in $indices -and $_.AddressState -eq 'Preferred'} | Select-Object IPAddress,PrefixLength,InterfaceAlias) | ConvertTo-Json -Compress"
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode or len(result.stdout) > 65536:
            raise PlatformError("Не удалось определить локальные интерфейсы Windows")
        data = json.loads(result.stdout or b"[]")
        if isinstance(data, dict):
            data = [data]
        return connected_lan_routes(
            [
                {
                    "dst": str(
                        ipaddress.ip_network(
                            f"{item['IPAddress']}/{item['PrefixLength']}", strict=False
                        )
                    ),
                    "dev": item["InterfaceAlias"],
                    "scope": "link",
                    "protocol": "kernel",
                }
                for item in data
            ]
        )
