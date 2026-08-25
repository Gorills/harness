from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

import harness.skills as skills_module
from harness.host_adapters import (
    ClaudeCodeAdapter,
    codex_skill_projection_surface,
    cursor_skill_projection_surface,
)
from harness.skills import (
    DetectedProjectStack,
    ResolvedSkill,
    SkillProjectionCollisionError,
    SkillProjectionError,
    apply_skill_projection,
    load_skill_registry,
    plan_skill_projection,
    resolve_skills,
)


def _resolved_skill(
    registry: Path, skill_text: str, *, skill_id: str = "fastapi"
) -> tuple[ResolvedSkill, ...]:
    skill = registry / skill_id
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (skill / "harness.yaml").write_text(
        f"id: {skill_id}\ntask_hints:\n  - {skill_id}\n",
        encoding="utf-8",
    )
    return resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=(skill_id,),
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _valid_skill_text() -> str:
    return (
        "---\n"
        "name: fastapi\n"
        "description: Apply the project FastAPI conventions.\n"
        "---\n\n"
        "# FastAPI\n\nPortable instructions.\n"
    )


def test_cursor_skill_projection_surface_uses_documented_compatibility_roots() -> None:
    surface = cursor_skill_projection_surface()

    assert surface.profile == "cursor"
    assert surface.target_root == PurePosixPath(".agents/skills")
    assert surface.visible_roots == (
        PurePosixPath(".agents/skills"),
        PurePosixPath(".cursor/skills"),
        PurePosixPath(".claude/skills"),
        PurePosixPath(".codex/skills"),
    )
    assert surface.required_frontmatter_fields == ("name", "description")
    assert surface.recursive_visible_roots == (
        PurePosixPath(".agents/skills"),
        PurePosixPath(".cursor/skills"),
    )
    assert surface.frontmatter_name_must_match_skill_id is True
    assert surface.frontmatter_name_pattern == r"[a-z0-9-]+"


def test_cursor_projection_rejects_skill_without_required_frontmatter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", "# FastAPI\n\nPortable instructions.\n")

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description, name"):
        plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))


def test_cursor_projection_requires_name_to_match_projected_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        "---\nname: other\ndescription: Apply FastAPI conventions.\n---\n",
    )

    with pytest.raises(SkillProjectionError, match="name must match"):
        plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))


def test_cursor_projection_rejects_name_outside_documented_character_set(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        "---\nname: fast_api\ndescription: Apply FastAPI conventions.\n---\n",
        skill_id="fast_api",
    )

    with pytest.raises(SkillProjectionError, match="unsupported format"):
        plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))


def test_cursor_only_projection_materializes_agents_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    skill_text = _valid_skill_text()
    resolved = _resolved_skill(tmp_path / "registry", skill_text)

    plan = plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))
    result = apply_skill_projection(plan)

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )
    assert result.materialized == 1
    target = workspace / ".agents" / "skills" / "fastapi"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == skill_text
    assert (target / ".harness-skill.json").is_file()
    assert not (target / "harness.yaml").exists()
    assert _git(workspace, "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md").returncode == 0


def test_cursor_reuses_single_root_with_claude_or_codex(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text())
    cursor = cursor_skill_projection_surface()
    claude = ClaudeCodeAdapter(Path("/claude"), Path("/python")).skill_projection_surface()
    codex = codex_skill_projection_surface()

    claude_cursor = plan_skill_projection(workspace, resolved, (claude, cursor))
    codex_cursor = plan_skill_projection(workspace, resolved, (codex, cursor))

    assert tuple(target.relative_root for target in claude_cursor.targets) == (
        PurePosixPath(".claude/skills"),
    )
    assert tuple(target.relative_root for target in codex_cursor.targets) == (
        PurePosixPath(".agents/skills"),
    )


def test_cursor_fails_closed_when_claude_and_codex_would_be_duplicate_visible(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text())
    cursor = cursor_skill_projection_surface()
    claude = ClaudeCodeAdapter(Path("/claude"), Path("/python")).skill_projection_surface()
    codex = codex_skill_projection_surface()

    with pytest.raises(SkillProjectionCollisionError, match="duplicate-free"):
        plan_skill_projection(workspace, resolved, (claude, codex, cursor))


@pytest.mark.parametrize(
    "relative",
    (
        PurePosixPath(".cursor/skills/fastapi"),
        PurePosixPath("apps/web/.cursor/skills/fastapi"),
        PurePosixPath("apps/web/.agents/skills/fastapi"),
        PurePosixPath(".cursor/skills/team/fastapi"),
        PurePosixPath("apps/web/.cursor/skills/team/fastapi"),
    ),
)
def test_cursor_projection_refuses_user_skill_in_visible_root(
    tmp_path: Path, relative: PurePosixPath
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text())
    duplicate = workspace / Path(*relative.parts)
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("# user-owned\n", encoding="utf-8")

    with pytest.raises(SkillProjectionCollisionError, match="duplicate Harness projection"):
        apply_skill_projection(
            plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))
        )

    assert (duplicate / "SKILL.md").read_text(encoding="utf-8") == "# user-owned\n"
    assert not (workspace / ".agents" / "skills" / "fastapi").exists()


def test_cursor_projection_allows_unrelated_nested_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text())
    unrelated = workspace / "apps" / "web" / ".cursor" / "skills" / "other"
    unrelated.mkdir(parents=True)
    (unrelated / "SKILL.md").write_text("# user-owned\n", encoding="utf-8")

    result = apply_skill_projection(
        plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))
    )

    assert result.materialized == 1
    assert (workspace / ".agents" / "skills" / "fastapi" / "SKILL.md").is_file()
    assert (unrelated / "SKILL.md").read_text(encoding="utf-8") == "# user-owned\n"


def test_cursor_projection_rechecks_nested_visibility_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text())
    duplicate = workspace / "apps" / "web" / ".cursor" / "skills" / "team" / "fastapi"
    original_build = skills_module._build_projected_skill

    def build_with_race(parent: Path, definition: skills_module.SkillDefinition) -> Path:
        replacement = original_build(parent, definition)
        duplicate.mkdir(parents=True)
        (duplicate / "SKILL.md").write_text("# raced user skill\n", encoding="utf-8")
        return replacement

    monkeypatch.setattr(skills_module, "_build_projected_skill", build_with_race)

    with pytest.raises(SkillProjectionCollisionError, match="duplicate Harness projection"):
        apply_skill_projection(
            plan_skill_projection(workspace, resolved, (cursor_skill_projection_surface(),))
        )

    assert not (workspace / ".agents" / "skills" / "fastapi").exists()
    assert (duplicate / "SKILL.md").read_text(encoding="utf-8") == "# raced user skill\n"
