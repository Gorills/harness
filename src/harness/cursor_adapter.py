from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.host_adapters import (
    HostIntegrationError,
    HostRegistrationCollisionError,
    HostRegistrationState,
    IntegrationChange,
    cursor_skill_projection_surface,
)
from harness.skills import SkillProjectionSurface
from harness.workspace_resolution import WorkspaceHint, WorkspaceHintMatchMode

_CURSOR_PROFILE = "cursor"
_HOST_PROFILE_ENV = "HARNESS_HOST_PROFILE"
_WORKSPACE_ROOT_ENV = "HARNESS_WORKSPACE_ROOT"
_SERVER_NAME = "harness"
_WORKSPACE_FOLDER = "${workspaceFolder}"
_OWNER_MARKER = ".harness-mcp-owner.json"
_OWNER_VERSION = 1
_EXCLUDE_BEGIN = "# BEGIN HARNESS CURSOR MCP"
_EXCLUDE_END = "# END HARNESS CURSOR MCP"
_EXCLUDE_BODY = ("/.cursor/mcp.json", f"/.cursor/{_OWNER_MARKER}")
_GIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class CursorRegistrationDiagnostic:
    """Read-only details for one Cursor Harness MCP registration."""

    path: Path
    state: HostRegistrationState
    expected_python: Path
    configured_python: str | None
    configured_workspace_root: str | None
    preflight_error: str | None = None


@dataclass(frozen=True, slots=True)
class _ConfigSnapshot:
    path: Path
    raw: bytes | None
    value: dict[str, object]
    mode: int


@dataclass(frozen=True, slots=True)
class _OwnerMarker:
    workspace_root: str
    exclude_owned: bool


@dataclass(frozen=True, slots=True)
class CursorAdapter:
    """Cursor's official JSON MCP configuration for the local IDE and CLI."""

    home: Path
    python_executable: Path

    @property
    def profile(self) -> str:
        return _CURSOR_PROFILE

    @property
    def global_config_path(self) -> Path:
        return self.home / ".cursor" / "mcp.json"

    def workspace_hints(self, environment: Mapping[str, str]) -> tuple[WorkspaceHint, ...]:
        configured = environment.get(_WORKSPACE_ROOT_ENV)
        if not configured:
            raise HostIntegrationError(
                "Cursor integration did not provide HARNESS_WORKSPACE_ROOT from ${workspaceFolder}"
            )
        root = _workspace_root(Path(configured))
        return (
            WorkspaceHint(
                path=root,
                source="cursor-workspace-folder",
                match_mode=WorkspaceHintMatchMode.ROOT,
            ),
        )

    def skill_projection_surface(self) -> SkillProjectionSurface:
        return cursor_skill_projection_surface()

    def registration_state(self) -> HostRegistrationState:
        return self.registration_diagnostic().state

    def registration_diagnostic(self) -> CursorRegistrationDiagnostic:
        """Inspect the global Harness MCP entry without mutating Cursor configuration."""
        return self._registration_diagnostic(self.global_config_path, self._desired_global())

    def project_registration_state(self, workspace_root: Path) -> HostRegistrationState:
        root = _workspace_root(workspace_root)
        return self._registration_state(self._project_config(root), self._project_desired())

    def project_registration_diagnostic(self, workspace_root: Path) -> CursorRegistrationDiagnostic:
        """Inspect one Workspace override, including ownership/adoption preflight errors."""
        root = _workspace_root(workspace_root)
        diagnostic = self._registration_diagnostic(
            self._project_config(root), self._project_desired()
        )
        try:
            self.preflight_project_reconcile(root)
        except HostIntegrationError as exc:
            return CursorRegistrationDiagnostic(
                path=diagnostic.path,
                state=diagnostic.state,
                expected_python=diagnostic.expected_python,
                configured_python=diagnostic.configured_python,
                configured_workspace_root=diagnostic.configured_workspace_root,
                preflight_error=str(exc),
            )
        return diagnostic

    def register_mcp(self) -> IntegrationChange:
        return self._ensure_entry(self.global_config_path, self._desired_global())

    def unregister_mcp(self) -> IntegrationChange:
        # Harness owns only mcpServers.harness globally. There is no durable proof that Harness
        # created the surrounding user config, so preserve the file even when the object is empty.
        return self._remove_entry(self.global_config_path, delete_empty_owned_file=False)

    def preflight_project_reconcile(self, workspace_root: Path) -> None:
        root = _workspace_root(workspace_root)
        path = self._project_config(root)
        state = self._registration_state(path, self._project_desired())
        if state is HostRegistrationState.FOREIGN:
            raise HostRegistrationCollisionError(
                f"Cursor project config already has a non-Harness MCP server named 'harness': {path}"
            )
        if (
            _git_is_tracked(root, path.relative_to(root))
            and state is not HostRegistrationState.CURRENT
        ):
            raise HostIntegrationError(
                "tracked Cursor project config requires manual adoption of the exact Harness entry: "
                f"{path}"
            )
        marker_path = self._owner_marker_path(root)
        if _git_is_tracked(root, marker_path.relative_to(root)):
            raise HostIntegrationError(
                f"tracked Harness Cursor ownership marker requires manual cleanup: {marker_path}"
            )
        self._read_owner_marker(root)

    def preflight_project_remove(self, workspace_root: Path) -> None:
        root = _workspace_root(workspace_root)
        path = self._project_config(root)
        state = self._registration_state(path, self._project_desired())
        if state is HostRegistrationState.FOREIGN:
            raise HostRegistrationCollisionError(
                f"Cursor project config has a non-Harness MCP server named 'harness': {path}"
            )
        if (
            _git_is_tracked(root, path.relative_to(root))
            and state is not HostRegistrationState.ABSENT
        ):
            raise HostIntegrationError(
                "tracked Cursor project config requires manual removal of the Harness entry: "
                f"{path}"
            )
        marker_path = self._owner_marker_path(root)
        marker = self._read_owner_marker(root)
        if marker is not None and _git_is_tracked(root, marker_path.relative_to(root)):
            raise HostIntegrationError(
                f"tracked Harness Cursor ownership marker requires manual cleanup: {marker_path}"
            )

    def reconcile_project(self, workspace_root: Path) -> IntegrationChange:
        root = _workspace_root(workspace_root)
        self.preflight_project_reconcile(root)
        path = self._project_config(root)
        state = self._registration_state(path, self._project_desired())
        if state is HostRegistrationState.CURRENT:
            return IntegrationChange.UNCHANGED

        marker = self._read_owner_marker(root)
        created_file = not _path_exists(path)
        if created_file:
            exclude_changed = _ensure_cursor_exclude(root)
            if marker is None:
                marker = _OwnerMarker(workspace_root=str(root), exclude_owned=exclude_changed)
                self._write_owner_marker(root, marker)
            elif exclude_changed and not marker.exclude_owned:
                marker = _OwnerMarker(workspace_root=str(root), exclude_owned=True)
                self._write_owner_marker(root, marker)
        try:
            return self._ensure_entry(path, self._project_desired())
        except Exception:
            # Leave a valid marker/exclude pair after an interrupted creation. It proves only
            # Harness-owned intent for this absent/new config and lets a retry recover safely.
            raise

    def remove_project(self, workspace_root: Path) -> IntegrationChange:
        root = _workspace_root(workspace_root)
        self.preflight_project_remove(root)
        marker = self._read_owner_marker(root)
        change = self._remove_entry(
            self._project_config(root),
            delete_empty_owned_file=marker is not None,
        )
        if marker is not None:
            if marker.exclude_owned:
                successor = self._transfer_exclude_ownership(root)
                if successor is None:
                    _remove_cursor_exclude(root)
            marker_path = self._owner_marker_path(root)
            snapshot = _read_config_bytes(marker_path)
            if snapshot is not None:
                _delete_if_unchanged(marker_path, snapshot)
        return change

    def _transfer_exclude_ownership(self, workspace_root: Path) -> Path | None:
        """Transfer shared Git exclude ownership to another Harness-created worktree."""
        candidates: list[tuple[Path, _OwnerMarker]] = []
        for root in _linked_worktree_roots(workspace_root):
            if root == workspace_root:
                continue
            marker = self._read_owner_marker(root)
            if marker is None:
                continue
            marker_path = self._owner_marker_path(root)
            if _git_is_tracked(root, marker_path.relative_to(root)):
                raise HostIntegrationError(
                    "tracked Harness Cursor ownership marker prevents exclude ownership transfer: "
                    f"{marker_path}"
                )
            candidates.append((root, marker))
        if not candidates:
            return None
        successor_root, successor = min(candidates, key=lambda item: str(item[0]))
        if not successor.exclude_owned:
            self._write_owner_marker(
                successor_root,
                _OwnerMarker(workspace_root=str(successor_root), exclude_owned=True),
            )
        return successor_root

    def _desired_global(self) -> dict[str, object]:
        return {
            "type": "stdio",
            "command": str(self.python_executable),
            "args": ["-m", "harness.mcp_process"],
            "env": {_HOST_PROFILE_ENV: _CURSOR_PROFILE},
        }

    def _project_desired(self) -> dict[str, object]:
        return {
            "type": "stdio",
            "command": str(self.python_executable),
            "args": ["-m", "harness.mcp_process"],
            "env": {
                _HOST_PROFILE_ENV: _CURSOR_PROFILE,
                _WORKSPACE_ROOT_ENV: _WORKSPACE_FOLDER,
            },
        }

    def _registration_state(
        self, path: Path, desired: Mapping[str, object]
    ) -> HostRegistrationState:
        return self._registration_diagnostic(path, desired).state

    def _registration_diagnostic(
        self, path: Path, desired: Mapping[str, object]
    ) -> CursorRegistrationDiagnostic:
        snapshot = _read_json_config(path)
        servers = _servers(snapshot.value, path, create=False)
        if servers is None or _SERVER_NAME not in servers:
            return CursorRegistrationDiagnostic(
                path=path,
                state=HostRegistrationState.ABSENT,
                expected_python=self.python_executable,
                configured_python=None,
                configured_workspace_root=None,
            )
        entry = servers[_SERVER_NAME]
        owned = _is_owned_entry(entry)
        configured_python: str | None = None
        configured_workspace_root: str | None = None
        if owned and isinstance(entry, dict):
            command = entry.get("command")
            if isinstance(command, str):
                configured_python = command
            env = entry.get("env")
            if isinstance(env, dict):
                observed_root = env.get(_WORKSPACE_ROOT_ENV)
                if isinstance(observed_root, str):
                    configured_workspace_root = observed_root
        if not owned:
            state = HostRegistrationState.FOREIGN
        elif entry == dict(desired):
            state = HostRegistrationState.CURRENT
        else:
            state = HostRegistrationState.STALE_OWNED
        return CursorRegistrationDiagnostic(
            path=path,
            state=state,
            expected_python=self.python_executable,
            configured_python=configured_python,
            configured_workspace_root=configured_workspace_root,
        )

    def _ensure_entry(self, path: Path, desired: Mapping[str, object]) -> IntegrationChange:
        snapshot = _read_json_config(path)
        servers = _servers(snapshot.value, path, create=True)
        assert servers is not None
        observed = servers.get(_SERVER_NAME)
        if observed is not None:
            if not _is_owned_entry(observed):
                raise HostRegistrationCollisionError(
                    f"Cursor config already has a non-Harness MCP server named 'harness': {path}"
                )
            if observed == dict(desired):
                return IntegrationChange.UNCHANGED
        servers[_SERVER_NAME] = dict(desired)
        replacement = _encode_json(snapshot.value)
        _replace_if_unchanged(path, snapshot.raw, replacement, snapshot.mode)
        return IntegrationChange.CHANGED

    def _remove_entry(self, path: Path, *, delete_empty_owned_file: bool) -> IntegrationChange:
        snapshot = _read_json_config(path)
        servers = _servers(snapshot.value, path, create=False)
        if servers is None or _SERVER_NAME not in servers:
            return IntegrationChange.UNCHANGED
        observed = servers[_SERVER_NAME]
        if not _is_owned_entry(observed):
            raise HostRegistrationCollisionError(
                f"Cursor MCP server named 'harness' is not owned by Harness: {path}"
            )
        del servers[_SERVER_NAME]
        if delete_empty_owned_file and snapshot.value == {"mcpServers": {}}:
            assert snapshot.raw is not None
            _delete_if_unchanged(path, snapshot.raw)
        else:
            _replace_if_unchanged(path, snapshot.raw, _encode_json(snapshot.value), snapshot.mode)
        return IntegrationChange.CHANGED

    def _project_config(self, workspace_root: Path) -> Path:
        return workspace_root / ".cursor" / "mcp.json"

    def _owner_marker_path(self, workspace_root: Path) -> Path:
        return workspace_root / ".cursor" / _OWNER_MARKER

    def _read_owner_marker(self, workspace_root: Path) -> _OwnerMarker | None:
        path = self._owner_marker_path(workspace_root)
        raw = _read_config_bytes(path)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostIntegrationError(f"Cursor ownership marker is malformed: {path}") from exc
        expected_keys = {"version", "workspace_root", "exclude_owned"}
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise HostIntegrationError(f"Cursor ownership marker is malformed: {path}")
        if value.get("version") != _OWNER_VERSION:
            raise HostIntegrationError(f"Cursor ownership marker version is unsupported: {path}")
        configured_root = value.get("workspace_root")
        exclude_owned = value.get("exclude_owned")
        if configured_root != str(workspace_root) or not isinstance(exclude_owned, bool):
            raise HostIntegrationError(f"Cursor ownership marker does not match Workspace: {path}")
        return _OwnerMarker(workspace_root=configured_root, exclude_owned=exclude_owned)

    def _write_owner_marker(self, workspace_root: Path, marker: _OwnerMarker) -> None:
        path = self._owner_marker_path(workspace_root)
        value = {
            "version": _OWNER_VERSION,
            "workspace_root": marker.workspace_root,
            "exclude_owned": marker.exclude_owned,
        }
        raw = _encode_json(value)
        existing = _read_config_bytes(path)
        if existing is None:
            _replace_if_unchanged(path, None, raw, 0o600)
            return
        if existing == raw:
            return
        current = self._read_owner_marker(workspace_root)
        if current is None or current.workspace_root != marker.workspace_root:
            raise HostIntegrationError(f"Cursor ownership marker changed unexpectedly: {path}")
        if current.exclude_owned and not marker.exclude_owned:
            raise HostIntegrationError(
                f"Cursor ownership marker cannot relinquish exclude ownership: {path}"
            )
        mode = stat.S_IMODE(path.lstat().st_mode)
        _replace_if_unchanged(path, existing, raw, mode)


def discover_cursor_adapter(
    *,
    environment: Mapping[str, str] | None = None,
    python_executable: Path | None = None,
) -> CursorAdapter:
    values = os.environ if environment is None else environment
    configured = values.get("HOME")
    home = Path.home() if not configured else Path(configured).expanduser()
    home = Path(os.path.abspath(home))
    python = Path(os.path.abspath(os.fspath(python_executable or sys.executable)))
    return CursorAdapter(home=home, python_executable=python)


def _workspace_root(path: Path) -> Path:
    try:
        root = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HostIntegrationError(f"Cursor Workspace root cannot be resolved: {path}") from exc
    if not root.is_dir():
        raise HostIntegrationError(f"Cursor Workspace root is not a directory: {root}")
    return root


def _is_owned_entry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    env = value.get("env")
    return (
        value.get("type") == "stdio"
        and isinstance(value.get("command"), str)
        and value.get("args") == ["-m", "harness.mcp_process"]
        and isinstance(env, dict)
        and env.get(_HOST_PROFILE_ENV) == _CURSOR_PROFILE
    )


def _read_json_config(path: Path) -> _ConfigSnapshot:
    raw = _read_config_bytes(path)
    if raw is None:
        return _ConfigSnapshot(path=path, raw=None, value={}, mode=0o600)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostIntegrationError(f"Cursor MCP config is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise HostIntegrationError(f"Cursor MCP config top level must be an object: {path}")
    mode = stat.S_IMODE(path.lstat().st_mode)
    return _ConfigSnapshot(path=path, raw=raw, value=value, mode=mode)


def _read_config_bytes(path: Path) -> bytes | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HostIntegrationError(f"Cursor integration path cannot be inspected: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HostIntegrationError(f"Cursor integration path is not a real regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HostIntegrationError(f"Cursor integration file cannot be read: {path}") from exc


def _servers(value: dict[str, object], path: Path, *, create: bool) -> dict[str, object] | None:
    observed = value.get("mcpServers")
    if observed is None:
        if not create:
            return None
        servers: dict[str, object] = {}
        value["mcpServers"] = servers
        return servers
    if not isinstance(observed, dict):
        raise HostIntegrationError(f"Cursor MCP config mcpServers must be an object: {path}")
    return observed


def _encode_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _require_parent_safe(path: Path) -> None:
    parent = path.parent
    if parent.exists():
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise HostIntegrationError(
                f"Cursor config directory cannot be inspected: {parent}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise HostIntegrationError(f"Cursor config directory is unsafe: {parent}")
        return
    try:
        parent.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        raise HostIntegrationError(f"Cursor config directory cannot be created: {parent}") from exc


def _replace_if_unchanged(
    path: Path, expected: bytes | None, replacement: bytes, mode: int
) -> None:
    _require_parent_safe(path)
    fd, temporary_name = tempfile.mkstemp(prefix=".harness-cursor-", dir=path.parent)
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if expected is None:
            if not _move_if_absent(temporary, path):
                raise HostRegistrationCollisionError(
                    f"Cursor config appeared before Harness mutation: {path}"
                )
            _fsync_directory(path.parent)
            return

        current = _read_config_bytes(path)
        if current != expected:
            raise HostRegistrationCollisionError(f"Cursor config changed before mutation: {path}")
        backup = _unused_sibling(path, ".harness-cursor-backup-")
        if not _move_if_absent(path, backup):
            raise HostIntegrationError(f"Cursor config backup path appeared: {backup}")
        moved = _read_config_bytes(backup)
        if moved != expected:
            _restore_backup(path, backup)
            backup = None
            raise HostRegistrationCollisionError(f"Cursor config changed during mutation: {path}")
        if not _move_if_absent(temporary, path):
            _restore_backup(path, backup, preserve_if_occupied=True)
            backup = None
            raise HostRegistrationCollisionError(
                f"Cursor config appeared during mutation; previous content was preserved: {path}"
            )
        backup.unlink()
        backup = None
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        # A backup that cannot be safely restored is deliberately left on disk with its exact
        # prior bytes. Never delete recovery evidence merely to make cleanup look tidy.


def _delete_if_unchanged(path: Path, expected: bytes) -> None:
    current = _read_config_bytes(path)
    if current is None:
        return
    if current != expected:
        raise HostRegistrationCollisionError(
            f"Cursor integration file changed before removal: {path}"
        )
    backup = _unused_sibling(path, ".harness-cursor-delete-")
    if not _move_if_absent(path, backup):
        raise HostIntegrationError(f"Cursor removal backup path appeared: {backup}")
    if _read_config_bytes(backup) != expected:
        _restore_backup(path, backup)
        raise HostRegistrationCollisionError(
            f"Cursor integration file changed during removal: {path}"
        )
    backup.unlink()
    _fsync_directory(path.parent)


def _restore_backup(path: Path, backup: Path, *, preserve_if_occupied: bool = False) -> None:
    if _path_exists(path):
        if preserve_if_occupied:
            return
        raise HostIntegrationError(
            f"Cursor integration recovery could not restore {path}; backup preserved at {backup}"
        )
    try:
        restored = _move_if_absent(backup, path)
    except OSError as exc:
        raise HostIntegrationError(
            f"Cursor integration recovery failed; backup preserved at {backup}"
        ) from exc
    if not restored:
        raise HostIntegrationError(
            f"Cursor integration recovery could not restore {path}; backup preserved at {backup}"
        )


def _unused_sibling(path: Path, prefix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    os.close(fd)
    candidate = Path(name)
    candidate.unlink()
    return candidate


def _move_if_absent(source: Path, target: Path) -> bool:
    if os.name == "nt":
        raise HostIntegrationError("Cursor production MCP configuration currently requires POSIX")
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise HostIntegrationError("atomic no-clobber rename is unavailable") from exc
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, target_bytes, 1)
    else:
        renamex_np = getattr(library, "renamex_np", None)
        if renamex_np is None:
            raise HostIntegrationError("atomic no-clobber rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, target_bytes, 0x00000004)
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    raise OSError(error_number, os.strerror(error_number), target)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostIntegrationError(f"Cursor integration path cannot be inspected: {path}") from exc
    return True


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostIntegrationError("Git could not inspect Cursor project configuration") from exc


def _linked_worktree_roots(workspace_root: Path) -> tuple[Path, ...]:
    completed = _git(workspace_root, "worktree", "list", "--porcelain")
    if completed.returncode != 0:
        raise HostIntegrationError("Git linked worktrees could not be inspected")
    roots: list[Path] = []
    for line in completed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree "))
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir():
            roots.append(resolved)
    return tuple(sorted(set(roots), key=str))


def _git_is_tracked(workspace_root: Path, relative: Path) -> bool:
    completed = _git(workspace_root, "ls-files", "--error-unmatch", "--", relative.as_posix())
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise HostIntegrationError("Git could not determine whether Cursor project config is tracked")


def _git_info_exclude(workspace_root: Path) -> Path:
    completed = _git(workspace_root, "rev-parse", "--git-path", "info/exclude")
    if completed.returncode != 0:
        raise HostIntegrationError("Git info/exclude path could not be resolved")
    raw = completed.stdout.strip()
    if not raw:
        raise HostIntegrationError("Git info/exclude path is empty")
    path = Path(raw)
    if not path.is_absolute():
        path = workspace_root / path
    return Path(os.path.abspath(path))


def _exclude_block() -> bytes:
    return ("\n".join((_EXCLUDE_BEGIN, *_EXCLUDE_BODY, _EXCLUDE_END)) + "\n").encode()


def _ensure_cursor_exclude(workspace_root: Path) -> bool:
    path = _git_info_exclude(workspace_root)
    raw = _read_config_bytes(path)
    if raw is None:
        raw = b""
    block = _exclude_block()
    if block in raw:
        return False
    if _EXCLUDE_BEGIN.encode() in raw or _EXCLUDE_END.encode() in raw:
        raise HostIntegrationError("Git info/exclude contains an ambiguous Harness Cursor block")
    prefix = raw
    if prefix and not prefix.endswith(b"\n"):
        prefix += b"\n"
    replacement = prefix + block
    exists = _path_exists(path)
    mode = stat.S_IMODE(path.lstat().st_mode) if exists else 0o644
    _replace_if_unchanged(path, raw if exists else None, replacement, mode)
    return True


def _remove_cursor_exclude(workspace_root: Path) -> None:
    path = _git_info_exclude(workspace_root)
    raw = _read_config_bytes(path)
    if raw is None:
        return
    block = _exclude_block()
    if block not in raw:
        return
    replacement = raw.replace(block, b"", 1)
    _replace_if_unchanged(path, raw, replacement, stat.S_IMODE(path.lstat().st_mode))


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
