from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING

from harness.git_workspace import _git_environment
from harness.hidden_policy import HIDDEN_INSTRUCTION_BODY
from harness.host_adapters import HostIntegrationError, HostRegistrationState, IntegrationChange

if TYPE_CHECKING:
    from harness.codex_adapter import CodexAdapter

CURSOR_PROFILE = "cursor"
CLAUDE_CODE_PROFILE = "claude-code"
CODEX_PROFILE = "codex"
IMPLEMENTED_HIDDEN_PROFILES = (CLAUDE_CODE_PROFILE, CURSOR_PROFILE)
SUPPORTED_HIDDEN_PROFILES = (CLAUDE_CODE_PROFILE, CODEX_PROFILE, CURSOR_PROFILE)
HIDDEN_OWNERSHIP_KIND = "hidden-instruction"
HIDDEN_OWNERSHIP_VERSION = 1
SCM_WRITE_ENFORCEMENT_UNSUPPORTED = "unsupported"
_GIT_TIMEOUT_SECONDS = 1.5
_EXCLUDE_MARKER_PREFIX = b"# harness-owned hidden projection separator="

CURSOR_HIDDEN_RULE_RELATIVE = PurePosixPath(".cursor/rules/harness-hidden.mdc")
CURSOR_HIDDEN_MARKER_RELATIVE = PurePosixPath(".cursor/rules/.harness-hidden.json")
CLAUDE_HIDDEN_RULE_RELATIVE = PurePosixPath(".claude/rules/harness-hidden.md")
CLAUDE_HIDDEN_MARKER_RELATIVE = PurePosixPath(".claude/rules/.harness-hidden.json")


class HiddenProjectionError(RuntimeError):
    """Raised when Hidden instruction projection cannot be applied safely."""


class HiddenProjectionCollisionError(HiddenProjectionError):
    """Raised when Hidden projection would overwrite tracked or user-owned content."""


@dataclass(frozen=True, slots=True)
class HiddenInstructionSurface:
    """One host-native always-on Hidden instruction pair."""

    profile: str
    rule_relative: PurePosixPath
    marker_relative: PurePosixPath
    rule_bytes: bytes


@dataclass(frozen=True, slots=True)
class HiddenProjectionResult:
    """Bounded outcome of applying or removing Hidden instruction files."""

    materialized: int
    removed: int
    unchanged: int
    exclude_changed: bool
    projected_paths: tuple[str, ...]
    scm_write_enforcement: str


@dataclass(frozen=True, slots=True)
class HiddenWorkspaceInspection:
    """Read-only Hidden artifact status for one worktree root."""

    missing_required: tuple[str, ...]
    unignored: tuple[str, ...]
    tracked: tuple[str, ...]
    orphans: tuple[str, ...]


def hidden_instruction_surfaces(profiles: Sequence[str]) -> tuple[HiddenInstructionSurface, ...]:
    """Return the Hidden instruction surfaces for the requested active host profiles."""
    selected: list[HiddenInstructionSurface] = []
    seen: set[str] = set()
    for profile in profiles:
        if profile in seen:
            continue
        seen.add(profile)
        selected.append(_surface_for_profile(profile))
    return tuple(selected)


def apply_hidden_projection(
    workspace_roots: Sequence[Path],
    profiles: Sequence[str],
    *,
    deadline: float | None = None,
) -> HiddenProjectionResult:
    """Materialize Harness-owned Hidden instructions and Git-local excludes."""
    requested_profiles = tuple(dict.fromkeys(profiles))
    unknown = set(requested_profiles) - set(SUPPORTED_HIDDEN_PROFILES)
    if unknown:
        raise HiddenProjectionError(
            "unsupported Hidden host profile: " + ", ".join(sorted(unknown))
        )
    codex_requested = CODEX_PROFILE in requested_profiles
    surfaces = hidden_instruction_surfaces(
        tuple(profile for profile in requested_profiles if profile != CODEX_PROFILE)
    )
    if not surfaces and not codex_requested:
        raise HiddenProjectionError(
            "no active host profiles; Hidden instructions cannot be projected"
        )
    roots = _require_projection_roots(workspace_roots)
    codex = _codex_adapter() if codex_requested else None
    if codex is not None:
        try:
            for root in roots:
                codex.preflight_project_reconcile(root, hidden=True)
        except HostIntegrationError as exc:
            raise HiddenProjectionError(
                f"Codex Hidden developer instructions cannot be projected: {exc}"
            ) from exc
    gitignore_before = _gitignore_bytes_by_root(roots)
    requested = {surface.profile for surface in surfaces}
    desired = set(_surface_paths(surfaces))
    materialized = 0
    unchanged = 0
    removed = 0
    for root in roots:
        _require_deadline(deadline)
        for profile in IMPLEMENTED_HIDDEN_PROFILES:
            if profile in requested:
                continue
            if _remove_owned_surface(root, _surface_for_profile(profile)):
                removed += 1
        for surface in surfaces:
            created = _materialize_surface(root, surface, deadline=deadline)
            if created:
                materialized += 1
            else:
                unchanged += 1
        if codex is not None:
            try:
                change = codex.reconcile_project(root, hidden=True)
            except HostIntegrationError as exc:
                raise HiddenProjectionError(
                    f"Codex Hidden developer instructions could not be reconciled: {exc}"
                ) from exc
            if change is IntegrationChange.CHANGED:
                materialized += 1
            else:
                unchanged += 1
    exclude_changed = _reconcile_hidden_exclude(roots[0], desired, deadline=deadline)
    _verify_hidden_projection(roots, surfaces, gitignore_before, deadline=deadline)
    projected_paths = set(desired)
    if codex_requested:
        projected_paths.add(PurePosixPath(".codex/config.toml"))
    return HiddenProjectionResult(
        materialized=materialized,
        removed=removed,
        unchanged=unchanged,
        exclude_changed=exclude_changed,
        projected_paths=tuple(path.as_posix() for path in sorted(projected_paths, key=str)),
        scm_write_enforcement=SCM_WRITE_ENFORCEMENT_UNSUPPORTED,
    )


def remove_hidden_projection(
    workspace_roots: Sequence[Path],
    profiles: Sequence[str] | None = None,
    *,
    deadline: float | None = None,
) -> HiddenProjectionResult:
    """Remove only Harness-owned Hidden instructions and their exclude entries."""
    requested_profiles = tuple(dict.fromkeys(profiles or ()))
    unknown = set(requested_profiles) - set(SUPPORTED_HIDDEN_PROFILES)
    if unknown:
        raise HiddenProjectionError(
            "unsupported Hidden host profile: " + ", ".join(sorted(unknown))
        )
    codex_requested = profiles is not None and CODEX_PROFILE in requested_profiles
    surfaces = (
        hidden_instruction_surfaces(
            tuple(profile for profile in requested_profiles if profile != CODEX_PROFILE)
        )
        if profiles
        else tuple(_surface_for_profile(profile) for profile in IMPLEMENTED_HIDDEN_PROFILES)
    )
    live_roots = tuple(
        root
        for root in dict.fromkeys(_normalize_root(item) for item in workspace_roots)
        if root.is_dir()
    )
    if not live_roots:
        return HiddenProjectionResult(
            materialized=0,
            removed=0,
            unchanged=0,
            exclude_changed=False,
            projected_paths=(),
            scm_write_enforcement=SCM_WRITE_ENFORCEMENT_UNSUPPORTED,
        )
    gitignore_before = _gitignore_bytes_by_root(live_roots)
    removed = 0
    codex = _codex_adapter() if codex_requested else None
    if codex is not None:
        try:
            for root in live_roots:
                codex.preflight_project_reconcile(root, hidden=False)
        except HostIntegrationError as exc:
            raise HiddenProjectionError(
                f"Codex Hidden developer instructions cannot be removed: {exc}"
            ) from exc
    for root in live_roots:
        _require_deadline(deadline)
        for surface in surfaces:
            if _remove_owned_surface(root, surface):
                removed += 1
        if codex is not None:
            try:
                if codex.reconcile_project(root, hidden=False) is IntegrationChange.CHANGED:
                    removed += 1
            except HostIntegrationError as exc:
                raise HiddenProjectionError(
                    f"Codex Hidden developer instructions could not be removed: {exc}"
                ) from exc
    exclude_changed = _reconcile_hidden_exclude(live_roots[0], set(), deadline=deadline)
    after = _gitignore_bytes_by_root(live_roots)
    if after != gitignore_before:
        raise HiddenProjectionError("Hidden cleanup changed .gitignore")
    return HiddenProjectionResult(
        materialized=0,
        removed=removed,
        unchanged=0,
        exclude_changed=exclude_changed,
        projected_paths=(),
        scm_write_enforcement=SCM_WRITE_ENFORCEMENT_UNSUPPORTED,
    )


def inspect_hidden_workspace(
    workspace_root: Path,
    *,
    required_profiles: Sequence[str],
    expect_hidden: bool,
    deadline: float | None = None,
) -> HiddenWorkspaceInspection:
    """Inspect Hidden instruction files without mutating the worktree."""
    root = _normalize_root(workspace_root)
    missing: list[str] = []
    unignored: list[str] = []
    tracked: list[str] = []
    orphans: list[str] = []
    requested_profiles = tuple(dict.fromkeys(required_profiles))
    unknown = set(requested_profiles) - set(SUPPORTED_HIDDEN_PROFILES)
    if unknown:
        raise HiddenProjectionError(
            "unsupported Hidden host profile: " + ", ".join(sorted(unknown))
        )
    codex_requested = CODEX_PROFILE in requested_profiles
    file_profiles = tuple(profile for profile in requested_profiles if profile != CODEX_PROFILE)
    if expect_hidden:
        for surface in hidden_instruction_surfaces(file_profiles):
            if not _is_owned_surface(root, surface):
                missing.append(surface.rule_relative.as_posix())
                continue
            for relative in (surface.rule_relative, surface.marker_relative):
                posix = relative.as_posix()
                if _git_is_tracked(root, relative, deadline=deadline):
                    tracked.append(posix)
                elif not _git_is_ignored(root, relative, deadline=deadline):
                    unignored.append(posix)
        if codex_requested:
            try:
                diagnostic = _codex_adapter().project_registration_diagnostic(root, hidden=True)
            except HostIntegrationError as exc:
                raise HiddenProjectionError(
                    f"Codex Hidden developer instructions could not be inspected: {exc}"
                ) from exc
            config_relative = PurePosixPath(".codex/config.toml")
            if diagnostic.state is not HostRegistrationState.CURRENT:
                missing.append(config_relative.as_posix())
            elif diagnostic.harness_owned:
                if _git_is_tracked(root, config_relative, deadline=deadline):
                    tracked.append(config_relative.as_posix())
                elif not _git_is_ignored(root, config_relative, deadline=deadline):
                    unignored.append(config_relative.as_posix())
    else:
        for profile in IMPLEMENTED_HIDDEN_PROFILES:
            surface = _surface_for_profile(profile)
            if _is_owned_surface(root, surface):
                orphans.append(surface.rule_relative.as_posix())
        if codex_requested:
            try:
                from harness.codex_adapter import codex_owned_hidden_instructions_active

                if codex_owned_hidden_instructions_active(root):
                    orphans.append(".codex/config.toml")
            except HostIntegrationError as exc:
                raise HiddenProjectionError(
                    f"Codex Hidden developer instructions could not be inspected: {exc}"
                ) from exc
    return HiddenWorkspaceInspection(
        missing_required=tuple(missing),
        unignored=tuple(unignored),
        tracked=tuple(tracked),
        orphans=tuple(orphans),
    )


def hidden_worktree_roots(
    workspace_root: Path, *, deadline: float | None = None
) -> tuple[Path, ...]:
    """Return live Git worktree roots that share this Workspace's common directory."""
    root = _normalize_root(workspace_root)
    roots = {root}
    result = _run_git(root, ["worktree", "list", "--porcelain"], deadline=deadline)
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree "))
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir():
            roots.add(resolved)
    return tuple(sorted(roots, key=str))


def _surface_for_profile(profile: str) -> HiddenInstructionSurface:
    if profile == CURSOR_PROFILE:
        return HiddenInstructionSurface(
            profile=CURSOR_PROFILE,
            rule_relative=CURSOR_HIDDEN_RULE_RELATIVE,
            marker_relative=CURSOR_HIDDEN_MARKER_RELATIVE,
            rule_bytes=("---\nalwaysApply: true\n---\n\n" + HIDDEN_INSTRUCTION_BODY).encode(
                "utf-8"
            ),
        )
    if profile == CLAUDE_CODE_PROFILE:
        return HiddenInstructionSurface(
            profile=CLAUDE_CODE_PROFILE,
            rule_relative=CLAUDE_HIDDEN_RULE_RELATIVE,
            marker_relative=CLAUDE_HIDDEN_MARKER_RELATIVE,
            rule_bytes=HIDDEN_INSTRUCTION_BODY.encode("utf-8"),
        )
    raise HiddenProjectionError(f"unsupported Hidden host profile: {profile}")


def _codex_adapter() -> CodexAdapter:
    from harness.codex_adapter import CodexAdapter

    return CodexAdapter(
        executable=Path("codex"),
        python_executable=Path(os.path.abspath(sys.executable)),
    )


def _surface_paths(surfaces: Sequence[HiddenInstructionSurface]) -> tuple[PurePosixPath, ...]:
    paths: list[PurePosixPath] = []
    for surface in surfaces:
        paths.append(surface.rule_relative)
        paths.append(surface.marker_relative)
    return tuple(paths)


def _require_projection_roots(workspace_roots: Sequence[Path]) -> tuple[Path, ...]:
    roots = tuple(
        dict.fromkeys(_normalize_root(root) for root in workspace_roots if Path(root).is_dir())
    )
    if not roots:
        raise HiddenProjectionError("Hidden projection has no live Git worktree root")
    return roots


def _normalize_root(workspace_root: Path) -> Path:
    try:
        resolved = workspace_root.resolve(strict=True)
        root_stat = workspace_root.lstat()
    except (OSError, RuntimeError) as exc:
        raise HiddenProjectionError(
            f"Hidden projection workspace root cannot be resolved: {workspace_root}"
        ) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not resolved.is_dir():
        raise HiddenProjectionError(
            f"Hidden projection workspace root must be a real directory: {workspace_root}"
        )
    return resolved


def _materialize_surface(
    workspace_root: Path,
    surface: HiddenInstructionSurface,
    *,
    deadline: float | None,
) -> bool:
    rule_path = _workspace_path(workspace_root, surface.rule_relative)
    marker_path = _workspace_path(workspace_root, surface.marker_relative)
    _require_parents_safe(workspace_root, surface.rule_relative)
    _require_parents_safe(workspace_root, surface.marker_relative)
    for relative, path in (
        (surface.rule_relative, rule_path),
        (surface.marker_relative, marker_path),
    ):
        if _git_is_tracked(workspace_root, relative, deadline=deadline):
            raise HiddenProjectionCollisionError(
                f"Hidden projection target is Git-tracked: {relative.as_posix()}"
            )
        _require_regular_or_absent(path, relative)
    marker_owned = _marker_is_owned(marker_path, surface.profile)
    if rule_path.exists() or marker_path.exists():
        if not marker_owned:
            raise HiddenProjectionCollisionError(
                f"Hidden projection target is user-owned: {surface.rule_relative.as_posix()}"
            )
        changed = False
        if _read_optional_bytes(marker_path) != _marker_bytes(surface.profile):
            _replace_file(marker_path, _marker_bytes(surface.profile))
            changed = True
        if _read_optional_bytes(rule_path) != surface.rule_bytes:
            _replace_file(rule_path, surface.rule_bytes)
            changed = True
        return changed
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(marker_path, _marker_bytes(surface.profile))
    _replace_file(rule_path, surface.rule_bytes)
    return True


def _remove_owned_surface(workspace_root: Path, surface: HiddenInstructionSurface) -> bool:
    rule_path = _workspace_path(workspace_root, surface.rule_relative)
    marker_path = _workspace_path(workspace_root, surface.marker_relative)
    if not rule_path.exists() and not marker_path.exists():
        return False
    if not _is_owned_surface(workspace_root, surface):
        if rule_path.exists() or marker_path.exists():
            raise HiddenProjectionCollisionError(
                f"Hidden cleanup target is user-owned: {surface.rule_relative.as_posix()}"
            )
        return False
    removed = False
    for path in (rule_path, marker_path):
        if path.exists():
            path.unlink()
            removed = True
    _remove_empty_parents(workspace_root, surface.rule_relative)
    return removed


def _is_owned_surface(workspace_root: Path, surface: HiddenInstructionSurface) -> bool:
    marker_path = _workspace_path(workspace_root, surface.marker_relative)
    rule_path = _workspace_path(workspace_root, surface.rule_relative)
    if not marker_path.is_file() or not rule_path.is_file():
        return False
    try:
        rule_stat = rule_path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(rule_stat.st_mode):
        return False
    return _marker_is_owned(marker_path, surface.profile)


def _marker_is_owned(marker_path: Path, profile: str) -> bool:
    try:
        marker_stat = marker_path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        return False
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("version") == HIDDEN_OWNERSHIP_VERSION
        and payload.get("kind") == HIDDEN_OWNERSHIP_KIND
        and payload.get("profile") == profile
        and set(payload) == {"version", "kind", "profile"}
    )


def _marker_bytes(profile: str) -> bytes:
    return (
        json.dumps(
            {
                "kind": HIDDEN_OWNERSHIP_KIND,
                "profile": profile,
                "version": HIDDEN_OWNERSHIP_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _verify_hidden_projection(
    roots: Sequence[Path],
    surfaces: Sequence[HiddenInstructionSurface],
    gitignore_before: dict[Path, bytes],
    *,
    deadline: float | None,
) -> None:
    after = _gitignore_bytes_by_root(roots)
    if after != gitignore_before:
        raise HiddenProjectionError("Hidden projection changed .gitignore")
    for root in roots:
        for surface in surfaces:
            if not _is_owned_surface(root, surface):
                raise HiddenProjectionError(
                    f"Hidden instruction was not materialized: {surface.rule_relative.as_posix()}"
                )
            for relative in (surface.rule_relative, surface.marker_relative):
                if _git_is_tracked(root, relative, deadline=deadline):
                    raise HiddenProjectionError(
                        f"Hidden projection path is tracked: {relative.as_posix()}"
                    )
                if not _git_is_ignored(root, relative, deadline=deadline):
                    raise HiddenProjectionError(
                        f"Hidden projection path is not Git-ignored: {relative.as_posix()}"
                    )
                status = _run_git(
                    root,
                    ["status", "--porcelain", "--untracked-files=all", "--", relative.as_posix()],
                    deadline=deadline,
                )
                if status.stdout.strip():
                    raise HiddenProjectionError(
                        f"Hidden projection path is visible to Git status: {relative.as_posix()}"
                    )


def _reconcile_hidden_exclude(
    workspace_root: Path,
    desired_paths: set[PurePosixPath] | tuple[PurePosixPath, ...],
    *,
    deadline: float | None,
) -> bool:
    desired = set(desired_paths)
    exclude_path = _git_info_exclude_path(workspace_root, deadline=deadline)
    original = _read_optional_bytes(exclude_path)
    updated = _reconcile_exclude_bytes(original, desired)
    if updated == original:
        return False
    _replace_file_if_unchanged(exclude_path, original, updated)
    return True


def _reconcile_exclude_bytes(original: bytes, desired_paths: set[PurePosixPath]) -> bytes:
    content = original
    existing = _owned_exclude_blocks(content)
    for relative, block in sorted(existing.items(), key=lambda item: str(item[0])):
        if relative not in desired_paths:
            content = content.replace(block, b"", 1)
    existing_paths = set(_owned_exclude_blocks(content))
    for relative in sorted(desired_paths, key=str):
        if relative in existing_paths:
            continue
        leading_newline = bool(content and not content.endswith(b"\n"))
        content += _exclude_block(relative, leading_newline=leading_newline)
        existing_paths.add(relative)
    return content


def _owned_exclude_blocks(content: bytes) -> dict[PurePosixPath, bytes]:
    blocks: dict[PurePosixPath, bytes] = {}
    for leading_newline in (False, True):
        marker_prefix = _EXCLUDE_MARKER_PREFIX + (b"1" if leading_newline else b"0") + b": "
        search_from = 0
        while True:
            index = content.find(marker_prefix, search_from)
            if index < 0:
                break
            line_end = content.find(b"\n", index)
            if line_end < 0:
                break
            encoded = content[index + len(marker_prefix) : line_end]
            try:
                relative = _validate_relative(PurePosixPath(encoded.decode("utf-8")))
            except (UnicodeDecodeError, HiddenProjectionError):
                search_from = line_end + 1
                continue
            block = _exclude_block(relative, leading_newline=leading_newline)
            block_start = index - 1 if leading_newline else index
            if block_start < 0 or content[block_start : block_start + len(block)] != block:
                search_from = line_end + 1
                continue
            blocks[relative] = block
            search_from = block_start + len(block)
    return blocks


def _exclude_block(relative: PurePosixPath, *, leading_newline: bool) -> bytes:
    prefix = b"\n" if leading_newline else b""
    marker = (
        f"# harness-owned hidden projection separator={1 if leading_newline else 0}: "
        f"{relative.as_posix()}\n"
    ).encode()
    return prefix + marker + f"/{relative.as_posix()}\n".encode()


def _git_info_exclude_path(workspace_root: Path, *, deadline: float | None) -> Path:
    result = _run_git(
        workspace_root, ["rev-parse", "--git-path", "info/exclude"], deadline=deadline
    )
    raw = result.stdout.decode("utf-8", errors="strict").strip()
    if not raw or "\x00" in raw:
        raise HiddenProjectionError("Git returned an invalid info/exclude path")
    path = Path(raw)
    if not path.is_absolute():
        path = workspace_root / path
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HiddenProjectionError("Git info/exclude parent cannot be resolved") from exc
    return parent / path.name


def _git_is_tracked(
    workspace_root: Path, relative: PurePosixPath, *, deadline: float | None
) -> bool:
    result = _run_git(
        workspace_root,
        ["ls-files", "--error-unmatch", "--", relative.as_posix()],
        deadline=deadline,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise HiddenProjectionError(
        f"Git could not determine whether Hidden path is tracked: {relative.as_posix()}"
    )


def _git_is_ignored(
    workspace_root: Path, relative: PurePosixPath, *, deadline: float | None
) -> bool:
    result = _run_git(
        workspace_root,
        ["check-ignore", "-q", "--", relative.as_posix()],
        deadline=deadline,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise HiddenProjectionError(f"Git could not check ignore status: {relative.as_posix()}")


def _run_git(
    workspace_root: Path,
    arguments: Sequence[str],
    *,
    deadline: float | None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    timeout = _git_timeout(deadline)
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HiddenProjectionError(
            "Git command for Hidden projection could not be executed"
        ) from exc
    if check and result.returncode != 0:
        raise HiddenProjectionError("Git command for Hidden projection failed")
    return result


def _gitignore_bytes_by_root(roots: Sequence[Path]) -> dict[Path, bytes]:
    payload: dict[Path, bytes] = {}
    for root in roots:
        payload[root] = _read_optional_bytes(root / ".gitignore")
    return payload


def _workspace_path(workspace_root: Path, relative: PurePosixPath) -> Path:
    return workspace_root.joinpath(*_validate_relative(relative).parts)


def _require_parents_safe(workspace_root: Path, relative: PurePosixPath) -> None:
    current = workspace_root
    for part in _validate_relative(relative).parts[:-1]:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HiddenProjectionError(
                f"Hidden projection parent cannot be inspected: {current}"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise HiddenProjectionCollisionError(
                f"Hidden projection parent is not a real directory: {current.relative_to(workspace_root)}"
            )


def _require_regular_or_absent(path: Path, relative: PurePosixPath) -> None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HiddenProjectionError(
            f"Hidden projection target cannot be inspected: {relative.as_posix()}"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise HiddenProjectionCollisionError(
            f"Hidden projection target is not a regular file: {relative.as_posix()}"
        )


def _validate_relative(value: PurePosixPath) -> PurePosixPath:
    if value.is_absolute() or not value.parts:
        raise HiddenProjectionError("Hidden projection path must be relative")
    if any(part in {"", ".", ".."} or "\x00" in part for part in value.parts):
        raise HiddenProjectionError("Hidden projection path is unsafe")
    return value


def _replace_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = _read_optional_bytes(path)
    _replace_file_if_unchanged(path, original, payload)


def _replace_file_if_unchanged(path: Path, original: bytes, updated: bytes) -> None:
    current = _read_optional_bytes(path)
    if current != original:
        raise HiddenProjectionError("Hidden projection file changed during write")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, raw = tempfile.mkstemp(prefix=".harness-hidden-", dir=path.parent)
        temporary = Path(raw)
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_optional_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise HiddenProjectionError(f"file cannot be read safely: {path}") from exc


def _remove_empty_parents(workspace_root: Path, relative: PurePosixPath) -> None:
    current = _workspace_path(workspace_root, relative).parent
    while current != workspace_root and current.is_dir():
        try:
            next(current.iterdir())
        except StopIteration:
            try:
                current_stat = current.lstat()
            except OSError:
                return
            if stat.S_ISLNK(current_stat.st_mode):
                return
            current.rmdir()
            current = current.parent
            continue
        return


def _git_timeout(deadline: float | None) -> float:
    if deadline is None:
        return _GIT_TIMEOUT_SECONDS
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise HiddenProjectionError("Hidden projection inspection deadline exceeded")
    return min(_GIT_TIMEOUT_SECONDS, remaining)


def _require_deadline(deadline: float | None) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise HiddenProjectionError("Hidden projection inspection deadline exceeded")
