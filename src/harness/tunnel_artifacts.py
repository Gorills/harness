# ruff: noqa: RUF001
"""Admission of exact, audited network artifacts; never install an unchecked latest."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from harness.tunnel_platform import PlatformError, require_owned


class ArtifactError(PlatformError):
    pass


def verify_bundle(root: Path, *, platform: str, required: tuple[str, ...]) -> dict[str, Any]:
    """The installer publishes a root-owned approved manifest and immutable files.

    Unapproved audit candidates may be inspected in disposable test namespaces,
    but no production call converts their presence into approval.
    """
    require_owned(root, directory=True)
    manifest_path = root / "manifest.json"
    require_owned(manifest_path)
    if manifest_path.stat().st_size > 65536:
        raise ArtifactError("Манифест сетевого пакета превышает допустимый размер")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ArtifactError("Не удалось проверить манифест сетевого пакета") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        - {"schema", "version", "platform", "audit", "components", "system_packages"}
        or manifest.get("schema") != 1
        or manifest.get("platform") != platform
    ):
        raise ArtifactError("Сетевой пакет не подходит этой платформе")
    audit = manifest["audit"]
    if (
        not isinstance(audit, dict)
        or audit.get("status") != "approved"
        or not all(
            isinstance(audit.get(key), str) and audit[key]
            for key in ("checked_at", "evidence", "reviewer")
        )
    ):
        raise ArtifactError("Сетевой пакет ещё не прошёл аудит зависимостей и платформы")
    components = manifest["components"]
    if (
        not isinstance(components, dict)
        or not set(required) <= set(components)
        or len(components) > 8
    ):
        raise ArtifactError("В сетевом пакете отсутствуют необходимые компоненты")
    for name, component in components.items():
        if name not in {
            "sing-box",
            "server-engine",
            "naive",
            "libcronet",
            "haproxy",
            "wintun",
        } or not isinstance(component, dict):
            raise ArtifactError("Неизвестный компонент сетевого пакета")
        if set(component) != {"file", "sha256", "version", "source", "audit_status"}:
            raise ArtifactError("Неполные сведения о сетевом компоненте")
        filename = component["file"]
        if (
            not isinstance(filename, str)
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
        ):
            raise ArtifactError("Недопустимое имя сетевого компонента")
        if component["audit_status"] != "approved":
            raise ArtifactError("Компонент сетевого пакета не прошёл аудит")
        path = root / filename
        require_owned(path)
        if path.stat().st_size > 256 * 1024 * 1024:
            raise ArtifactError("Сетевой компонент превышает допустимый размер")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != component["sha256"]:
            raise ArtifactError("Контрольная сумма сетевого компонента изменилась")
    return manifest


def component_path(root: Path, manifest: dict[str, Any], name: str) -> Path:
    path = root / str(manifest["components"][name]["file"])
    require_owned(path)
    if os.name != "nt" and not stat.S_IMODE(path.stat().st_mode) & 0o111:
        raise ArtifactError("Сетевой компонент не является исполняемым")
    return path
