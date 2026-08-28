from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import harness.builtin_skills as builtin_module
from harness.builtin_skills import BUILTIN_SKILLS, BuiltinSkillCollisionError, sync_builtin_skills
from harness.skill_runtime import supported_skill_profiles, validate_skill_definitions_for_profiles
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
        "backend-security",
        "testing-strategy",
    )
    assert _ids(resolve_skills(definitions, empty, task_hints=("complex-change",))) == (
        "architecture-decisions",
        "complex-change-planning",
        "independent-review",
        "spec-audit",
        "testing-strategy",
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
