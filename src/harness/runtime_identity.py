from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from importlib.metadata import version as distribution_version
from pathlib import Path

_RUNTIME_HASH_CHUNK_BYTES = 128 * 1024
_RUNTIME_HASH_MAX_BYTES = 16 * 1024 * 1024


class RuntimeIdentityError(RuntimeError):
    """Raised when the installed Harness runtime cannot be fingerprinted safely."""


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Immutable identity for one loaded Harness installation/runtime."""

    package_version: str
    python_executable: str
    code_sha256: str


def _runtime_source_files(package_root: Path) -> tuple[Path, ...]:
    try:
        files = tuple(
            sorted(
                (path for path in package_root.rglob("*.py") if "__pycache__" not in path.parts),
                key=lambda path: path.relative_to(package_root).as_posix(),
            )
        )
    except OSError as exc:
        raise RuntimeIdentityError("Harness runtime package could not be enumerated") from exc
    if not files:
        raise RuntimeIdentityError("Harness runtime package contains no Python source files")
    return files


def current_runtime_identity() -> RuntimeIdentity:
    """Fingerprint the current installed Harness Python code without following file symlinks."""
    package_root = Path(__file__).parent
    try:
        root_metadata = package_root.lstat()
    except OSError as exc:
        raise RuntimeIdentityError("Harness runtime package root could not be inspected") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeIdentityError("Harness runtime package root is unsafe")

    digest = hashlib.sha256()
    total_bytes = 0
    files = _runtime_source_files(package_root)
    relative_files = tuple(path.relative_to(package_root).as_posix() for path in files)

    for path in files:
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeIdentityError(
                "Harness runtime package file could not be inspected"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeIdentityError(
                "Harness runtime package contains an unsafe Python source entry"
            )
        total_bytes += metadata.st_size
        if total_bytes > _RUNTIME_HASH_MAX_BYTES:
            raise RuntimeIdentityError("Harness runtime package exceeds the fingerprint byte limit")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(metadata.st_size.to_bytes(8, "big"))
        file_bytes = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_RUNTIME_HASH_CHUNK_BYTES):
                    file_bytes += len(chunk)
                    if file_bytes > metadata.st_size:
                        raise RuntimeIdentityError(
                            "Harness runtime package changed during fingerprinting"
                        )
                    digest.update(chunk)
            final_metadata = path.lstat()
        except RuntimeIdentityError:
            raise
        except OSError as exc:
            raise RuntimeIdentityError("Harness runtime package file could not be read") from exc
        if (
            final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or stat.S_ISLNK(final_metadata.st_mode)
            or not stat.S_ISREG(final_metadata.st_mode)
        ):
            raise RuntimeIdentityError("Harness runtime package changed during fingerprinting")

    try:
        final_root_metadata = package_root.lstat()
    except OSError as exc:
        raise RuntimeIdentityError(
            "Harness runtime package root could not be re-inspected"
        ) from exc
    if (
        stat.S_ISLNK(final_root_metadata.st_mode)
        or not stat.S_ISDIR(final_root_metadata.st_mode)
        or final_root_metadata.st_dev != root_metadata.st_dev
        or final_root_metadata.st_ino != root_metadata.st_ino
        or tuple(
            path.relative_to(package_root).as_posix()
            for path in _runtime_source_files(package_root)
        )
        != relative_files
    ):
        raise RuntimeIdentityError("Harness runtime package changed during fingerprinting")

    try:
        package_version = distribution_version("harness")
    except Exception as exc:
        raise RuntimeIdentityError("Harness package version could not be inspected") from exc

    return RuntimeIdentity(
        package_version=package_version,
        python_executable=os.path.abspath(sys.executable),
        code_sha256=digest.hexdigest(),
    )
