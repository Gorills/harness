from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness.skills import SKILL_FILE_NAME, SKILL_METADATA_FILE_NAME

_BUILTIN_MANIFEST_NAME: Final[str] = ".harness-builtin-skills.json"
_BUILTIN_MANIFEST_VERSION: Final[int] = 1
_BUILTIN_FILE_MODE: Final[int] = 0o600
_BUILTIN_DIR_MODE: Final[int] = 0o700


class BuiltinSkillError(RuntimeError):
    """Raised when the Harness-owned quality skill pack cannot be reconciled safely."""


class BuiltinSkillCollisionError(BuiltinSkillError):
    """Raised when a built-in id collides with unknown or user-modified registry content."""


@dataclass(frozen=True, slots=True)
class BuiltinSkill:
    skill_id: str
    description: str
    task_hints: tuple[str, ...]
    body: str
    applies_languages: tuple[str, ...] = ()
    applies_dependencies: tuple[str, ...] = ()
    applies_manifests: tuple[str, ...] = ()

    def files(self) -> dict[str, bytes]:
        frontmatter = f"---\nname: {self.skill_id}\ndescription: {self.description}\n---\n\n"
        metadata = [f"id: {self.skill_id}"]
        applies = (
            ("languages", self.applies_languages),
            ("dependencies", self.applies_dependencies),
            ("manifests", self.applies_manifests),
        )
        if any(values for _, values in applies):
            metadata.append("applies:")
            for field, values in applies:
                if values:
                    metadata.append(f"  {field}:")
                    metadata.extend(f"    - {value}" for value in values)
        metadata.append("task_hints:")
        metadata.extend(f"  - {hint}" for hint in self.task_hints)
        return {
            SKILL_FILE_NAME: (frontmatter + self.body.strip() + "\n").encode(),
            SKILL_METADATA_FILE_NAME: ("\n".join(metadata) + "\n").encode(),
        }


@dataclass(frozen=True, slots=True)
class BuiltinSkillSyncResult:
    installed: int
    updated: int
    unchanged: int
    adopted: int
    skill_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Replacement:
    target: Path
    backup: Path | None


BUILTIN_SKILLS: Final[tuple[BuiltinSkill, ...]] = (
    BuiltinSkill(
        "architecture-decisions",
        "Capture durable architectural decisions without turning routine edits into paperwork.",
        ("architecture", "adr", "auth", "database-migration", "schema-change", "complex-change"),
        """
# Architecture decisions
Use this skill when a change alters a durable boundary, data model, protocol, security model, or operational contract.
- Read existing architecture docs and ADRs before designing.
- Prefer the smallest design that preserves existing invariants.
- Record an ADR only for durable decisions, not routine implementation detail.
- State context, decision, alternatives rejected, consequences, migration/rollback implications, and verification boundary.
- Keep code, docs, and ADR terminology consistent. Do not invent behavior that authoritative evidence does not establish.
""",
    ),
    BuiltinSkill(
        "testing-strategy",
        "Choose focused tests during iteration and repository-required gates before publication.",
        ("test", "testing", "bugfix", "refactor", "auth", "database-migration", "complex-change"),
        """
# Testing strategy
- Reproduce failures or define a falsifiable acceptance check before changing behavior when practical.
- During iteration, run the smallest relevant unit/integration checks that can catch the change.
- Add regression coverage for the actual failure mode and important negative paths.
- Mock network/external systems at explicit boundaries; prefer real local domain/storage behavior where cheap.
- Before publication or merge, run every repository-mandated quality gate for the exact candidate. A targeted green test never substitutes for required CI.
- After repeated failed attempts, stop changing code, restate the evidence and current hypothesis, and inspect the boundary before trying another fix.
- Report only checks that actually ran; distinguish failed, not run, and environment-blocked verification.
""",
        applies_manifests=("pyproject.toml", "package.json", "cargo.toml", "go.mod"),
    ),
    BuiltinSkill(
        "backend-security",
        "Review backend changes against current authentication, validation, secrets, and data-safety boundaries.",
        ("security", "auth", "authentication", "authorization", "api", "session", "secrets"),
        """
# Backend security
- Treat authentication and authorization as separate checks; enforce authorization server-side at the resource boundary.
- Validate untrusted input structurally and use parameterized database access. Encode output for its destination context.
- Prefer HttpOnly and Secure cookies for browser sessions when appropriate; choose SameSite for the actual flow, including OAuth/OIDC redirects.
- Store passwords with a current memory-hard password KDF such as Argon2id, or a deliberately tuned supported alternative.
- Keep secrets out of source, logs, errors, and generated artifacts; environment variables and managed/mounted secret stores are acceptable deployment mechanisms.
- Fail closed on permission ambiguity and avoid exposing production stack traces or sensitive payloads.
""",
        applies_dependencies=("fastapi", "django", "flask", "express", "@nestjs/core"),
    ),
    BuiltinSkill(
        "container-infrastructure",
        "Keep Docker and local infrastructure reproducible, bounded to the current project, and production-conscious.",
        ("docker", "container", "compose", "infrastructure", "dev-infra"),
        """
# Container infrastructure
- Operate only on this project's declared containers, compose files, networks, and volumes.
- Reuse the repository task runner and existing service names before adding another entry point.
- Prefer deterministic builds, multi-stage images where useful, non-root runtime users, explicit health checks, and bounded resources for production paths.
- Preserve required persistent data in named/project-scoped volumes; make destructive reset steps explicit.
- Do not silently inspect, stop, prune, or mutate unrelated host containers or global Docker state.
- Verify the narrow service first, then the repository's required integration/smoke checks.
""",
        applies_manifests=(
            "dockerfile",
            "containerfile",
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
        ),
    ),
    BuiltinSkill(
        "observability",
        "Add useful logs, metrics, traces, alerts, and runbook context without leaking sensitive data.",
        ("observability", "logging", "metrics", "tracing", "incident", "outage"),
        """
# Observability
- Prefer structured events with stable names and correlation/request identifiers.
- Never log secrets, credentials, raw tokens, or unnecessary personal data.
- Add metrics for user-impacting outcomes and saturation/error signals, not every internal variable.
- Trace cross-boundary latency only where it helps diagnose real flows; preserve propagation across service calls.
- Alerts should correspond to actionable symptoms and link to a concise runbook or recovery path.
- During incidents, preserve evidence, form one hypothesis at a time, and verify recovery with user-visible health signals.
""",
        applies_dependencies=(
            "opentelemetry-api",
            "opentelemetry-sdk",
            "prometheus-client",
            "structlog",
            "sentry-sdk",
            "@opentelemetry/api",
            "pino",
            "winston",
        ),
    ),
    BuiltinSkill(
        "scalability-architecture",
        "Scale from measured constraints while avoiding speculative distributed complexity.",
        ("scalability", "high-load", "performance", "throughput", "latency", "capacity"),
        """
# Scalability architecture
- Start from measured workload, latency, throughput, durability, and failure requirements.
- Prefer the simplest architecture that meets the current envelope; do not add unused distributed machinery.
- Introduce caches, queues, replicas, sharding, or async pipelines only with an explicit bottleneck and invalidation/failure semantics.
- Define concurrency, idempotency, backpressure, retry, timeout, and overload behavior at external boundaries.
- Benchmark or load-test the relevant path and record assumptions that materially affect sizing.
""",
    ),
    BuiltinSkill(
        "ci-release",
        "Change CI and release flows without bypassing repository gates, lockfiles, migration safety, or rollback.",
        ("ci", "cd", "release", "deployment", "pipeline", "github-actions"),
        """
# CI and release
- Treat the repository's existing CI contract as authoritative; extend it rather than replacing it with generic conventions.
- Keep dependency lockfiles current and use reproducible tool versions.
- PR checks should cover the affected behavior; required main/release gates remain mandatory even when focused local tests are green.
- Keep deployment credentials in the platform's secret mechanism and minimize permission scope.
- Make database migrations and irreversible operations explicit, ordered, and recoverable where possible.
- Document rollback/forward-fix behavior for release changes and verify the exact candidate being published.
""",
        applies_manifests=(".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml"),
    ),
    BuiltinSkill(
        "public-frontend",
        "Build public web surfaces with semantic markup, accessibility, metadata, and performance appropriate to the product.",
        ("public-frontend", "frontend", "seo", "accessibility", "web-performance"),
        """
# Public frontend
- Start mobile-first and preserve semantic HTML, keyboard access, readable focus states, and meaningful labels.
- Prefer SSR/SSG when discovery, first-load performance, or shareable metadata materially benefits from it; do not impose it on private app surfaces without need.
- Provide correct title/description/social metadata and canonical URLs for public pages where relevant.
- Keep client JavaScript and image/font cost proportional to the interaction value.
- Test the actual responsive and accessibility behavior affected by the change; do not treat visual polish as a substitute for semantics.
""",
        applies_dependencies=("react", "next", "vue", "nuxt", "astro", "svelte", "@sveltejs/kit"),
    ),
    BuiltinSkill(
        "complex-change-planning",
        "Plan multi-boundary changes as bounded deliverable slices without creating a parallel task tracker.",
        ("complex-change", "migration", "multi-module", "legacy-change", "large-refactor"),
        """
# Complex change planning
Use Harness Task state as the source of truth. Do not create a second epic/status system unless the repository already requires one.
- Map affected contracts, callers, callees, persistence, tests, and operational edges before editing.
- Split work into the smallest dependency-ordered slices that each leave the repository coherent.
- Identify blast radius, migration/rollback concerns, and explicit acceptance evidence for risky boundaries.
- Keep one current implementation slice active; checkpoint progress instead of duplicating status in ad-hoc files.
""",
    ),
    BuiltinSkill(
        "spec-audit",
        "Audit specification completeness and contradictions before implementation of risky or cross-boundary changes.",
        ("spec-audit", "complex-change", "architecture", "migration", "security"),
        """
# Specification audit
Before implementation, independently test the requested behavior against existing contracts.
- Identify the authoritative spec/ADR/API/schema and invariants the change must preserve.
- List material ambiguities, contradictions, missing failure behavior, migration concerns, and acceptance criteria.
- Resolve what can be proven from repository evidence; do not invent missing product decisions.
- If a gap can cause incompatible implementations or irreversible damage, stop implementation at that boundary and surface the blocker.
- Keep the audit concise: only findings that can change implementation or verification belong in the result.
""",
    ),
    BuiltinSkill(
        "independent-review",
        "Review a completed slice against its contracts, failure modes, and exact verification evidence before publication.",
        ("review", "independent-review", "complex-change", "security", "release"),
        """
# Independent review
Review the finished change as if you did not implement it.
- Re-read the governing contract and inspect the complete diff plus nearby callers/callees.
- Look for stale-write races, unsafe defaults, ownership/collision mistakes, migration/recovery gaps, disclosure leaks, and tests that only prove the happy path.
- Classify findings by materiality. Fix correctness/safety/contract issues; do not churn code for taste.
- Re-run checks affected by any fix and the repository-required publication gate.
- Report verified evidence separately from assumptions, not-run checks, and real blockers.
""",
    ),
    BuiltinSkill(
        "project-conventions",
        "Capture only non-mechanical project conventions that make the next task cheaper.",
        ("bootstrap", "onboarding", "project-conventions", "setup", "dev-workflow"),
        """
# Project conventions
Do not duplicate facts Harness can derive from manifests or the Structural Index.
Capture durable Knowledge only for conventions future agents would otherwise rediscover: focused test commands, canonical task runner, local integration environment, docs locations, unsafe operations, migration workflow, and release practice.
Verify conventions from repository evidence before recording them. Prefer a few anchored operational facts over a broad generated project summary.
""",
    ),
)


def sync_builtin_skills(registry_root: Path) -> BuiltinSkillSyncResult:
    _prepare_registry(registry_root)
    manifest_path = registry_root / _BUILTIN_MANIFEST_NAME
    owned = _load_manifest(manifest_path)
    desired = {skill.skill_id: _tree_sha256(skill.files()) for skill in BUILTIN_SKILLS}
    replacements = []
    installed = updated = unchanged = adopted = 0
    try:
        for skill in BUILTIN_SKILLS:
            target = registry_root / skill.skill_id
            files = skill.files()
            wanted = desired[skill.skill_id]
            current = _directory_sha256(target) if _path_exists(target) else None
            recorded = owned.get(skill.skill_id)
            if current == wanted:
                unchanged += 1
                adopted += int(recorded != wanted)
                owned[skill.skill_id] = wanted
                continue
            if current is not None and recorded != current:
                raise BuiltinSkillCollisionError(
                    f"built-in skill collides with user-owned or modified content: {skill.skill_id}"
                )
            replacements.append(_materialize_replacement(registry_root, target, files))
            owned[skill.skill_id] = wanted
            if current is None:
                installed += 1
            else:
                updated += 1
        _write_manifest(manifest_path, owned)
    except Exception:
        _rollback_replacements(replacements)
        raise
    _finalize_replacements(replacements)
    return BuiltinSkillSyncResult(
        installed, updated, unchanged, adopted, tuple(s.skill_id for s in BUILTIN_SKILLS)
    )


def _prepare_registry(root: Path) -> None:
    try:
        root.parent.mkdir(parents=True, exist_ok=True, mode=_BUILTIN_DIR_MODE)
        pm = root.parent.lstat()
        if stat.S_ISLNK(pm.st_mode) or not stat.S_ISDIR(pm.st_mode):
            raise BuiltinSkillError("skill registry parent must be a real directory")
        if hasattr(os, "geteuid") and pm.st_uid != os.geteuid():
            raise BuiltinSkillError("skill registry parent must be owned by the current user")
        root.mkdir(exist_ok=True, mode=_BUILTIN_DIR_MODE)
        m = root.lstat()
    except OSError as exc:
        raise BuiltinSkillError("skill registry cannot be prepared") from exc
    if stat.S_ISLNK(m.st_mode) or not stat.S_ISDIR(m.st_mode):
        raise BuiltinSkillError("skill registry must be a real directory")
    if hasattr(os, "geteuid") and m.st_uid != os.geteuid():
        raise BuiltinSkillError("skill registry must be owned by the current user")


def _load_manifest(path: Path) -> dict[str, str]:
    try:
        m = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BuiltinSkillError("built-in skill manifest cannot be inspected") from exc
    if stat.S_ISLNK(m.st_mode) or not stat.S_ISREG(m.st_mode):
        raise BuiltinSkillError("built-in skill manifest must be a real file")
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuiltinSkillError("built-in skill manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "skills"}
        or payload["version"] != 1
        or not isinstance(payload["skills"], dict)
    ):
        raise BuiltinSkillError("built-in skill manifest has unsupported version or shape")
    result = {}
    for skill_id, digest in payload["skills"].items():
        if not isinstance(skill_id, str) or not isinstance(digest, str) or len(digest) != 64:
            raise BuiltinSkillError("built-in skill manifest contains invalid ownership data")
        result[skill_id] = digest
    return result


def _write_manifest(path: Path, owned: dict[str, str]) -> None:
    payload = (
        json.dumps(
            {"version": 1, "skills": dict(sorted(owned.items()))},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    fd = -1
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, _BUILTIN_FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise BuiltinSkillError("built-in skill manifest could not be persisted") from exc


def _materialize_replacement(root: Path, target: Path, files: dict[str, bytes]) -> _Replacement:
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.builtin-stage-", dir=root))
    os.chmod(stage, _BUILTIN_DIR_MODE)
    try:
        for name, payload in files.items():
            p = stage / name
            p.write_bytes(payload)
            os.chmod(p, _BUILTIN_FILE_MODE)
        backup = None
        if _path_exists(target):
            _require_real_skill_directory(target)
            backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.builtin-backup-", dir=root))
            backup.rmdir()
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup is not None and not _path_exists(target):
                os.replace(backup, target)
            raise
        return _Replacement(target, backup)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _rollback_replacements(items: Sequence[_Replacement]) -> None:
    for item in reversed(items):
        try:
            if _path_exists(item.target):
                _remove_path(item.target)
            if item.backup is not None and _path_exists(item.backup):
                os.replace(item.backup, item.target)
        except OSError:
            continue


def _finalize_replacements(items: Sequence[_Replacement]) -> None:
    for item in items:
        if item.backup is not None and _path_exists(item.backup):
            shutil.rmtree(item.backup, ignore_errors=True)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BuiltinSkillError(f"skill registry entry cannot be inspected: {path.name}") from exc
    return True


def _remove_path(path: Path) -> None:
    m = path.lstat()
    shutil.rmtree(path) if stat.S_ISDIR(m.st_mode) and not stat.S_ISLNK(
        m.st_mode
    ) else path.unlink()


def _require_real_skill_directory(path: Path) -> None:
    try:
        m = path.lstat()
    except OSError as exc:
        raise BuiltinSkillError(f"skill directory cannot be inspected: {path.name}") from exc
    if stat.S_ISLNK(m.st_mode) or not stat.S_ISDIR(m.st_mode):
        raise BuiltinSkillCollisionError(f"skill registry entry is unsafe: {path.name}")


def _directory_sha256(path: Path) -> str:
    _require_real_skill_directory(path)
    files = {}
    try:
        children = sorted(path.iterdir(), key=lambda x: x.name)
    except OSError as exc:
        raise BuiltinSkillError(f"skill directory cannot be listed: {path.name}") from exc
    for child in children:
        try:
            m = child.lstat()
        except OSError as exc:
            raise BuiltinSkillError(f"skill entry cannot be inspected: {path.name}") from exc
        if stat.S_ISLNK(m.st_mode) or not stat.S_ISREG(m.st_mode):
            raise BuiltinSkillCollisionError(
                f"built-in skill contains unexpected non-file content: {path.name}"
            )
        try:
            files[child.name] = child.read_bytes()
        except OSError as exc:
            raise BuiltinSkillError(
                f"skill entry cannot be read: {path.name}/{child.name}"
            ) from exc
    return _tree_sha256(files)


def _tree_sha256(files: dict[str, bytes]) -> str:
    d = hashlib.sha256()
    for name, payload in sorted(files.items()):
        e = name.encode()
        d.update(len(e).to_bytes(4, "big"))
        d.update(e)
        d.update(len(payload).to_bytes(8, "big"))
        d.update(payload)
    return d.hexdigest()
