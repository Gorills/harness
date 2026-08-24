from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path, PurePosixPath

from harness.git_workspace import _git_environment
from harness.index import IndexedFileKind, IndexedFileRecord, list_indexed_files
from harness.registry import WorkspaceRecord, get_workspace
from harness.tasks import get_relevant_task, get_task, get_task_stack_hints

SKILL_FILE_NAME = "SKILL.md"
SKILL_METADATA_FILE_NAME = "harness.yaml"
SKILL_OWNERSHIP_MARKER_NAME = ".harness-skill.json"
DEFAULT_MAX_VISIBLE_SKILLS = 12
_SKILL_MARKER_VERSION = 1
_GIT_TIMEOUT_SECONDS = 1.5
_MAX_METADATA_BYTES = 64 * 1024
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_LANGUAGE_SUFFIXES: Mapping[str, str] = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".cxx": "cpp",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".mjs": "javascript",
    ".mts": "typescript",
    ".php": "php",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sql": "sql",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
}


class SkillError(RuntimeError):
    """Base class for deterministic Harness skill failures."""


class SkillRegistryError(SkillError):
    """Raised when canonical skill registry content is unsafe or malformed."""


class SkillResolutionError(SkillError):
    """Raised when skill relevance cannot be determined safely."""


class SkillProjectionError(SkillError):
    """Raised when a generated skill projection cannot be reconciled safely."""


class SkillProjectionCollisionError(SkillProjectionError):
    """Raised when projection would overwrite user/tracked content or duplicate visibility."""


@dataclass(frozen=True, slots=True)
class SkillApplicability:
    languages: tuple[str, ...]
    dependencies: tuple[str, ...]
    manifests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    skill_id: str
    source_directory: Path
    portable_files: tuple[PurePosixPath, ...]
    content_sha256: str
    applies: SkillApplicability
    task_hints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DetectedProjectStack:
    languages: frozenset[str]
    dependencies: frozenset[str]
    manifests: frozenset[str]


@dataclass(frozen=True, slots=True)
class SkillResolutionPolicy:
    max_visible_skills: int = DEFAULT_MAX_VISIBLE_SKILLS

    def __post_init__(self) -> None:
        if isinstance(self.max_visible_skills, bool) or not isinstance(
            self.max_visible_skills, int
        ):
            raise SkillResolutionError("skill budget must be an integer")
        if self.max_visible_skills <= 0:
            raise SkillResolutionError("skill budget must be positive")


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    definition: SkillDefinition
    match_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillProjectionSurface:
    profile: str
    target_root: PurePosixPath
    visible_roots: tuple[PurePosixPath, ...]

    def __post_init__(self) -> None:
        if not self.profile or "\x00" in self.profile:
            raise SkillProjectionError("skill projection profile must be non-empty text")
        target = _validate_projection_root(self.target_root)
        visible = tuple(_validate_projection_root(root) for root in self.visible_roots)
        if not visible or len(set(visible)) != len(visible):
            raise SkillProjectionError(
                "skill projection visible roots must be non-empty and unique"
            )
        if target not in visible:
            raise SkillProjectionError(
                "skill projection target root must be visible to its profile"
            )


@dataclass(frozen=True, slots=True)
class SkillProjectionTarget:
    relative_root: PurePosixPath
    skills: tuple[SkillDefinition, ...]


@dataclass(frozen=True, slots=True)
class SkillProjectionPlan:
    workspace_root: Path
    targets: tuple[SkillProjectionTarget, ...]
    managed_roots: tuple[PurePosixPath, ...]


@dataclass(frozen=True, slots=True)
class SkillProjectionResult:
    materialized: int
    removed: int
    unchanged: int
    exclude_changed: bool


@dataclass(slots=True)
class _PreparedProjection:
    target: Path
    replacement: Path | None
    expected_state_sha256: str | None
    committed_state_sha256: str | None
    backup: Path | None = None
    committed: bool = False


def default_skill_registry(*, home: Path | None = None) -> Path:
    """Return the canonical external skill registry path from the approved v1 contract."""
    base = Path.home() if home is None else home
    if not base.is_absolute():
        raise SkillRegistryError("Harness home directory must be absolute")
    return base / ".harness" / "skills"


def load_skill_registry(registry_root: Path) -> tuple[SkillDefinition, ...]:
    """Load a deterministic canonical skill registry without following symlinks."""
    try:
        root_stat = registry_root.lstat()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise SkillRegistryError("skill registry cannot be inspected") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise SkillRegistryError("skill registry must be a real directory")

    definitions: list[SkillDefinition] = []
    seen_ids: set[str] = set()
    try:
        children = sorted(registry_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise SkillRegistryError("skill registry cannot be listed") from exc
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            child_stat = child.lstat()
        except OSError as exc:
            raise SkillRegistryError(f"skill entry cannot be inspected: {child.name}") from exc
        if not stat.S_ISDIR(child_stat.st_mode) or stat.S_ISLNK(child_stat.st_mode):
            raise SkillRegistryError(f"skill registry entry must be a real directory: {child.name}")
        definition = _load_skill_definition(child)
        if definition.skill_id in seen_ids:
            raise SkillRegistryError(f"duplicate skill id: {definition.skill_id}")
        seen_ids.add(definition.skill_id)
        definitions.append(definition)
    return tuple(definitions)


def detect_workspace_stack(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> DetectedProjectStack:
    """Derive a bounded deterministic stack from the current Structural Index and manifests."""
    workspace = get_workspace(connection, workspace_id)
    records = list_indexed_files(connection, workspace_id)
    languages: set[str] = set()
    dependencies: set[str] = set()
    manifests: set[str] = set()

    for record in records:
        path = PurePosixPath(record.relative_path)
        suffix = path.suffix.casefold()
        language = _LANGUAGE_SUFFIXES.get(suffix)
        if language is not None:
            languages.add(language)
        manifests.add(path.as_posix().casefold())
        manifests.add(path.name.casefold())
        if record.kind is not IndexedFileKind.FILE:
            continue
        name = path.name.casefold()
        if name == "package.json":
            dependencies.update(_package_json_dependencies(workspace, record))
        elif name == "pyproject.toml":
            dependencies.update(_pyproject_dependencies(workspace, record))
        elif name.startswith("requirements") and name.endswith(".txt"):
            dependencies.update(_requirements_dependencies(workspace, record))
        elif name == "cargo.toml":
            dependencies.update(_cargo_dependencies(workspace, record))
        elif name == "go.mod":
            dependencies.update(_go_mod_dependencies(workspace, record))

    return DetectedProjectStack(
        languages=frozenset(languages),
        dependencies=frozenset(dependencies),
        manifests=frozenset(manifests),
    )


def resolve_workspace_skills(
    connection: sqlite3.Connection,
    workspace_id: str,
    definitions: Sequence[SkillDefinition],
    *,
    task_id: str | None = None,
    explicit_include: Iterable[str] = (),
    explicit_exclude: Iterable[str] = (),
    policy: SkillResolutionPolicy | None = None,
) -> tuple[ResolvedSkill, ...]:
    """Resolve relevant skills from indexed stack, Task hints, and explicit project policy."""
    stack = detect_workspace_stack(connection, workspace_id)
    if task_id is None:
        task = get_relevant_task(connection, workspace_id)
    else:
        task = get_task(connection, task_id)
        if task.workspace_id != workspace_id:
            raise SkillResolutionError("skill Task does not belong to the selected Workspace")
    task_hints = () if task is None else get_task_stack_hints(connection, task.task_id)
    return resolve_skills(
        definitions,
        stack,
        task_hints=task_hints,
        explicit_include=explicit_include,
        explicit_exclude=explicit_exclude,
        policy=policy,
    )


def resolve_skills(
    definitions: Sequence[SkillDefinition],
    stack: DetectedProjectStack,
    *,
    task_hints: Iterable[str] = (),
    explicit_include: Iterable[str] = (),
    explicit_exclude: Iterable[str] = (),
    policy: SkillResolutionPolicy | None = None,
) -> tuple[ResolvedSkill, ...]:
    """Select a deterministic bounded relevant subset from the canonical registry."""
    effective_policy = SkillResolutionPolicy() if policy is None else policy
    by_id = {definition.skill_id: definition for definition in definitions}
    if len(by_id) != len(definitions):
        raise SkillResolutionError("skill definitions contain duplicate ids")
    hints = {_normalize_match_token(value, "task hint") for value in task_hints}
    include = {_normalize_skill_id(value) for value in explicit_include}
    exclude = {_normalize_skill_id(value) for value in explicit_exclude}
    overlap = include & exclude
    if overlap:
        raise SkillResolutionError(
            f"skill ids are both explicitly included and excluded: {', '.join(sorted(overlap))}"
        )
    unknown = (include | exclude) - set(by_id)
    if unknown:
        raise SkillResolutionError(f"explicit skill ids are unknown: {', '.join(sorted(unknown))}")
    if len(include) > effective_policy.max_visible_skills:
        raise SkillResolutionError("explicit skills exceed the configured model-visible budget")

    matched: list[tuple[tuple[int, int, int, int, int], ResolvedSkill]] = []
    for definition in definitions:
        if definition.skill_id in exclude:
            continue
        task_matches = sorted(set(definition.task_hints) & hints)
        dependency_matches = sorted(set(definition.applies.dependencies) & stack.dependencies)
        manifest_matches = sorted(set(definition.applies.manifests) & stack.manifests)
        language_matches = sorted(set(definition.applies.languages) & stack.languages)
        explicit = definition.skill_id in include
        if not (
            explicit or task_matches or dependency_matches or manifest_matches or language_matches
        ):
            continue
        reasons: list[str] = []
        if explicit:
            reasons.append("explicit")
        reasons.extend(f"task_hint:{value}" for value in task_matches)
        reasons.extend(f"dependency:{value}" for value in dependency_matches)
        reasons.extend(f"manifest:{value}" for value in manifest_matches)
        reasons.extend(f"language:{value}" for value in language_matches)
        priority = (
            1 if explicit else 0,
            len(task_matches),
            len(dependency_matches),
            len(manifest_matches),
            len(language_matches),
        )
        matched.append((priority, ResolvedSkill(definition, tuple(reasons))))

    matched.sort(
        key=lambda item: (
            -item[0][0],
            -item[0][1],
            -item[0][2],
            -item[0][3],
            -item[0][4],
            item[1].definition.skill_id,
        )
    )
    selected = matched[: effective_policy.max_visible_skills]
    return tuple(item[1] for item in selected)


def plan_skill_projection(
    workspace_root: Path,
    resolved_skills: Sequence[ResolvedSkill],
    surfaces: Sequence[SkillProjectionSurface],
) -> SkillProjectionPlan:
    """Choose a minimal duplicate-free set of native roots for all active host profiles."""
    root = _require_real_directory(workspace_root, "Workspace root")
    if not surfaces:
        return SkillProjectionPlan(root, (), ())
    profiles = [surface.profile for surface in surfaces]
    if len(set(profiles)) != len(profiles):
        raise SkillProjectionError("skill projection profiles must be unique")
    candidates = tuple(sorted({surface.target_root for surface in surfaces}, key=str))
    valid: list[tuple[int, tuple[str, ...], tuple[PurePosixPath, ...]]] = []
    for count in range(1, len(candidates) + 1):
        for candidate_set in combinations(candidates, count):
            if not all(
                sum(root_candidate in surface.visible_roots for root_candidate in candidate_set)
                == 1
                for surface in surfaces
            ):
                continue
            native_hits = sum(surface.target_root in candidate_set for surface in surfaces)
            valid.append(
                (
                    -native_hits,
                    tuple(str(candidate) for candidate in candidate_set),
                    candidate_set,
                )
            )
        if valid:
            break
    if not valid:
        raise SkillProjectionCollisionError(
            "active host skill visibility roots cannot produce a duplicate-free projection plan"
        )
    _, _, chosen = min(valid)
    definitions = tuple(resolved.definition for resolved in resolved_skills)
    managed_roots = tuple(
        sorted({visible for surface in surfaces for visible in surface.visible_roots}, key=str)
    )
    return SkillProjectionPlan(
        workspace_root=root,
        targets=tuple(
            SkillProjectionTarget(relative_root=item, skills=definitions) for item in chosen
        ),
        managed_roots=managed_roots,
    )


def apply_skill_projection(plan: SkillProjectionPlan) -> SkillProjectionResult:
    """Reconcile generated skill directories and Git-local ignore entries with rollback on failure."""
    workspace_root = _require_real_directory(plan.workspace_root, "Workspace root")
    target_roots = tuple(_validate_projection_root(target.relative_root) for target in plan.targets)
    if len(set(target_roots)) != len(target_roots):
        raise SkillProjectionError("skill projection plan contains duplicate target roots")
    managed_roots = {_validate_projection_root(root) for root in plan.managed_roots}
    if not set(target_roots) <= managed_roots:
        raise SkillProjectionError("skill projection targets must be included in managed roots")

    desired_by_path: dict[PurePosixPath, SkillDefinition] = {}
    for target in plan.targets:
        root_relative = _validate_projection_root(target.relative_root)
        skill_ids: set[str] = set()
        for definition in target.skills:
            if definition.skill_id in skill_ids:
                raise SkillProjectionError("skill projection target contains duplicate skill ids")
            skill_ids.add(definition.skill_id)
            relative = root_relative / definition.skill_id
            existing = desired_by_path.get(relative)
            if existing is not None and existing != definition:
                raise SkillProjectionError(
                    "skill projection plan maps one path to different skills"
                )
            desired_by_path[relative] = definition

    existing_owned = _owned_projection_paths(workspace_root, managed_roots)
    _preflight_visible_skill_collisions(
        workspace_root,
        managed_roots,
        desired_by_path,
        existing_owned,
    )
    _preflight_projection_paths(workspace_root, desired_by_path, existing_owned)
    expected_states = {
        relative: _projection_state_sha256(_workspace_projection_path(workspace_root, relative))
        for relative in set(desired_by_path) | existing_owned
    }

    unchanged = 0
    prepared: list[_PreparedProjection] = []
    for relative, definition in sorted(desired_by_path.items(), key=lambda item: str(item[0])):
        target_path = _workspace_projection_path(workspace_root, relative)
        if target_path.exists() and _projection_matches(target_path, definition):
            unchanged += 1
            continue
        replacement = _build_projected_skill(target_path.parent, definition)
        prepared.append(
            _PreparedProjection(
                target=target_path,
                replacement=replacement,
                expected_state_sha256=expected_states.get(relative),
                committed_state_sha256=_projection_state_sha256(replacement),
            )
        )

    stale = sorted(existing_owned - set(desired_by_path), key=str)
    for relative in stale:
        prepared.append(
            _PreparedProjection(
                target=_workspace_projection_path(workspace_root, relative),
                replacement=None,
                expected_state_sha256=expected_states.get(relative),
                committed_state_sha256=None,
            )
        )

    desired_paths = set(desired_by_path)
    exclude_path = _git_info_exclude_path(workspace_root)
    original_exclude = _read_optional_bytes(exclude_path)
    updated_exclude = _reconcile_exclude_bytes(
        original_exclude,
        managed_roots=managed_roots,
        desired_paths=desired_paths,
    )
    exclude_changed = updated_exclude != original_exclude

    try:
        _commit_projection_changes(workspace_root, prepared)
        if exclude_changed:
            _replace_file_if_unchanged(exclude_path, original_exclude, updated_exclude)
    except Exception as exc:
        rollback_error = _rollback_projection_changes(workspace_root, prepared)
        _cleanup_prepared_replacements(prepared)
        if rollback_error is not None:
            raise SkillProjectionError(
                "skill projection failed and prior generated state could not be restored"
            ) from rollback_error
        raise exc
    _finalize_projection_changes(prepared)
    return SkillProjectionResult(
        materialized=sum(item.replacement is not None for item in prepared),
        removed=sum(item.replacement is None for item in prepared),
        unchanged=unchanged,
        exclude_changed=exclude_changed,
    )


def _load_skill_definition(directory: Path) -> SkillDefinition:
    metadata_path = directory / SKILL_METADATA_FILE_NAME
    skill_file = directory / SKILL_FILE_NAME
    _require_regular_file(skill_file, f"skill {directory.name} {SKILL_FILE_NAME}")
    _require_regular_file(metadata_path, f"skill {directory.name} {SKILL_METADATA_FILE_NAME}")
    metadata_bytes = _read_bounded_file(metadata_path, _MAX_METADATA_BYTES)
    try:
        metadata_text = metadata_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillRegistryError(f"skill metadata is not UTF-8: {directory.name}") from exc
    skill_id, applies, task_hints = _parse_metadata(metadata_text, directory.name)
    directory_id = _normalize_skill_id(directory.name)
    if skill_id != directory_id:
        raise SkillRegistryError(
            f"skill metadata id {skill_id!r} does not match directory {directory.name!r}"
        )
    portable_files = _portable_skill_files(directory)
    if PurePosixPath(SKILL_FILE_NAME) not in portable_files:
        raise SkillRegistryError(f"skill is missing {SKILL_FILE_NAME}: {skill_id}")
    content_sha256 = _portable_tree_sha256(directory, portable_files)
    return SkillDefinition(
        skill_id=skill_id,
        source_directory=directory,
        portable_files=portable_files,
        content_sha256=content_sha256,
        applies=applies,
        task_hints=task_hints,
    )


def _parse_metadata(
    text: str, directory_name: str
) -> tuple[str, SkillApplicability, tuple[str, ...]]:
    skill_id: str | None = None
    applies: dict[str, list[str]] = {"languages": [], "dependencies": [], "manifests": []}
    task_hints: list[str] = []
    section: str | None = None
    subsection: str | None = None
    seen_top: set[str] = set()
    seen_apply: set[str] = set()
    for line_number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise SkillRegistryError(f"skill metadata uses tabs at {directory_name}:{line_number}")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            subsection = None
            if stripped.startswith("id:"):
                if "id" in seen_top:
                    raise SkillRegistryError(f"duplicate skill metadata id: {directory_name}")
                seen_top.add("id")
                skill_id = _normalize_skill_id(_metadata_scalar(stripped[3:], directory_name))
                section = "id"
            elif stripped == "applies:":
                if "applies" in seen_top:
                    raise SkillRegistryError(f"duplicate applies metadata: {directory_name}")
                seen_top.add("applies")
                section = "applies"
            elif stripped == "task_hints:":
                if "task_hints" in seen_top:
                    raise SkillRegistryError(f"duplicate task_hints metadata: {directory_name}")
                seen_top.add("task_hints")
                section = "task_hints"
            else:
                raise SkillRegistryError(
                    f"unsupported skill metadata field at {directory_name}:{line_number}"
                )
            continue
        if section == "applies" and indent == 2 and stripped.endswith(":"):
            key = stripped[:-1]
            if key not in applies or key in seen_apply:
                raise SkillRegistryError(
                    f"unsupported or duplicate applies field at {directory_name}:{line_number}"
                )
            seen_apply.add(key)
            subsection = key
            continue
        if (
            section == "applies"
            and subsection is not None
            and indent == 4
            and stripped.startswith("- ")
        ):
            value = _metadata_scalar(stripped[2:], directory_name)
            applies[subsection].append(value)
            continue
        if section == "task_hints" and indent == 2 and stripped.startswith("- "):
            task_hints.append(_metadata_scalar(stripped[2:], directory_name))
            continue
        raise SkillRegistryError(
            f"invalid skill metadata structure at {directory_name}:{line_number}"
        )

    if skill_id is None:
        raise SkillRegistryError(f"skill metadata is missing id: {directory_name}")
    languages = _normalize_unique_tokens(applies["languages"], "language")
    dependencies = _normalize_unique_tokens(applies["dependencies"], "dependency")
    manifests = _normalize_unique_manifests(applies["manifests"])
    normalized_hints = _normalize_unique_tokens(task_hints, "task hint")
    return (
        skill_id,
        SkillApplicability(languages, dependencies, manifests),
        normalized_hints,
    )


def _metadata_scalar(value: str, skill_name: str) -> str:
    scalar = value.strip()
    if not scalar:
        raise SkillRegistryError(f"skill metadata scalar is empty: {skill_name}")
    if scalar[0] in "[{&*!|>":
        raise SkillRegistryError(f"complex YAML is not supported in skill metadata: {skill_name}")
    if scalar[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(scalar)
        except (SyntaxError, ValueError) as exc:
            raise SkillRegistryError(f"invalid quoted skill metadata scalar: {skill_name}") from exc
        if not isinstance(parsed, str):
            raise SkillRegistryError(f"skill metadata scalar must be text: {skill_name}")
        scalar = parsed
    if "\x00" in scalar or any(ord(character) < 0x20 for character in scalar):
        raise SkillRegistryError(f"skill metadata scalar contains control characters: {skill_name}")
    return scalar


def _portable_skill_files(directory: Path) -> tuple[PurePosixPath, ...]:
    files: list[PurePosixPath] = []
    for root, directories, filenames in os.walk(directory, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in list(directories):
            child = root_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                raise SkillRegistryError(f"skill contains unsafe directory entry: {child}")
        for name in filenames:
            child = root_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISREG(child_stat.st_mode):
                raise SkillRegistryError(f"skill contains unsafe file entry: {child}")
            relative = PurePosixPath(child.relative_to(directory).as_posix())
            if relative == PurePosixPath(SKILL_METADATA_FILE_NAME):
                continue
            if relative.name == SKILL_OWNERSHIP_MARKER_NAME:
                raise SkillRegistryError(
                    f"skill registry content uses reserved ownership marker: {directory.name}"
                )
            files.append(relative)
    return tuple(sorted(files, key=str))


def _portable_tree_sha256(directory: Path, files: Sequence[PurePosixPath]) -> str:
    digest = hashlib.sha256()
    for relative in files:
        payload = (directory / Path(*relative.parts)).read_bytes()
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _package_json_dependencies(workspace: WorkspaceRecord, record: IndexedFileRecord) -> set[str]:
    payload = _read_indexed_manifest(workspace, record)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillResolutionError(
            f"indexed package.json is malformed: {record.relative_path}"
        ) from exc
    if not isinstance(value, dict):
        raise SkillResolutionError(f"indexed package.json is not an object: {record.relative_path}")
    dependencies: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = value.get(key, {})
        if section is None:
            continue
        if not isinstance(section, dict):
            raise SkillResolutionError(
                f"package.json {key} must be an object: {record.relative_path}"
            )
        for name in section:
            if not isinstance(name, str):
                raise SkillResolutionError(
                    f"package.json dependency name must be text: {record.relative_path}"
                )
            dependencies.add(_normalize_dependency(name))
    return dependencies


def _pyproject_dependencies(workspace: WorkspaceRecord, record: IndexedFileRecord) -> set[str]:
    payload = _read_indexed_manifest(workspace, record)
    try:
        value = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SkillResolutionError(
            f"indexed pyproject.toml is malformed: {record.relative_path}"
        ) from exc
    dependencies: set[str] = set()
    project = value.get("project", {})
    if isinstance(project, dict):
        for item in project.get("dependencies", []) or []:
            if not isinstance(item, str):
                raise SkillResolutionError("pyproject project.dependencies entries must be text")
            name = _requirement_name(item)
            if name is not None:
                dependencies.add(name)
        optional = project.get("optional-dependencies", {}) or {}
        if isinstance(optional, dict):
            for items in optional.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, str):
                        name = _requirement_name(item)
                        if name is not None:
                            dependencies.add(name)
    groups = value.get("dependency-groups", {}) or {}
    if isinstance(groups, dict):
        for items in groups.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, str):
                    name = _requirement_name(item)
                    if name is not None:
                        dependencies.add(name)
    tool = value.get("tool", {})
    if isinstance(tool, dict):
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            section = poetry.get("dependencies", {})
            if isinstance(section, dict):
                dependencies.update(
                    _normalize_dependency(str(name)) for name in section if name != "python"
                )
    return dependencies


def _requirements_dependencies(workspace: WorkspaceRecord, record: IndexedFileRecord) -> set[str]:
    payload = _read_indexed_manifest(workspace, record)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillResolutionError(
            f"indexed requirements file is not UTF-8: {record.relative_path}"
        ) from exc
    dependencies: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if (
            not line
            or line.startswith("#")
            or line.startswith(("-r", "--requirement", "-c", "--constraint"))
        ):
            continue
        name = _requirement_name(line)
        if name is not None:
            dependencies.add(name)
    return dependencies


def _cargo_dependencies(workspace: WorkspaceRecord, record: IndexedFileRecord) -> set[str]:
    payload = _read_indexed_manifest(workspace, record)
    try:
        value = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SkillResolutionError(
            f"indexed Cargo.toml is malformed: {record.relative_path}"
        ) from exc
    dependencies: set[str] = set()
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        section = value.get(key, {})
        if isinstance(section, dict):
            dependencies.update(_normalize_dependency(str(name)) for name in section)
    return dependencies


def _go_mod_dependencies(workspace: WorkspaceRecord, record: IndexedFileRecord) -> set[str]:
    payload = _read_indexed_manifest(workspace, record)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillResolutionError(f"indexed go.mod is not UTF-8: {record.relative_path}") from exc
    dependencies: set[str] = set()
    in_require = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line == "require (":
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        candidate = (
            line.removeprefix("require ").strip()
            if line.startswith("require ")
            else line
            if in_require
            else ""
        )
        if candidate:
            name = candidate.split()[0]
            dependencies.add(_normalize_dependency(name))
    return dependencies


def _read_indexed_manifest(workspace: WorkspaceRecord, record: IndexedFileRecord) -> bytes:
    path = workspace.workspace_root / Path(*PurePosixPath(record.relative_path).parts)
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise SkillResolutionError(
            f"indexed manifest cannot be inspected: {record.relative_path}"
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise SkillResolutionError(
            f"indexed manifest is no longer a regular file: {record.relative_path}"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SkillResolutionError(
            f"indexed manifest cannot be read: {record.relative_path}"
        ) from exc
    if (
        len(payload) != record.size_bytes
        or hashlib.sha256(payload).hexdigest() != record.content_sha256
    ):
        raise SkillResolutionError(
            f"Structural Index is stale for manifest: {record.relative_path}"
        )
    return payload


def _owned_projection_paths(
    workspace_root: Path,
    managed_roots: set[PurePosixPath],
) -> set[PurePosixPath]:
    owned: set[PurePosixPath] = set()
    for relative_root in managed_roots:
        root = _workspace_projection_path(workspace_root, relative_root)
        if not root.exists():
            continue
        root_stat = root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise SkillProjectionCollisionError(
                f"skill projection root is not a real directory: {relative_root}"
            )
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or child.is_symlink():
                continue
            marker = _read_projection_marker(child)
            if marker is not None:
                owned.add(relative_root / child.name)
    return owned


def _preflight_visible_skill_collisions(
    workspace_root: Path,
    managed_roots: set[PurePosixPath],
    desired: Mapping[PurePosixPath, SkillDefinition],
    existing_owned: set[PurePosixPath],
) -> None:
    desired_paths = set(desired)
    desired_ids = {definition.skill_id for definition in desired.values()}
    for relative_root in managed_roots:
        _require_projection_parents_safe(workspace_root, relative_root)
        for skill_id in desired_ids:
            relative = relative_root / skill_id
            if relative in desired_paths or relative in existing_owned:
                continue
            target = _workspace_projection_path(workspace_root, relative)
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SkillProjectionError(
                    f"skill visibility target cannot be inspected: {relative}"
                ) from exc
            raise SkillProjectionCollisionError(
                f"user-owned skill would duplicate Harness projection visibility: {relative}"
            )


def _preflight_projection_paths(
    workspace_root: Path,
    desired: Mapping[PurePosixPath, SkillDefinition],
    existing_owned: set[PurePosixPath],
) -> None:
    for relative, definition in desired.items():
        target = _workspace_projection_path(workspace_root, relative)
        _require_projection_parents_safe(workspace_root, relative.parent)
        tracked = _git_tracked_paths(workspace_root, relative)
        if tracked:
            raise SkillProjectionCollisionError(
                f"skill projection target is tracked by Git: {relative}"
            )
        if not target.exists():
            continue
        marker = _read_projection_marker(target)
        if marker is None or marker.get("skill_id") != definition.skill_id:
            raise SkillProjectionCollisionError(
                f"skill projection target is user-owned or has invalid ownership: {relative}"
            )
    for relative in existing_owned - set(desired):
        if _git_tracked_paths(workspace_root, relative):
            raise SkillProjectionCollisionError(
                f"stale Harness skill projection became tracked by Git: {relative}"
            )


def _projection_matches(target: Path, definition: SkillDefinition) -> bool:
    marker = _read_projection_marker(target)
    if marker is None:
        return False
    if marker != {
        "version": _SKILL_MARKER_VERSION,
        "skill_id": definition.skill_id,
        "content_sha256": definition.content_sha256,
    }:
        return False
    files = _projected_files(target)
    return _portable_tree_sha256(target, files) == definition.content_sha256


def _build_projected_skill(parent: Path, definition: SkillDefinition) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".harness-{definition.skill_id}-", dir=parent))
    try:
        for relative in definition.portable_files:
            source = definition.source_directory / Path(*relative.parts)
            destination = temporary / Path(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
        marker = {
            "version": _SKILL_MARKER_VERSION,
            "skill_id": definition.skill_id,
            "content_sha256": definition.content_sha256,
        }
        (temporary / SKILL_OWNERSHIP_MARKER_NAME).write_text(
            json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return temporary
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _commit_projection_changes(
    workspace_root: Path,
    prepared: Sequence[_PreparedProjection],
) -> None:
    for item in prepared:
        relative_parent = PurePosixPath(item.target.parent.relative_to(workspace_root).as_posix())
        _require_projection_parents_safe(workspace_root, relative_parent)
        if item.expected_state_sha256 is not None:
            marker = _read_projection_marker(item.target)
            if marker is None or marker.get("skill_id") != item.target.name:
                raise SkillProjectionCollisionError(
                    "Harness skill projection changed ownership before mutation: "
                    f"{item.target.relative_to(workspace_root)}"
                )
        current_state = _projection_state_sha256(item.target)
        if current_state != item.expected_state_sha256:
            raise SkillProjectionCollisionError(
                f"skill projection target changed before mutation: {item.target.relative_to(workspace_root)}"
            )
        item.target.parent.mkdir(parents=True, exist_ok=True)
        if item.target.exists():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".harness-backup-{item.target.name}-", dir=item.target.parent
                )
            )
            backup.rmdir()
            os.replace(item.target, backup)
            item.backup = backup
            try:
                moved_marker = _read_projection_marker(backup)
                moved_state = _projection_state_sha256(backup)
            except Exception:
                _restore_uncommitted_projection_backup(workspace_root, item)
                raise
            if moved_state != item.expected_state_sha256 or (
                item.expected_state_sha256 is not None
                and (moved_marker is None or moved_marker.get("skill_id") != item.target.name)
            ):
                _restore_uncommitted_projection_backup(workspace_root, item)
                raise SkillProjectionCollisionError(
                    "skill projection target changed during mutation: "
                    f"{item.target.relative_to(workspace_root)}"
                )
        if item.replacement is not None:
            try:
                os.replace(item.replacement, item.target)
            except Exception:
                _restore_uncommitted_projection_backup(workspace_root, item)
                raise
        item.committed = True


def _restore_uncommitted_projection_backup(
    workspace_root: Path,
    item: _PreparedProjection,
) -> None:
    backup = item.backup
    if backup is None:
        return
    if item.target.exists():
        raise SkillProjectionError(
            "skill projection target changed during mutation and moved content could not be "
            f"restored: {item.target.relative_to(workspace_root)}"
        )
    try:
        os.replace(backup, item.target)
    except OSError as exc:
        raise SkillProjectionError(
            "skill projection target could not be restored after mutation validation failed: "
            f"{item.target.relative_to(workspace_root)}"
        ) from exc
    item.backup = None


def _rollback_projection_changes(
    workspace_root: Path,
    prepared: Sequence[_PreparedProjection],
) -> Exception | None:
    first_error: Exception | None = None
    for item in reversed(prepared):
        if not item.committed:
            continue
        try:
            if item.replacement is not None:
                _stage_committed_projection_for_rollback(workspace_root, item)
            else:
                current_state = _projection_state_sha256(item.target)
                if current_state != item.committed_state_sha256:
                    raise SkillProjectionCollisionError(
                        f"skill projection target changed before rollback: {item.target}"
                    )
            if item.backup is not None:
                _restore_uncommitted_projection_backup(workspace_root, item)
        except (OSError, SkillProjectionError) as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _stage_committed_projection_for_rollback(
    workspace_root: Path,
    item: _PreparedProjection,
) -> None:
    staging = item.replacement
    if staging is None or item.committed_state_sha256 is None:
        raise SkillProjectionError("materialized skill projection is missing rollback state")
    if _projection_entry_exists(staging):
        raise SkillProjectionCollisionError(
            f"skill projection rollback staging path exists: {staging}"
        )
    try:
        os.replace(item.target, staging)
    except OSError as exc:
        raise SkillProjectionError(
            f"skill projection target could not be staged for rollback: {item.target}"
        ) from exc
    try:
        moved_state = _projection_state_sha256(staging)
    except Exception:
        _restore_rollback_candidate(workspace_root, item)
        raise
    if moved_state != item.committed_state_sha256:
        _restore_rollback_candidate(workspace_root, item)
        raise SkillProjectionCollisionError(
            f"skill projection target changed during rollback mutation: {item.target}"
        )


def _restore_rollback_candidate(
    workspace_root: Path,
    item: _PreparedProjection,
) -> None:
    staging = item.replacement
    if staging is None:
        return
    if _projection_entry_exists(item.target):
        item.replacement = None
        raise SkillProjectionError(
            "skill projection rollback moved content could not be restored; preserved at "
            f"{staging.relative_to(workspace_root)}"
        )
    try:
        os.replace(staging, item.target)
    except OSError as exc:
        item.replacement = None
        raise SkillProjectionError(
            "skill projection rollback moved content could not be restored; preserved at "
            f"{staging.relative_to(workspace_root)}"
        ) from exc


def _projection_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SkillProjectionError(f"skill projection path cannot be inspected: {path}") from exc
    return True


def _finalize_projection_changes(prepared: Sequence[_PreparedProjection]) -> None:
    for item in prepared:
        if item.backup is not None and item.backup.exists():
            shutil.rmtree(item.backup)
        if item.replacement is not None and item.replacement.exists():
            shutil.rmtree(item.replacement)


def _cleanup_prepared_replacements(prepared: Sequence[_PreparedProjection]) -> None:
    for item in prepared:
        if item.replacement is not None and item.replacement.exists():
            shutil.rmtree(item.replacement, ignore_errors=True)


def _read_projection_marker(target: Path) -> dict[str, object] | None:
    marker_path = target / SKILL_OWNERSHIP_MARKER_NAME
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillProjectionError(f"skill projection cannot be inspected: {target}") from exc
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        return None
    try:
        marker_stat = marker_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillProjectionError(f"skill ownership marker cannot be inspected: {target}") from exc
    if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
        return None
    try:
        value = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"version", "skill_id", "content_sha256"}:
        return None
    if value.get("version") != _SKILL_MARKER_VERSION:
        return None
    skill_id = value.get("skill_id")
    content_sha256 = value.get("content_sha256")
    if not isinstance(skill_id, str) or not _SKILL_ID_RE.fullmatch(skill_id):
        return None
    if (
        not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
    ):
        return None
    return value


def _projection_state_sha256(target: Path) -> str | None:
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillProjectionError(f"skill projection state cannot be inspected: {target}") from exc
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        raise SkillProjectionCollisionError(
            f"skill projection target is not a real directory: {target}"
        )
    digest = hashlib.sha256()
    for root, directories, filenames in os.walk(target, topdown=True, followlinks=False):
        directories.sort()
        root_path = Path(root)
        for name in directories:
            child = root_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                raise SkillProjectionCollisionError(
                    f"skill projection target contains unsafe directory: {child}"
                )
        for name in sorted(filenames):
            child = root_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISREG(child_stat.st_mode):
                raise SkillProjectionCollisionError(
                    f"skill projection target contains unsafe file: {child}"
                )
            relative = child.relative_to(target).as_posix().encode("utf-8")
            payload = child.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def _projected_files(target: Path) -> tuple[PurePosixPath, ...]:
    files: list[PurePosixPath] = []
    for root, directories, filenames in os.walk(target, topdown=True, followlinks=False):
        directories.sort()
        root_path = Path(root)
        for name in directories:
            child = root_path / name
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                raise SkillProjectionError(f"projected skill contains unsafe directory: {child}")
        for name in filenames:
            child = root_path / name
            if child.name == SKILL_OWNERSHIP_MARKER_NAME:
                continue
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISREG(child_stat.st_mode):
                raise SkillProjectionError(f"projected skill contains unsafe file: {child}")
            files.append(PurePosixPath(child.relative_to(target).as_posix()))
    return tuple(sorted(files, key=str))


def _git_info_exclude_path(workspace_root: Path) -> Path:
    result = _run_git(workspace_root, ["rev-parse", "--git-path", "info/exclude"])
    raw = result.stdout.decode("utf-8", errors="strict").strip()
    if not raw or "\x00" in raw:
        raise SkillProjectionError("Git returned an invalid info/exclude path")
    path = Path(raw)
    if not path.is_absolute():
        path = workspace_root / path
    try:
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SkillProjectionError("Git info/exclude parent cannot be resolved") from exc
    return parent / path.name


def _git_tracked_paths(workspace_root: Path, relative: PurePosixPath) -> tuple[str, ...]:
    result = _run_git(
        workspace_root,
        ["ls-files", "-z", "--", relative.as_posix(), f"{relative.as_posix()}/"],
    )
    return tuple(os.fsdecode(item) for item in result.stdout.split(b"\x00") if item)


def _run_git(workspace_root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillProjectionError(
            "Git command for skill projection could not be executed"
        ) from exc
    if result.returncode != 0:
        raise SkillProjectionError("Git command for skill projection failed")
    return result


def _reconcile_exclude_bytes(
    original: bytes,
    *,
    managed_roots: set[PurePosixPath],
    desired_paths: set[PurePosixPath],
) -> bytes:
    content = original
    existing = _owned_exclude_blocks(content)
    for relative, block in sorted(existing.items(), key=lambda item: str(item[0])):
        if (
            any(_is_under_root(relative, root) for root in managed_roots)
            and relative not in desired_paths
        ):
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
        marker_prefix = (
            b"# harness-owned skill projection separator="
            + (b"1" if leading_newline else b"0")
            + b": "
        )
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
                relative = _validate_projection_path(PurePosixPath(encoded.decode("utf-8")))
            except (UnicodeDecodeError, SkillProjectionError):
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
        f"# harness-owned skill projection separator={1 if leading_newline else 0}: "
        f"{relative.as_posix()}\n"
    ).encode()
    return prefix + marker + _exclude_pattern(relative)


def _exclude_pattern(relative: PurePosixPath) -> bytes:
    return f"/{relative.as_posix()}/\n".encode()


def _replace_file_if_unchanged(path: Path, original: bytes, updated: bytes) -> None:
    current = _read_optional_bytes(path)
    if current != original:
        raise SkillProjectionError("Git info/exclude changed during skill projection")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, raw = tempfile.mkstemp(prefix=".harness-exclude-", dir=path.parent)
        temporary = Path(raw)
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_optional_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise SkillProjectionError(f"file cannot be read safely: {path}") from exc


def _workspace_projection_path(workspace_root: Path, relative: PurePosixPath) -> Path:
    validated = _validate_projection_path(relative)
    return workspace_root.joinpath(*validated.parts)


def _require_projection_parents_safe(workspace_root: Path, relative_root: PurePosixPath) -> None:
    current = workspace_root
    for part in _validate_projection_root(relative_root).parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SkillProjectionError(
                f"skill projection parent cannot be inspected: {current}"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
            raise SkillProjectionCollisionError(
                f"skill projection parent is not a real directory: {current.relative_to(workspace_root)}"
            )


def _validate_projection_root(value: PurePosixPath) -> PurePosixPath:
    validated = _validate_projection_path(value)
    if len(validated.parts) < 2:
        raise SkillProjectionError("skill projection root must be a nested project path")
    return validated


def _validate_projection_path(value: PurePosixPath) -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise SkillProjectionError("skill projection path must be a PurePosixPath")
    if value.is_absolute() or not value.parts:
        raise SkillProjectionError("skill projection path must be relative")
    if any(part in {"", ".", ".."} or "\x00" in part for part in value.parts):
        raise SkillProjectionError("skill projection path is unsafe")
    return value


def _is_under_root(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _require_real_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        path_stat = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise SkillProjectionError(f"{label} cannot be resolved") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not resolved.is_dir():
        raise SkillProjectionError(f"{label} must be a real directory")
    return resolved


def _require_regular_file(path: Path, label: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise SkillRegistryError(f"{label} cannot be inspected") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise SkillRegistryError(f"{label} must be a regular file")


def _read_bounded_file(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(limit + 1)
    except OSError as exc:
        raise SkillRegistryError(f"skill metadata cannot be read: {path.parent.name}") from exc
    if len(payload) > limit:
        raise SkillRegistryError(f"skill metadata exceeds {limit} bytes: {path.parent.name}")
    return payload


def _normalize_skill_id(value: str) -> str:
    normalized = _normalize_match_token(value, "skill id")
    if not _SKILL_ID_RE.fullmatch(normalized):
        raise SkillRegistryError(f"invalid skill id: {value!r}")
    return normalized


def _normalize_match_token(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise SkillResolutionError(f"{label} must be text")
    normalized = value.strip().casefold()
    if not normalized or "\x00" in normalized:
        raise SkillResolutionError(f"{label} must be non-empty text")
    return normalized


def _normalize_dependency(value: str) -> str:
    normalized = _normalize_match_token(value, "dependency")
    if normalized.startswith("@"):
        return normalized
    return re.sub(r"[-_.]+", "-", normalized)


def _normalize_unique_tokens(values: Sequence[str], label: str) -> tuple[str, ...]:
    normalized = tuple(_normalize_match_token(value, label) for value in values)
    if len(set(normalized)) != len(normalized):
        raise SkillRegistryError(f"skill metadata contains duplicate {label} values")
    if label == "dependency":
        normalized = tuple(_normalize_dependency(value) for value in normalized)
        if len(set(normalized)) != len(normalized):
            raise SkillRegistryError("skill metadata contains normalized duplicate dependencies")
    return tuple(sorted(normalized))


def _normalize_unique_manifests(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        token = _normalize_match_token(value, "manifest").replace("\\", "/")
        path = PurePosixPath(token)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SkillRegistryError(f"skill metadata manifest path is unsafe: {value!r}")
        normalized.append(path.as_posix())
    if len(set(normalized)) != len(normalized):
        raise SkillRegistryError("skill metadata contains duplicate manifests")
    return tuple(sorted(normalized))


def _requirement_name(value: str) -> str | None:
    match = _REQUIREMENT_NAME_RE.match(value)
    return None if match is None else _normalize_dependency(match.group(1))
