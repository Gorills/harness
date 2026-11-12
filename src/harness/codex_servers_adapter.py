"""Explicit ownership-aware global Codex Servers delivery; preserves unrelated TOML bytes."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness.builtin_skills import BUILTIN_SKILLS

_BEGIN = "# Harness Servers owned begin\n"
_END = "# Harness Servers owned end\n"
_OWNER = ".harness-servers-owner.json"
_LOCK = ".harness-servers.lock"
_LOCK_CONTENT = b"Harness Servers lifecycle lock v1\n"
_LOCK_TIMEOUT = 5.0


class GlobalServersSetupError(RuntimeError):
    pass


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and path.is_junction())


def _read(path: Path) -> bytes | None:
    if _is_link(path):
        raise GlobalServersSetupError("Global setup refuses symlink targets")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise GlobalServersSetupError("Global setup requires bounded regular files")
    return path.read_bytes()


def _private_parent(path: Path) -> None:
    for parent in (path, *path.parents):
        if _is_link(parent):
            raise GlobalServersSetupError("Global setup refuses symlink parents")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _acquire_lifecycle_lock(path: Path, deadline: float) -> int:
    """Use a persistent owned inode; unlinking it would split holders from waiters."""
    _private_parent(path.parent)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        if _is_link(path) or not path.is_file():
            raise GlobalServersSetupError("Global setup refuses unknown lifecycle lock") from None
        fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or opened.st_nlink != 1
            or identity != (current.st_dev, current.st_ino)
            or _is_link(path)
        ):
            raise GlobalServersSetupError("Global setup refuses changed lifecycle lock")
        if os.name == "nt":
            from harness.windows_fs import (
                WindowsFileSecurityError,
                require_private_windows_path,
                secure_owned_windows_path,
            )

            try:
                if created:
                    secure_owned_windows_path(path, directory=False)
                require_private_windows_path(path, directory=False)
            except WindowsFileSecurityError as exc:
                raise GlobalServersSetupError("Global setup refuses unsafe lifecycle lock") from exc
        elif opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600:
            raise GlobalServersSetupError("Global setup refuses unknown lifecycle lock")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GlobalServersSetupError(
                        "Global setup/remove busy; no target changes made; retry later"
                    ) from exc
                time.sleep(min(0.05, remaining))
        # A replaced/unlinked lock cannot coordinate the actual target surfaces.
        for parent in path.parents:
            if _is_link(parent):
                raise GlobalServersSetupError("Global setup refuses symlink parents")
        current = path.lstat()
        if _is_link(path) or identity != (current.st_dev, current.st_ino):
            raise GlobalServersSetupError("Global setup refuses changed lifecycle lock")
        if created:
            if os.write(fd, _LOCK_CONTENT) != len(_LOCK_CONTENT):
                raise GlobalServersSetupError("Global setup could not initialize lifecycle lock")
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, len(_LOCK_CONTENT) + 1) != _LOCK_CONTENT:
                raise GlobalServersSetupError("Global setup refuses unknown lifecycle lock")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _lifecycle_locks(codex_home: Path, home: Path) -> Iterator[None]:
    # Both scopes matter: HOME can share skills across CODEX_HOMEs, and conversely.
    paths = {codex_home / _LOCK, home / ".agents/skills" / _LOCK}
    deadline = time.monotonic() + _LOCK_TIMEOUT
    held: list[int] = []
    try:
        for path in sorted(paths, key=lambda item: os.path.normcase(str(item))):
            held.append(_acquire_lifecycle_lock(path, deadline))
        yield
    except OSError as exc:
        raise GlobalServersSetupError("Global setup/remove filesystem operation failed") from exc
    finally:
        for fd in reversed(held):
            os.close(fd)


def _atomic(path: Path, data: bytes, expected: bytes | None, skill_file: Path) -> None:
    _private_parent(path.parent)
    fd, name = tempfile.mkstemp(prefix=".harness-servers-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if os.name == "nt":
                from harness.windows_fs import WindowsFileSecurityError, secure_owned_windows_path

                try:
                    secure_owned_windows_path(Path(name), directory=False)
                except WindowsFileSecurityError as exc:
                    raise GlobalServersSetupError(
                        "Global setup refuses unsafe temporary file"
                    ) from exc
            else:
                os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Preparing the temp file can be slow. Recheck after it, just before commit.
        # This bounds the external-writer window; it is not an atomic filesystem CAS.
        _check_target(path, expected, skill_file, prepared_file=Path(name))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _check_target(
    path: Path, expected: bytes | None, skill_file: Path, *, prepared_file: Path | None = None
) -> None:
    for parent in path.parents:
        if _is_link(parent):
            raise GlobalServersSetupError("Global setup refuses symlink parents")
    if path == skill_file and path.parent.exists():
        owned_names = {"SKILL.md"} if expected is not None else set()
        if prepared_file is not None:
            owned_names.add(prepared_file.name)
        if (
            _is_link(path.parent)
            or not path.parent.is_dir()
            or set(item.name for item in path.parent.iterdir()) != owned_names
        ):
            raise GlobalServersSetupError("Global server skill ownership changed")
    if _read(path) != expected:
        raise GlobalServersSetupError("Global setup target changed; reconcile manually")


def _delete(path: Path, expected: bytes | None, skill_file: Path) -> None:
    _check_target(path, expected, skill_file)
    path.unlink(missing_ok=True)


def _restore_previous(
    snapshots: dict[Path, bytes | None],
    written: dict[Path, bytes | None],
    skill_file: Path,
    skill_existed: bool,
) -> bool:
    restored = True
    for target, payload in reversed(written.items()):
        try:
            # Undo only our bytes/deletion; a later operator edit belongs to the operator.
            snapshot = snapshots[target]
            if snapshot is None:
                _delete(target, payload, skill_file)
            else:
                _atomic(target, snapshot, payload, skill_file)
        except (OSError, GlobalServersSetupError):
            restored = False
    try:
        if not skill_existed and skill_file.parent.exists():
            _check_target(skill_file, None, skill_file)
            skill_file.parent.rmdir()
        for target, snapshot in snapshots.items():
            _check_target(target, snapshot, skill_file)
    except (OSError, GlobalServersSetupError):
        restored = False
    return restored


def setup_global_servers(
    codex_home: Path, home: Path, url: str, token: str, *, remove: bool = False
) -> None:
    """Called by explicit production lifecycle or tests with synthetic absolute homes."""
    if (
        not codex_home.is_absolute()
        or not home.is_absolute()
        or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/servers/mcp", url)
        or not token
        or not token.isascii()
    ):
        raise GlobalServersSetupError("Invalid global setup target")
    # Normalize lexical aliases before choosing the shared lock order; links are refused.
    codex_home = Path(os.path.abspath(codex_home))
    home = Path(os.path.abspath(home))
    with _lifecycle_locks(codex_home, home):
        _setup_global_servers(codex_home, home, url, token, remove=remove)


def _setup_global_servers(
    codex_home: Path, home: Path, url: str, token: str, *, remove: bool
) -> None:
    config = codex_home / "config.toml"
    marker = codex_home / _OWNER
    config_original = _read(config)
    original = config_original or b""
    owner_raw = _read(marker)
    owner = {} if owner_raw is None else json.loads(owner_raw)
    try:
        text = original.decode()
        parsed = tomllib.loads(text)
    except (UnicodeError, ValueError) as exc:
        raise GlobalServersSetupError("Invalid Codex configuration; no changes made") from exc
    begin = text.find(_BEGIN)
    end = text.find(_END)
    old = ""
    if begin >= 0 or end >= 0:
        if begin < 0 or end < begin or text.count(_BEGIN) != 1 or text.count(_END) != 1:
            raise GlobalServersSetupError("Global setup ownership block is invalid")
        end += len(_END)
        old = text[begin:end]
        if owner.get("config_sha256") != hashlib.sha256(old.encode()).hexdigest():
            raise GlobalServersSetupError("Owned global MCP block was edited; reconcile manually")
    elif "harness-servers" in parsed.get("mcp_servers", {}):
        raise GlobalServersSetupError("User-owned harness-servers MCP entry already exists")
    skill = home / ".agents/skills/server-operations"
    skill_file = skill / "SKILL.md"
    skill_original = _read(skill_file)
    desired_skill = next(s for s in BUILTIN_SKILLS if s.skill_id == "server-operations").files()[
        "SKILL.md"
    ]
    if skill.exists():
        if (
            _is_link(skill)
            or not skill.is_dir()
            or set(item.name for item in skill.iterdir()) != {"SKILL.md"}
            or skill_original is None
            or owner.get("skill_sha256") != hashlib.sha256(skill_original).hexdigest()
        ):
            raise GlobalServersSetupError(
                "User-owned or edited global server skill; no changes made"
            )
    if remove and not owner:
        return
    block = (
        _BEGIN
        + "[mcp_servers.harness-servers]\nurl = "
        + json.dumps(url)
        + "\nenabled = true\nrequired = false\nhttp_headers = { Authorization = "
        + json.dumps("Bearer " + token)
        + " }\n"
        + _END
    )
    new_text = (
        text[:begin] + block + text[end:]
        if old
        else text + ("\n" if text and not text.endswith("\n") else "") + block
    )
    tomllib.loads(new_text)
    # Preflight all parents before modifying either surface; preserve unrelated TOML bytes.
    for target in (config, marker, skill_file):
        for parent in target.parents:
            if _is_link(parent):
                raise GlobalServersSetupError("Global setup refuses symlink parents")
    snapshots = {
        skill_file: skill_original,
        config: config_original,
        marker: owner_raw,
    }
    desired_marker = json.dumps(
        {
            "version": 1,
            "separator_added": owner.get(
                "separator_added", bool(text and not text.endswith("\n") and not old)
            ),
            "config_sha256": hashlib.sha256(block.encode()).hexdigest(),
            "skill_sha256": hashlib.sha256(desired_skill).hexdigest(),
        }
    ).encode()
    targets: tuple[tuple[Path, bytes | None], ...]
    if remove:
        prefix = text[:begin]
        if owner.get("separator_added") and prefix.endswith("\n"):
            prefix = prefix[:-1]
        new_text = prefix + text[end:] if old else text
        targets = (
            (config, new_text.encode() if new_text != text else config_original),
            (skill_file, None),
            (marker, None),
        )
    else:
        targets = (
            (skill_file, desired_skill),
            (config, new_text.encode()),
            (marker, desired_marker),
        )
    skill_existed = skill.exists()
    written: dict[Path, bytes | None] = {}
    try:
        for target, payload in targets:
            _check_target(target, snapshots[target], skill_file)
            if payload is None:
                _delete(target, snapshots[target], skill_file)
            else:
                _atomic(target, payload, snapshots[target], skill_file)
            written[target] = payload
            if remove and target == skill_file and skill.exists():
                _check_target(skill_file, None, skill_file)
                skill.rmdir()
    except (OSError, GlobalServersSetupError) as exc:
        restored = _restore_previous(snapshots, written, skill_file, skill_existed)
        raise GlobalServersSetupError(
            "Global setup/remove failed; previous content restored"
            if restored
            else "Global setup/remove failed; concurrent changes preserved or restore incomplete; reconcile manually"
        ) from exc
