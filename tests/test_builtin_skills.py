from __future__ import annotations

import json
import os
import shutil
import stat
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
from harness.skills import (
    DetectedProjectStack,
    ResolvedSkill,
    load_skill_registry,
    resolve_skills,
)


def _ids(items: Sequence[ResolvedSkill]) -> tuple[str, ...]:
    return tuple(item.definition.skill_id for item in items)


def _builtin_by_id(skill_id: str) -> BuiltinSkill:
    return next(skill for skill in BUILTIN_SKILLS if skill.skill_id == skill_id)


def _materialize_builtin(registry: Path, skill: BuiltinSkill) -> None:
    registry.mkdir(parents=True, exist_ok=True)
    registry.chmod(0o700)
    directory = registry / skill.skill_id
    directory.mkdir()
    for relative, payload in skill.files().items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


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
    for skill in BUILTIN_SKILLS:
        text = (registry / skill.skill_id / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {json.dumps(skill.skill_id, ensure_ascii=False)}" in text
        assert f"description: {json.dumps(skill.description, ensure_ascii=False)}" in text


def test_builtin_pack_resolves_from_project_stack_without_task_hints(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    empty = DetectedProjectStack(frozenset(), frozenset(), frozenset())
    assert _ids(resolve_skills(definitions, empty)) == ()
    software = DetectedProjectStack(
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset({"software-project"}),
    )
    software_ids = set(_ids(resolve_skills(definitions, software)))
    assert {
        "complex-change-planning",
        "language-engineering",
        "legacy-preservation",
        "project-architecture",
        "secure-by-design",
        "testing-strategy",
    } <= software_ids
    assert software_ids.isdisjoint(
        {
            "backend-security",
            "architecture-decisions",
            "project-conventions",
            "spec-audit",
            "independent-review",
            "scalability-architecture",
        }
    )
    mobile = DetectedProjectStack(
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset({"mobile-app", "software-project"}),
    )
    assert {"mobile-application", "secure-by-design"} <= set(
        _ids(resolve_skills(definitions, mobile))
    )


def test_secure_by_design_accompanies_software_project_stack(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    software_project_stack = DetectedProjectStack(
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset({"software-project"}),
    )
    unrelated = frozenset({"public-frontend", "mobile-application", "frontend-design"})
    ids = set(_ids(resolve_skills(definitions, software_project_stack)))
    assert "secure-by-design" in ids
    assert ids.isdisjoint(unrelated)
    assert "testing-strategy" in ids


def test_builtin_pack_routes_deep_quality_guidance_by_stack_and_intent(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    by_id = {definition.skill_id: definition for definition in definitions}

    assert {
        "complex-change-planning",
        "container-infrastructure",
        "data-integrity",
        "language-engineering",
        "project-architecture",
        "public-frontend",
        "frontend-design",
        "secure-by-design",
        "mobile-application",
        "server-application",
        "godot-development",
        "deployment-operations",
        "legacy-preservation",
    } <= set(by_id)
    assert by_id.keys().isdisjoint(
        {
            "architecture-decisions",
            "backend-security",
            "independent-review",
            "project-conventions",
            "scalability-architecture",
            "spec-audit",
        }
    )
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
    assert {
        "frontend-design",
        "language-engineering",
        "public-frontend",
        "testing-strategy",
    } <= frontend_ids

    static_frontend = DetectedProjectStack(
        frozenset({"css", "html"}),
        frozenset(),
        frozenset(),
        frozenset({"software-project", "web-frontend"}),
    )
    assert {"frontend-design", "public-frontend"} <= set(
        _ids(resolve_skills(definitions, static_frontend))
    )

    mobile = DetectedProjectStack(
        frozenset({"typescript"}),
        frozenset({"expo", "react", "react-dom", "react-native", "react-native-web"}),
        frozenset({"package.json"}),
        frozenset({"mobile-app", "software-project"}),
    )
    mobile_ids = set(_ids(resolve_skills(definitions, mobile)))
    assert {
        "frontend-design",
        "language-engineering",
        "mobile-application",
        "secure-by-design",
    } <= mobile_ids
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

    docker_stack = resolve_skills(
        definitions,
        DetectedProjectStack(
            frozenset(),
            frozenset(),
            frozenset(),
            frozenset({"containerized"}),
        ),
    )
    assert "container-infrastructure" in _ids(docker_stack)
    assert (
        _ids(
            resolve_skills(definitions, DetectedProjectStack(frozenset(), frozenset(), frozenset()))
        )
        == ()
    )


def test_builtin_pack_keeps_polyglot_workspace_surfaces(tmp_path: Path) -> None:
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
        "legacy-preservation",
        "mobile-application",
        "secure-by-design",
        "server-application",
        "testing-strategy",
    } <= repository_baseline


def test_frontend_design_accompanies_every_builtin_frontend_signal(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)

    for facet in ("web-frontend", "mobile-app"):
        stack = DetectedProjectStack(
            frozenset(),
            frozenset(),
            frozenset(),
            frozenset({facet}),
        )
        assert "frontend-design" in _ids(resolve_skills(definitions, stack)), facet


def test_frontend_design_routes_surface_guidance_and_visual_review(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definition = next(
        item for item in load_skill_registry(registry) if item.skill_id == "frontend-design"
    )

    assert definition.portable_files == (
        PurePosixPath("SKILL.md"),
        PurePosixPath("references/marketing-sites.md"),
        PurePosixPath("references/product-interfaces.md"),
        PurePosixPath("references/visual-language.md"),
        PurePosixPath("references/visual-review.md"),
    )


def test_builtin_descriptions_state_their_activation_boundary() -> None:
    for skill in BUILTIN_SKILLS:
        assert "when" in skill.description.casefold(), skill.skill_id


def test_builtin_descriptions_start_with_use_when() -> None:
    for skill in BUILTIN_SKILLS:
        assert skill.description.startswith("Use when"), skill.skill_id


def test_builtin_pack_omits_task_hints_and_requires_stack_applies(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    for skill in BUILTIN_SKILLS:
        assert skill.task_hints == (), skill.skill_id
        assert (
            skill.applies_languages
            or skill.applies_dependencies
            or skill.applies_manifests
            or skill.applies_facets
        ), skill.skill_id
        metadata = (registry / skill.skill_id / "harness.yaml").read_text(encoding="utf-8")
        assert "task_hints:" not in metadata, skill.skill_id


def test_merged_quality_guidance_is_routed_from_surviving_skills(tmp_path: Path) -> None:
    architecture = _builtin_by_id("project-architecture")
    change = _builtin_by_id("complex-change-planning")
    language = _builtin_by_id("language-engineering")
    legacy = _builtin_by_id("legacy-preservation")
    security_web = dict(_builtin_by_id("secure-by-design").references)["web-backend.md"]
    assert architecture.applies_facets == ("software-project",)
    assert change.applies_facets == ("software-project",)
    assert language.applies_facets == ("software-project",)
    assert language.applies_languages
    assert legacy.applies_facets == ("software-project",)
    assert legacy.task_hints == ()
    assert "characterization, contract, or golden tests" in legacy.body
    assert dict(architecture.references)["architecture-decisions.md"]
    assert (
        "Record an ADR only for durable decisions"
        in dict(architecture.references)["architecture-decisions.md"]
    )
    assert "measured workload" in dict(architecture.references)["scalability.md"]
    assert (
        "independently test the requested behavior"
        in dict(change.references)["specification-audit.md"]
    )
    assert "as if you did not implement it" in dict(change.references)["independent-review.md"]
    assert "legacy-preservation.md" not in dict(change.references)
    assert "legacy-preservation" in change.body
    assert change.description.startswith(
        "Use when planning a cross-boundary or migration-ordered change"
    )
    assert "preserving legacy compatibility" not in change.description
    assert (
        "exclude ordinary single-module bugfixes and routine test-only work" in change.description
    )
    assert legacy.description.startswith("Use when")
    assert "established behavior" in legacy.description
    assert "greenfield-only" in legacy.description
    testing = _builtin_by_id("testing-strategy")
    conventions = " ".join(testing.body.split())
    assert "Do not duplicate facts Harness can derive from manifests" in conventions
    assert "canonical task runner" in conventions
    assert "unsafe operations" in conventions
    assert "Argon2id" in security_web
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    by_id = {definition.skill_id: definition for definition in definitions}
    assert by_id["project-architecture"].portable_files == (
        PurePosixPath("SKILL.md"),
        PurePosixPath("references/architecture-decisions.md"),
        PurePosixPath("references/scalability.md"),
    )
    assert by_id["complex-change-planning"].portable_files == (
        PurePosixPath("SKILL.md"),
        PurePosixPath("references/independent-review.md"),
        PurePosixPath("references/specification-audit.md"),
    )
    assert by_id["legacy-preservation"].portable_files == (PurePosixPath("SKILL.md"),)


def test_builtin_pack_fixture_matrix_routes_relevant_surfaces(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)

    def assert_pack(
        stack: DetectedProjectStack,
        required: set[str],
        forbidden: set[str],
    ) -> set[str]:
        ids = set(_ids(resolve_skills(definitions, stack)))
        assert required <= ids
        assert ids.isdisjoint(forbidden)
        return ids

    python_cli = DetectedProjectStack(
        frozenset({"python"}),
        frozenset(),
        frozenset({"pyproject.toml"}),
        frozenset({"software-project"}),
    )
    assert_pack(
        python_cli,
        {
            "complex-change-planning",
            "language-engineering",
            "legacy-preservation",
            "project-architecture",
            "secure-by-design",
            "testing-strategy",
        },
        {
            "ci-release",
            "container-infrastructure",
            "data-integrity",
            "frontend-design",
            "mobile-application",
            "public-frontend",
            "server-application",
        },
    )

    fastapi = DetectedProjectStack(
        frozenset({"python"}),
        frozenset({"alembic", "fastapi", "sqlalchemy"}),
        frozenset({"pyproject.toml"}),
        frozenset({"backend-service", "database-backed", "software-project"}),
    )
    assert_pack(
        fastapi,
        {
            "data-integrity",
            "language-engineering",
            "legacy-preservation",
            "secure-by-design",
            "server-application",
            "testing-strategy",
        },
        {"frontend-design", "mobile-application", "public-frontend"},
    )

    nextjs = DetectedProjectStack(
        frozenset({"typescript"}),
        frozenset({"next"}),
        frozenset({"package.json"}),
        frozenset({"software-project", "web-frontend"}),
    )
    assert_pack(
        nextjs,
        {
            "frontend-design",
            "language-engineering",
            "legacy-preservation",
            "public-frontend",
            "secure-by-design",
            "testing-strategy",
        },
        {"data-integrity", "mobile-application", "server-application"},
    )

    expo = DetectedProjectStack(
        frozenset({"typescript"}),
        frozenset({"expo", "react", "react-dom", "react-native", "react-native-web"}),
        frozenset({"package.json"}),
        frozenset({"mobile-app", "software-project"}),
    )
    assert_pack(
        expo,
        {
            "frontend-design",
            "language-engineering",
            "legacy-preservation",
            "mobile-application",
            "secure-by-design",
            "testing-strategy",
        },
        {"public-frontend", "server-application"},
    )

    docker_ci = DetectedProjectStack(
        frozenset({"python"}),
        frozenset({"alembic", "fastapi", "sqlalchemy"}),
        frozenset({"dockerfile", "pyproject.toml"}),
        frozenset(
            {
                "backend-service",
                "ci-pipeline",
                "containerized",
                "database-backed",
                "software-project",
            }
        ),
    )
    assert_pack(
        docker_ci,
        {
            "ci-release",
            "container-infrastructure",
            "data-integrity",
            "language-engineering",
            "legacy-preservation",
            "secure-by-design",
            "server-application",
            "testing-strategy",
        },
        {"frontend-design", "mobile-application", "public-frontend"},
    )

    mixed = DetectedProjectStack(
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
    mixed_ids = assert_pack(
        mixed,
        {
            "data-integrity",
            "frontend-design",
            "language-engineering",
            "legacy-preservation",
            "mobile-application",
            "secure-by-design",
            "server-application",
            "testing-strategy",
        },
        {"public-frontend"},
    )
    assert "complex-change-planning" in mixed_ids
    assert "project-architecture" in mixed_ids


def test_busy_polyglot_keeps_every_matching_surface(tmp_path: Path) -> None:
    """A broad detected stack keeps every matching surface; nonmatching Godot stays absent."""
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    busy = DetectedProjectStack(
        frozenset({"python", "sql", "typescript"}),
        frozenset(
            {
                "alembic",
                "expo",
                "fastapi",
                "opentelemetry-api",
                "prometheus-client",
                "react-native",
                "sqlalchemy",
            }
        ),
        frozenset({"dockerfile", "package.json", "pyproject.toml"}),
        frozenset(
            {
                "backend-service",
                "ci-pipeline",
                "containerized",
                "database-backed",
                "deployment-ops",
                "mobile-app",
                "software-project",
                "web-frontend",
            }
        ),
    )
    resolved = resolve_skills(definitions, busy)
    ids = _ids(resolved)
    assert len(BUILTIN_SKILLS) == 16
    assert set(ids) == {
        "ci-release",
        "complex-change-planning",
        "container-infrastructure",
        "data-integrity",
        "deployment-operations",
        "frontend-design",
        "language-engineering",
        "legacy-preservation",
        "mobile-application",
        "observability",
        "project-architecture",
        "public-frontend",
        "secure-by-design",
        "server-application",
        "testing-strategy",
    }
    assert "godot-development" not in ids
    observability = next(item for item in resolved if item.definition.skill_id == "observability")
    assert any(reason.startswith("dependency:") for reason in observability.match_reasons)
    assert all(item.match_reasons for item in resolved)


def test_ci_release_is_self_contained_for_github_actions_supply_chain(tmp_path: Path) -> None:
    body = _builtin_by_id("ci-release").body
    for needle in (
        "Preserve repository and organization",
        "full-length commit SHA",
        "expected upstream",
        "GITHUB_TOKEN",
        "untrusted fork pull-request",
        "OIDC",
        "protected environments",
        "provenance and attestations",
    ):
        assert needle in body, needle
    assert "secure-by-design" not in body
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    definitions = load_skill_registry(registry)
    ci = DetectedProjectStack(
        frozenset(),
        frozenset(),
        frozenset(),
        frozenset({"ci-pipeline"}),
    )
    selected = set(_ids(resolve_skills(definitions, ci)))
    assert "ci-release" in selected
    assert "secure-by-design" not in selected


def test_observability_requires_bounded_metric_cardinality() -> None:
    body = _builtin_by_id("observability").body
    assert "Keep metric labels/attributes bounded." in body
    assert "user IDs" in body
    assert "histogram/bucket" in body


def test_frontend_design_uses_current_platform_touch_targets() -> None:
    product = dict(_builtin_by_id("frontend-design").references)["product-interfaces.md"]
    compact = " ".join(product.split())
    assert "44 x 44 logical pixels" not in product
    assert "current accessibility guidance" in compact
    assert "44x44 pt" in compact
    assert "48x48 dp" in compact
    assert "Do not go below the applicable platform/accessibility minimum" in compact


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


@pytest.mark.parametrize(
    "description",
    (
        "Use when the pipeline uses env: production.",
        "Use when a workflow comment would look like # not-a-comment.",
        "Use when the host says \"quoted\" and 'single'.",
        "Use when café naïve 日本語 is in scope.",
        "Use when the first line stays.\nThe second line is still one YAML scalar.",
    ),
)
def test_builtin_files_quote_yaml_sensitive_frontmatter(tmp_path: Path, description: str) -> None:
    skill = BuiltinSkill("quoted-skill", description, ("quoted-skill",), "# Quoted\nBody.\n")
    quoted_name = json.dumps("quoted-skill", ensure_ascii=False)
    quoted_description = json.dumps(description, ensure_ascii=False)
    markdown = skill.files()["SKILL.md"].decode()
    assert f"name: {quoted_name}\n" in markdown
    description_line = next(
        line for line in markdown.splitlines() if line.startswith("description:")
    )
    assert description_line == f"description: {quoted_description}"
    registry = tmp_path / "skills"
    _materialize_builtin(registry, skill)
    definitions = load_skill_registry(registry)
    validate_skill_definitions_for_profiles(definitions, tuple(sorted(supported_skill_profiles())))
    fields = dict(definitions[0].frontmatter_text_fields)
    assert fields["name"] == "quoted-skill"
    assert fields["description"] == description.strip()


@pytest.mark.parametrize(
    "description",
    (
        "",
        "   ",
        "\n",
        "\n\n",
        "Use when line\u2028separator appears.",
        "Use when paragraph\u2029separator appears.",
    ),
)
def test_builtin_files_fail_closed_on_unserializable_description(description: str) -> None:
    skill = BuiltinSkill("quoted-skill", description, ("quoted-skill",), "# Quoted\nBody.\n")
    with pytest.raises(BuiltinSkillError, match="description"):
        skill.files()


def test_isolated_runtime_uses_one_explicit_compatible_skill_profile_set(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "harness.db"
    assert active_skill_profiles_for_runtime(
        database,
        environment={"HARNESS_DEV_ROOT": str(tmp_path)},
    ) == ("codex", "cursor")
    with pytest.raises(SkillRuntimeError, match="unsupported host skill profile"):
        active_skill_profiles_for_runtime(
            database,
            environment={
                "HARNESS_DEV_ROOT": str(tmp_path),
                "HARNESS_DEV_SKILL_PROFILES": "claude-code",
            },
        )
    with pytest.raises(SkillRuntimeError, match="unsupported host skill profile"):
        active_skill_profiles_for_runtime(
            database,
            environment={
                "HARNESS_DEV_ROOT": str(tmp_path),
                "HARNESS_DEV_SKILL_PROFILES": "claude-code,codex,cursor",
            },
        )


def test_builtin_pack_refuses_foreign_collision_without_mutation(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    foreign = registry / "observability"
    foreign.mkdir(parents=True)
    registry.chmod(0o700)
    (foreign / "SKILL.md").write_text("user skill\n")
    (foreign / "harness.yaml").write_text("id: observability\n")
    before = {p.name: p.read_bytes() for p in foreign.iterdir()}
    with pytest.raises(BuiltinSkillCollisionError):
        sync_builtin_skills(registry)
    assert {p.name: p.read_bytes() for p in foreign.iterdir()} == before
    assert not (registry / ".harness-builtin-skills.json").exists()


def test_builtin_pack_refuses_dangling_symlink_collision(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    registry.mkdir()
    registry.chmod(0o700)
    (registry / "observability").symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(BuiltinSkillCollisionError, match="unsafe"):
        sync_builtin_skills(registry)


def test_sync_refuses_group_writable_preexisting_registry(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    registry.mkdir()
    registry.chmod(0o770)
    with pytest.raises(BuiltinSkillError, match="group/other write"):
        sync_builtin_skills(registry)
    assert stat.S_IMODE(registry.stat().st_mode) & 0o022
    assert list(registry.iterdir()) == []


def test_builtin_pack_updates_only_manifest_owned_unmodified_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    original = next(s for s in BUILTIN_SKILLS if s.skill_id == "observability")
    changed = replace(original, body=original.body + "\n- Unique owned-content update probe.\n")
    monkeypatch.setattr(
        builtin_module,
        "BUILTIN_SKILLS",
        tuple(changed if s.skill_id == changed.skill_id else s for s in BUILTIN_SKILLS),
    )
    result = sync_builtin_skills(registry)
    assert result.updated == 1
    assert (
        "Unique owned-content update probe."
        in (registry / "observability" / "SKILL.md").read_text()
    )


def test_builtin_pack_refuses_update_after_user_modifies_owned_skill(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    sync_builtin_skills(registry)
    target = registry / "testing-strategy" / "SKILL.md"
    target.write_text(target.read_text() + "\nUser customization.\n")
    with pytest.raises(BuiltinSkillCollisionError):
        sync_builtin_skills(registry)


def _retirement_skill(skill_id: str = "retired-example") -> BuiltinSkill:
    return BuiltinSkill(
        skill_id,
        f"Use when testing built-in retirement of {skill_id}.",
        (skill_id,),
        f"# {skill_id}\nValid body so the skill would load if left in place.\n",
    )


def _manifest_skill_ids(registry: Path) -> set[str]:
    payload = json.loads((registry / ".harness-builtin-skills.json").read_text(encoding="utf-8"))
    skills = payload["skills"]
    assert isinstance(skills, dict)
    return set(skills)


def _skill_files(target: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    }


def _backup_leftovers(registry: Path) -> list[Path]:
    return sorted(path for path in registry.iterdir() if ".builtin-backup-" in path.name)


def _persist_error() -> BuiltinSkillError:
    return BuiltinSkillError("built-in skill manifest could not be persisted")


def test_builtin_pack_removes_unmodified_retired_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    assert (registry / extra.skill_id).is_dir()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    result = sync_builtin_skills(registry)
    assert result.retired == 1
    assert result.released == 0
    assert extra.skill_id not in result.skill_ids
    assert result.skill_ids == tuple(skill.skill_id for skill in BUILTIN_SKILLS)
    assert not (registry / extra.skill_id).exists()
    assert extra.skill_id not in _manifest_skill_ids(registry)


def test_builtin_pack_rename_removes_old_and_installs_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    old = _retirement_skill("old-quality-skill")
    new = _retirement_skill("new-quality-skill")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, old))
    sync_builtin_skills(registry)
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, new))
    result = sync_builtin_skills(registry)
    assert result.installed == 1
    assert result.retired == 1
    assert result.released == 0
    assert result.unchanged == len(BUILTIN_SKILLS)
    assert old.skill_id not in result.skill_ids
    assert new.skill_id in result.skill_ids
    assert not (registry / old.skill_id).exists()
    assert (registry / new.skill_id).is_dir()
    owned = _manifest_skill_ids(registry)
    assert old.skill_id not in owned
    assert new.skill_id in owned


def test_retired_missing_directory_drops_manifest_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    shutil.rmtree(registry / extra.skill_id)
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    result = sync_builtin_skills(registry)
    assert result.retired == 0
    assert result.released == 0
    assert extra.skill_id not in result.skill_ids
    assert extra.skill_id not in _manifest_skill_ids(registry)
    assert not (registry / extra.skill_id).exists()


def test_modified_retired_skill_is_preserved_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    target = registry / extra.skill_id / "SKILL.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "User customization.\n",
        encoding="utf-8",
    )
    customized = target.read_bytes()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    result = sync_builtin_skills(registry)
    assert result.released == 1
    assert result.retired == 0
    assert extra.skill_id not in result.skill_ids
    assert extra.skill_id not in _manifest_skill_ids(registry)
    assert (registry / extra.skill_id).is_dir()
    assert target.read_bytes() == customized
    definitions = load_skill_registry(registry)
    assert extra.skill_id in {definition.skill_id for definition in definitions}


def test_retired_skill_no_longer_loads_after_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    loaded = {definition.skill_id for definition in load_skill_registry(registry)}
    assert extra.skill_id in loaded
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    sync_builtin_skills(registry)
    loaded = {definition.skill_id for definition in load_skill_registry(registry)}
    assert extra.skill_id not in loaded


def test_retired_skill_no_longer_projects_after_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    empty = DetectedProjectStack(frozenset(), frozenset(), frozenset())
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    before = load_skill_registry(registry)
    assert extra.skill_id in _ids(resolve_skills(before, empty, explicit_include=(extra.skill_id,)))
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    sync_builtin_skills(registry)
    after = load_skill_registry(registry)
    assert extra.skill_id not in {definition.skill_id for definition in after}
    assert extra.skill_id not in _ids(resolve_skills(after, empty))


def test_retirement_rolls_back_if_manifest_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    target = registry / extra.skill_id
    before_files = _skill_files(target)
    before_owned = _manifest_skill_ids(registry)
    before_manifest = (registry / ".harness-builtin-skills.json").read_bytes()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    write_calls = 0

    def fail_write_after_retirement(_path: Path, owned: dict[str, str]) -> None:
        nonlocal write_calls
        write_calls += 1
        assert extra.skill_id not in owned
        assert not target.exists()
        raise _persist_error()

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write_after_retirement)
    with pytest.raises(BuiltinSkillError, match="could not be persisted"):
        sync_builtin_skills(registry)
    assert write_calls == 1
    assert target.is_dir()
    assert _skill_files(target) == before_files
    assert _manifest_skill_ids(registry) == before_owned
    assert (registry / ".harness-builtin-skills.json").read_bytes() == before_manifest
    restored_ids = {definition.skill_id for definition in load_skill_registry(registry)}
    assert extra.skill_id in restored_ids
    assert _backup_leftovers(registry) == []


def test_manifest_write_failure_after_one_replacement_restores_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    skill = _retirement_skill("owned-quality")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (skill,))
    sync_builtin_skills(registry)
    target = registry / skill.skill_id
    before_files = _skill_files(target)
    before_manifest = (registry / ".harness-builtin-skills.json").read_bytes()
    updated = replace(skill, body=skill.body + "Updated owned quality body.\n")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (updated,))
    write_calls = 0

    def fail_write_after_replacement(_path: Path, owned: dict[str, str]) -> None:
        nonlocal write_calls
        write_calls += 1
        assert skill.skill_id in owned
        assert "Updated owned quality body." in (target / "SKILL.md").read_text(encoding="utf-8")
        raise _persist_error()

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write_after_replacement)
    with pytest.raises(BuiltinSkillError, match="could not be persisted"):
        sync_builtin_skills(registry)
    assert write_calls == 1
    assert _skill_files(target) == before_files
    assert (registry / ".harness-builtin-skills.json").read_bytes() == before_manifest
    assert _backup_leftovers(registry) == []


def test_manifest_write_failure_after_multiple_replacements_restores_all_old_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    first = _retirement_skill("first-quality")
    second = _retirement_skill("second-quality")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (first, second))
    sync_builtin_skills(registry)
    first_target = registry / first.skill_id
    second_target = registry / second.skill_id
    before = {
        first.skill_id: _skill_files(first_target),
        second.skill_id: _skill_files(second_target),
    }
    before_manifest = (registry / ".harness-builtin-skills.json").read_bytes()
    monkeypatch.setattr(
        builtin_module,
        "BUILTIN_SKILLS",
        (
            replace(first, body=first.body + "First replacement body.\n"),
            replace(second, body=second.body + "Second replacement body.\n"),
        ),
    )

    def fail_write_after_both(_path: Path, owned: dict[str, str]) -> None:
        assert set(owned) == {first.skill_id, second.skill_id}
        assert "First replacement body." in (first_target / "SKILL.md").read_text(encoding="utf-8")
        assert "Second replacement body." in (second_target / "SKILL.md").read_text(
            encoding="utf-8"
        )
        raise _persist_error()

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write_after_both)
    with pytest.raises(BuiltinSkillError, match="could not be persisted"):
        sync_builtin_skills(registry)
    assert _skill_files(first_target) == before[first.skill_id]
    assert _skill_files(second_target) == before[second.skill_id]
    assert (registry / ".harness-builtin-skills.json").read_bytes() == before_manifest
    assert _backup_leftovers(registry) == []


def test_rollback_target_removal_failure_is_explicit_recovery_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    skill = _retirement_skill("removal-quality")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (skill,))
    sync_builtin_skills(registry)
    target = registry / skill.skill_id
    monkeypatch.setattr(
        builtin_module,
        "BUILTIN_SKILLS",
        (replace(skill, body=skill.body + "New removal body.\n"),),
    )
    real_remove = builtin_module._remove_path
    write_failed = False

    def fail_write(_path: Path, _owned: dict[str, str]) -> None:
        nonlocal write_failed
        write_failed = True
        raise _persist_error()

    def fail_remove(path: Path) -> None:
        if write_failed and path == target:
            raise OSError("cannot remove replacement target")
        real_remove(path)

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write)
    monkeypatch.setattr(builtin_module, "_remove_path", fail_remove)
    with pytest.raises(
        BuiltinSkillError, match="prior registry state could not be restored"
    ) as caught:
        sync_builtin_skills(registry)
    assert write_failed
    outer = str(caught.value)
    assert "could not be persisted" not in outer
    assert "preserved at" in outer
    leftovers = _backup_leftovers(registry)
    assert len(leftovers) == 1
    assert leftovers[0].is_dir()
    assert str(leftovers[0]) in outer
    cause = caught.value.__cause__
    assert cause is not None
    assert "preserved at" in str(cause)
    assert str(leftovers[0]) in str(cause)
    assert "New removal body." in (target / "SKILL.md").read_text(encoding="utf-8")


def test_rollback_backup_restore_failure_preserves_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    skill = _retirement_skill("restore-quality")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (skill,))
    sync_builtin_skills(registry)
    target = registry / skill.skill_id
    before_files = _skill_files(target)
    monkeypatch.setattr(
        builtin_module,
        "BUILTIN_SKILLS",
        (replace(skill, body=skill.body + "New restore body.\n"),),
    )
    real_replace = os.replace
    write_failed = False

    def fail_write(_path: Path, _owned: dict[str, str]) -> None:
        nonlocal write_failed
        write_failed = True
        raise _persist_error()

    def fail_restore(source: Path, dest: Path) -> None:
        if write_failed and dest == target:
            raise OSError("cannot restore backup")
        real_replace(source, dest)

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write)
    monkeypatch.setattr("harness.builtin_skills.os.replace", fail_restore)
    with pytest.raises(
        BuiltinSkillError, match="prior registry state could not be restored"
    ) as caught:
        sync_builtin_skills(registry)
    assert write_failed
    outer = str(caught.value)
    assert "could not be persisted" not in outer
    assert "preserved at" in outer
    leftovers = _backup_leftovers(registry)
    assert len(leftovers) == 1
    assert leftovers[0].is_dir()
    assert str(leftovers[0]) in outer
    cause = caught.value.__cause__
    assert cause is not None
    assert "preserved at" in str(cause)
    assert str(leftovers[0]) in str(cause)
    assert _skill_files(leftovers[0]) == before_files
    assert not target.exists()


def test_rollback_continues_after_one_item_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    first = _retirement_skill("first-rollback")
    second = _retirement_skill("second-rollback")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (first, second))
    sync_builtin_skills(registry)
    first_target = registry / first.skill_id
    second_target = registry / second.skill_id
    before_first = _skill_files(first_target)
    monkeypatch.setattr(
        builtin_module,
        "BUILTIN_SKILLS",
        (
            replace(first, body=first.body + "First rollback body.\n"),
            replace(second, body=second.body + "Second rollback body.\n"),
        ),
    )
    real_exists = builtin_module._path_exists
    write_failed = False
    inspected_after_write: list[Path] = []

    def fail_write(_path: Path, _owned: dict[str, str]) -> None:
        nonlocal write_failed
        write_failed = True
        assert "First rollback body." in (first_target / "SKILL.md").read_text(encoding="utf-8")
        assert "Second rollback body." in (second_target / "SKILL.md").read_text(encoding="utf-8")
        raise _persist_error()

    def fail_second_inspection(path: Path) -> bool:
        if write_failed:
            inspected_after_write.append(path)
            if path == second_target:
                raise BuiltinSkillError(f"skill registry entry cannot be inspected: {path.name}")
        return real_exists(path)

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write)
    monkeypatch.setattr(builtin_module, "_path_exists", fail_second_inspection)
    with pytest.raises(
        BuiltinSkillError, match="prior registry state could not be restored"
    ) as caught:
        sync_builtin_skills(registry)
    assert write_failed
    assert second_target in inspected_after_write
    assert first_target in inspected_after_write
    assert inspected_after_write.index(second_target) < inspected_after_write.index(first_target)
    assert _skill_files(first_target) == before_first
    assert "Second rollback body." in (second_target / "SKILL.md").read_text(encoding="utf-8")
    leftovers = _backup_leftovers(registry)
    assert len(leftovers) == 1
    assert leftovers[0].name.startswith(f".{second.skill_id}.builtin-backup-")
    outer = str(caught.value)
    assert "could not be persisted" not in outer
    assert "preserved at" in outer
    assert str(leftovers[0]) in outer


def test_rename_rolls_back_if_manifest_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    old = _retirement_skill("old-quality-skill")
    new = _retirement_skill("new-quality-skill")
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (old,))
    sync_builtin_skills(registry)
    old_target = registry / old.skill_id
    new_target = registry / new.skill_id
    before_files = _skill_files(old_target)
    before_manifest = (registry / ".harness-builtin-skills.json").read_bytes()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (new,))
    write_calls = 0

    def fail_write_after_rename(_path: Path, owned: dict[str, str]) -> None:
        nonlocal write_calls
        write_calls += 1
        assert old.skill_id not in owned
        assert new.skill_id in owned
        assert not old_target.exists()
        assert new_target.is_dir()
        raise _persist_error()

    monkeypatch.setattr(builtin_module, "_write_manifest", fail_write_after_rename)
    with pytest.raises(BuiltinSkillError, match="could not be persisted"):
        sync_builtin_skills(registry)
    assert write_calls == 1
    assert old_target.is_dir()
    assert _skill_files(old_target) == before_files
    assert not new_target.exists()
    assert (registry / ".harness-builtin-skills.json").read_bytes() == before_manifest
    assert old.skill_id in _manifest_skill_ids(registry)
    assert new.skill_id not in _manifest_skill_ids(registry)
    assert _backup_leftovers(registry) == []


def test_retired_unsafe_symlink_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "skills"
    extra = _retirement_skill()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", (*BUILTIN_SKILLS, extra))
    sync_builtin_skills(registry)
    target = registry / extra.skill_id
    shutil.rmtree(target)
    target.symlink_to(tmp_path / "missing", target_is_directory=True)
    before_manifest = (registry / ".harness-builtin-skills.json").read_bytes()
    monkeypatch.setattr(builtin_module, "BUILTIN_SKILLS", BUILTIN_SKILLS)
    with pytest.raises(BuiltinSkillCollisionError, match="unsafe"):
        sync_builtin_skills(registry)
    assert target.is_symlink()
    assert (registry / ".harness-builtin-skills.json").read_bytes() == before_manifest
    assert extra.skill_id in _manifest_skill_ids(registry)