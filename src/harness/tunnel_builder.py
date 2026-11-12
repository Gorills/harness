# ruff: noqa: RUF001
"""Unprivileged, pinned, reproducible engine build; no system Go installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

from harness.tunnel_artifacts import ArtifactError

RESOURCES = Path(__file__).with_name("tunnel_recipe")


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _resource(spec: dict[str, Any]) -> bytes:
    relative = PurePosixPath(spec["file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ArtifactError("Недопустимый ресурс сборки")
    path = RESOURCES.joinpath(*relative.parts)
    if _hash(path) != spec["sha256"]:
        raise ArtifactError("Рецепт сетевой сборки изменён")
    return path.read_bytes()


def fetch_archive(spec: dict[str, Any], path: Path) -> None:
    allowed = {
        "codeload.github.com",
        "go.dev",
        "dl.google.com",
        "proxy.golang.org",
        "storage.googleapis.com",
    }
    url = urllib.parse.urlsplit(spec["url"])
    if url.scheme != "https" or url.hostname not in allowed or url.username:
        raise ArtifactError("Недопустимый источник сборки")
    try:
        with urllib.request.urlopen(spec["url"], timeout=60) as response, path.open("xb") as target:
            final = urllib.parse.urlsplit(response.url)
            if final.scheme != "https" or final.hostname not in allowed:
                raise ArtifactError("Недопустимое перенаправление сборки")
            size = 0
            while data := response.read(1024 * 1024):
                size += len(data)
                if size > 512 * 1024 * 1024:
                    raise ArtifactError("Архив сборки слишком велик")
                target.write(data)
    except OSError as error:
        raise ArtifactError("Не удалось загрузить проверенный исходный код") from error
    if _hash(path) != spec["sha256"]:
        raise ArtifactError("Контрольная сумма исходного кода не совпала")


def extract_archive(archive: Path, destination: Path, prefix: str) -> None:
    """Extract only bounded regular members beneath the expected exact prefix."""
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with (
        zipfile.ZipFile(archive) if zipfile.is_zipfile(archive) else tarfile.open(archive) as source
    ):
        members = source.infolist() if isinstance(source, zipfile.ZipFile) else source.getmembers()
        if len(members) > 60000:
            raise ArtifactError("В архиве сборки слишком много файлов")
        for member in members:
            name = member.filename if isinstance(member, zipfile.ZipInfo) else member.name
            is_directory = (
                member.is_dir() if isinstance(member, zipfile.ZipInfo) else member.isdir()
            )
            if is_directory and name.rstrip("/") == prefix.rstrip("/"):
                continue
            if not name.startswith(prefix):
                raise ArtifactError("Неожиданный префикс исходного архива")
            relative = PurePosixPath(name[len(prefix) :])
            if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
                raise ArtifactError("Недопустимый путь исходного архива")
            if not relative.parts or str(relative) == ".":
                continue
            target = destination.joinpath(*relative.parts)
            directory = member.is_dir() if isinstance(member, zipfile.ZipInfo) else member.isdir()
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            if isinstance(member, tarfile.TarInfo) and not member.isfile():
                raise ArtifactError("Ссылки в исходном архиве запрещены")
            if (
                isinstance(member, zipfile.ZipInfo)
                and (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ArtifactError("Ссылки в исходном архиве запрещены")
            size = member.file_size if isinstance(member, zipfile.ZipInfo) else member.size
            total += size
            if size > 128 * 1024 * 1024 or total > 1024 * 1024 * 1024:
                raise ArtifactError("Распакованный исходный архив слишком велик")
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = (
                source.open(cast(zipfile.ZipInfo, member))
                if isinstance(source, zipfile.ZipFile)
                else source.extractfile(cast(tarfile.TarInfo, member))
            )
            if stream is None:
                raise ArtifactError("Не удалось прочитать исходный файл")
            with stream, target.open("xb") as output:
                shutil.copyfileobj(stream, output)
            executable = (
                (member.external_attr >> 16) & 0o111
                if isinstance(member, zipfile.ZipInfo)
                else member.mode & 0o111
            )
            target.chmod(0o755 if executable else 0o644)


def build_engine(specification: dict[str, Any], destination: Path) -> None:
    """Only product catalog callers may admit the exact expected artifact SHA."""
    recipe_path = RESOURCES / "engine-source-build-recipe.json"
    if _hash(recipe_path) != specification["recipe_sha256"]:
        raise ArtifactError("Рецепт сетевого пакета не совпал с допущенным")
    recipe = json.loads(recipe_path.read_bytes())
    with tempfile.TemporaryDirectory(prefix="harness-engine-build-") as temporary:
        scratch = Path(temporary)
        source = scratch / "source"
        archive = scratch / "source.tar.gz"
        fetch_archive(recipe["source"], archive)
        extract_archive(archive, source, recipe["source"]["member_prefix"])
        for replacement in recipe["replacements"]:
            (source / replacement["target"]).write_bytes(_resource(replacement))
        for index, override in enumerate(recipe["module_overrides"]):
            archive = scratch / f"module-{index}.zip"
            fetch_archive(override["source"], archive)
            module = source / override["destination"]
            extract_archive(archive, module, override["source"]["member_prefix"])
            for replacement in override["file_replacements"]:
                target = module / replacement["target"]
                original = replacement["original_sha256"]
                if (original is None and target.exists()) or (
                    original and _hash(target) != original
                ):
                    raise ArtifactError("Исходный файл патча изменён")
                target.write_bytes(_resource(replacement))
        tool = recipe["toolchains"][f"{sys.platform}-amd64"]
        archive = scratch / "toolchain.archive"
        fetch_archive(tool, archive)
        extract_archive(archive, scratch / "toolchain/go", tool["member_prefix"])
        go = scratch / "toolchain" / tool["go_binary"]
        output = scratch / "engine"
        target_os = specification["target_os"]
        if target_os not in {"linux", "windows"}:
            raise ArtifactError("Недопустимая платформа сборки")
        substitutions = {
            "${scratch}": str(scratch),
            "${go_binary}": str(go),
            "${output}": str(output),
            "${target_os}": target_os,
        }

        def expand(value: str) -> str:
            for key, item in substitutions.items():
                value = value.replace(key, item)
            return value

        environment = {key: expand(value) for key, value in recipe["build"]["env"].items()}
        environment["PATH"] = (
            str(go.parent)
            + os.pathsep
            + (
                os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\System32"
                if os.name == "nt"
                else "/usr/bin:/bin"
            )
        )
        if os.name == "nt":
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
            environment["TEMP"] = str(scratch / "tmp")
        environment["GOROOT"] = str(go.parent.parent)
        for directory in ("gopath", "gocache", "gomodcache", "tmp"):
            (scratch / directory).mkdir()
        resolved = subprocess.run(
            [str(go), "list", "-m", "-json", "all"],
            cwd=source,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=900,
            check=False,
        )
        if resolved.returncode or len(resolved.stdout) > 8 * 1024 * 1024:
            raise ArtifactError("Не удалось подтвердить граф зависимостей сборки")
        remaining = resolved.stdout.decode()
        modules = []
        decoder = json.JSONDecoder()
        while remaining.strip():
            module, end = decoder.raw_decode(remaining.lstrip())
            modules.append((module["Path"], module.get("Version", "")))
            remaining = remaining.lstrip()[end:]
        locked = json.loads(_resource(recipe["resolved_modules"]))
        if sorted(modules) != sorted(
            (module["Path"], module.get("Version", "")) for module in locked
        ):
            raise ArtifactError("Граф зависимостей не совпал с проверенным")
        result = subprocess.run(
            [expand(value) for value in recipe["build"]["argv"]],
            cwd=source,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1800,
            check=False,
        )
        if result.returncode or not output.is_file() or _hash(output) != specification["sha256"]:
            raise ArtifactError("Сборка не совпала с проверенным сетевым пакетом")
        shutil.copyfile(output, destination)
        destination.chmod(0o755)
