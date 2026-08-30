from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import harness.builtin_skills as builtin_module
from harness.builtin_skills import (
    BUILTIN_SKILLS,
    BuiltinSkill,
    BuiltinSkillCollisionError,
    BuiltinSkillError,
    sync_builtin_skills,
)
from harness.skill_runtime import (
    SkillRuntimeError,
    active_skill_profiles_for_runtime,
    supported_skill_profiles,
    validate_skill_definitions_for_profiles,
)
from harness.skills import DetectedProjectStack, ResolvedSkill, load_skill_registry, resolve_skills


def _ids(items: Sequence[ResolvedSkill]) -> tuple[str, ...]:
    return tuple(item.definition.skill_id for item in items)


def test_builtin_pack_sync_is_idempotent_and_host_compatible(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    first = sync_builtin_skills(registry)
    second = sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    validate_skill_definitions_for_profiles(definitions, tuple(sorted(supported_skill_profiles())))
    assert first.installed == len(BUILTIN_SKILLS)
    assert second.unchanged == len(BUILTIN_SKILLS)
    assert len(definitions) == len(BUILTIN_SKILLS)
    assert all("_" not in d.skill_id for d in definitions)


def test_builtin_pack_uses_task_hints_for_bounded_composition(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    empty = DetectedProjectStack(frozenset(), frozenset(), frozenset())
    assert _ids(resolve_skills(definitions, empty, task_hints=("auth",))) == (
        "architecture-decisions",
        "secure-by-design",
        "testing-strategy",
    )
    assert _ids(resolve_skills(definitions, empty, task_hints=("complex-change",))) == (
        "architecture-decisions",
        "complex-change-planning",
        "independent-review",
        "project-architecture",
        "spec-audit",
        "testing-strategy",
    )
    assert {"mobile-application", "secure-by-design"} <= set(
        _ids(resolve_skills(definitions, empty, task_hints=("expo",)))
    )


def test_builtin_pack_routes_deep_quality_guidance_by_stack_and_intent(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    by_id = {definition.skill_id: definition for definition in definitions}

    assert {
        "container-infrastructure",
        "data-integrity",
        "language-engineering",
        "legacy-preservation",
        "project-architecture",
        "public-frontend",
        "secure-by-design",
        "mobile-application",
        "server-application",
        "godot-development",
        "deployment-operations",
    } <= set(by_id)
    assert by_id["language-engineering"].portable_files == (
        PurePosixPath("SKILL.md"),
        PurePosixPath("references/c-cpp.md"),
        PurePosixPath("references/dotnet.md"),
        PurePosixPath("references/gdscript.md"),
        PurePosixPath("references/go.md"),
        PurePosixPath("references/javascript-typescript.md"),
        PurePosixPath("references/jvm.md"),
        PurePosixPath("references/php.md"),
        PurePosixPath("references/python.md"),
        PurePosixPath("references/ruby.md"),
        PurePosixPath("references/rust.md"),
        PurePosixPath("references/shell.md"),
        PurePosixPath("references/sql.md"),
        PurePosixPath("references/swift.md"),
    )

    frontend = DetectedProjectStack(
        frozenset({"typescript"}),
        frozenset({"next"}),
        frozenset({"package.json"}),
        frozenset({"software-project", "web-frontend"}),
    )
    frontend_ids = set(_ids(resolve_skills(definitions, frontend)))
    assert {"language-engineering", "public-frontend", "testing-strategy"} <= frontend_ids

    static_frontend = DetectedProjectStack(
        frozenset({"css", "html"}),
        frozenset(),
        frozenset(),
        frozenset({"software-project", "web-frontend"}),
    )
    assert "public-frontend" in _ids(resolve_skills(definitions, static_frontend))

    mobile = DetectedProjectStack(
        frozenset({"typescript"}),
        frozenset({"expo", "react", "react-dom", "react-native", "react-native-web"}),
        frozenset({"package.json"}),
        frozenset({"mobile-app", "software-project"}),
    )
    mobile_ids = set(_ids(resolve_skills(definitions, mobile)))
    assert {"language-engineering", "mobile-application", "secure-by-design"} <= mobile_ids
    assert "public-frontend" not in mobile_ids

    stack_specific = DetectedProjectStack(
        frozenset({"gdscript", "shell"}),
        frozenset({"django"}),
        frozenset({"project.godot", "nginx.conf"}),
        frozenset(
            {
                "backend-service",
                "deployment-ops",
                "godot-project",
                "software-project",
            }
        ),
    )
    assert {
        "deployment-operations",
        "godot-development",
        "language-engineering",
        "secure-by-design",
        "server-application",
    } <= set(_ids(resolve_skills(definitions, stack_specific)))

    docker_greenfield = resolve_skills(
        definitions,
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("docker", "new-project"),
    )
    assert {"container-infrastructure", "project-architecture"} <= set(_ids(docker_greenfield))

    legacy = resolve_skills(
        definitions,
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("legacy-change", "bugfix"),
    )
    assert {
        "complex-change-planning",
        "legacy-preservation",
        "testing-strategy",
    } <= set(_ids(legacy))


def test_builtin_pack_focuses_polyglot_workspace_on_current_task(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    stack = DetectedProjectStack(
        frozenset({"python", "sql", "typescript"}),
        frozenset(
            {
                "alembic",
                "expo",
                "fastapi",
                "react-native",
                "sqlalchemy",
            }
        ),
        frozenset({"package.json", "pyproject.toml"}),
        frozenset(
            {
                "backend-service",
                "database-backed",
                "mobile-app",
                "software-project",
            }
        ),
    )

    repository_baseline = set(_ids(resolve_skills(definitions, stack)))
    assert {
        "data-integrity",
        "language-engineering",
        "mobile-application",
        "secure-by-design",
        "server-application",
        "testing-strategy",
    } <= repository_baseline

    apk_task = set(
        _ids(
            resolve_skills(
                definitions,
                stack,
                task_hints=("expo", "android", "apk", "bugfix"),
            )
        )
    )
    assert apk_task == {
        "language-engineering",
        "mobile-application",
        "secure-by-design",
        "testing-strategy",
    }

    api_migration_task = set(
        _ids(
            resolve_skills(
                definitions,
                stack,
                task_hints=("fastapi", "alembic", "database-migration"),
            )
        )
    )
    assert {
        "architecture-decisions",
        "data-integrity",
        "language-engineering",
        "secure-by-design",
        "server-application",
        "testing-strategy",
    } <= api_migration_task
    assert "mobile-application" not in api_migration_task


def test_builtin_descriptions_state_their_activation_boundary() -> None:
    for skill in BUILTIN_SKILLS:
        assert "when" in skill.description.casefold(), skill.skill_id


def test_builtin_reference_files_are_routed_from_their_entrypoint() -> None:
    for skill in BUILTIN_SKILLS:
        files = skill.files()
        entrypoint = files["SKILL.md"].decode()
        for name, _ in skill.references:
            relative = f"references/{name}"
            assert relative in files
            assert f"({relative})" in entrypoint


def test_builtin_pack_detects_user_changes_inside_reference_tree(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    target = registry / "language-engineering" / "references" / "python.md"
    target.write_text(target.read_text() + "\nUser customization.\n")

    with pytest.raises(BuiltinSkillCollisionError):
        sync_builtin_skills(registry)


@pytest.mark.parametrize("name", ("../escape.md", "nested/file.md", "windows\\path.md"))
def test_builtin_pack_rejects_reference_paths_outside_flat_reference_root(name: str) -> None:
    skill = BuiltinSkill("safe-skill", "Safe skill.", (), "# Safe", references=((name, "x"),))

    with pytest.raises(BuiltinSkillError, match="reference name is invalid"):
        skill.files()


def test_isolated_runtime_uses_one_explicit_compatible_skill_profile_set(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "harness.db"
    assert active_skill_profiles_for_runtime(
        database,
        environment={"HARNESS_DEV_ROOT": str(tmp_path)},
    ) == ("codex", "cursor")
    assert active_skill_profiles_for_runtime(
        database,
        environment={
            "HARNESS_DEV_ROOT": str(tmp_path),
            "HARNESS_DEV_SKILL_PROFILES": "claude-code",
        },
    ) == ("claude-code",)
    with pytest.raises(SkillRuntimeError, match="duplicate-free"):
        active_skill_profiles_for_runtime(
            database,
            environment={
                "HARNESS_DEV_ROOT": str(tmp_path),
                "HARNESS_DEV_SKILL_PROFILES": "claude-code,codex,cursor",
            },
        )


def test_builtin_pack_refuses_foreign_collision_without_mutation(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    foreign = registry / "backend-security"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("user skill\n")
    (foreign / "harness.yaml").write_text("id: backend-security\n")
    before = {p.name: p.read_bytes() for p in foreign.iterdir()}
    with pytest.raises(BuiltinSkillCollisionError):
        sync_builtin_skills(registry)
    assert {p.name: p.read_bytes() for p in foreign.iterdir()} == before
    assert not (registry / ".harness-builtin-skills.json").exists()


def test_builtin_pack_refuses_dangling_symlink_collision(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    registry.mkdir()
    (registry / "backend-security").symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(BuiltinSkillCollisionError, match="unsafe"):
        sync_builtin_skills(registry)


def test_builtin_pack_updates_only_manifest_owned_unmodified_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    original = next(s for s in BUILTIN_SKILLS if s.skill_id == "observability")
    changed = replace(
        original, body=original.body + "\n- Prefer bounded cardinality for metric labels.\n"
    )
    monkeypatch.setattr(
        builtin_module,
        "BUILTIN_SKILLS",
        tuple(changed if s.skill_id == changed.skill_id else s for s in BUILTIN_SKILLS),
    )
    result = sync_builtin_skills(registry)
    assert result.updated == 1
    assert "bounded cardinality" in (registry / "observability" / "SKILL.md").read_text()


def test_builtin_pack_refuses_update_after_user_modifies_owned_skill(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    target = registry / "testing-strategy" / "SKILL.md"
    target.write_text(target.read_text() + "\nUser customization.\n")
    with pytest.raises(BuiltinSkillCollisionError):
        sync_builtin_skills(registry)
