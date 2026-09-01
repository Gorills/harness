from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from harness.host_adapters import (
    codex_skill_projection_surface,
    cursor_skill_projection_surface,
)
from harness.skills import (
    DetectedProjectStack,
    ResolvedSkill,
    SkillProjectionError,
    apply_skill_projection,
    load_skill_registry,
    plan_skill_projection,
    resolve_skills,
)


def _resolved_skill(registry: Path, skill_text: str) -> tuple[ResolvedSkill, ...]:
    skill = registry / "fastapi"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (skill / "harness.yaml").write_text(
        "id: fastapi\ntask_hints:\n  - fastapi\n",
        encoding="utf-8",
    )
    definitions = load_skill_registry(registry)
    return resolve_skills(
        definitions,
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def test_codex_skill_projection_surface_uses_documented_repository_contract() -> None:
    surface = codex_skill_projection_surface()

    assert surface.profile == "codex"
    assert surface.target_root == PurePosixPath(".agents/skills")
    assert surface.visible_roots == (PurePosixPath(".agents/skills"),)
    assert surface.required_frontmatter_fields == ("name", "description")


def test_codex_projection_rejects_skill_without_required_frontmatter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", "# FastAPI\n\nPortable instructions.\n")

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description, name"):
        plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))


def test_codex_projection_rejects_structurally_malformed_frontmatter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        "---\nname: fastapi\ndescription: Looks valid\n- malformed\n---\n\n# FastAPI\n",
    )

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description, name"):
        plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))


def test_codex_projection_rejects_duplicate_top_level_frontmatter_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        "---\nname: fastapi\nname: duplicate\ndescription: Looks valid\n---\n",
    )

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description, name"):
        plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))


@pytest.mark.parametrize(
    "invalid_description",
    ("[not, text]", "{kind: mapping}", "true", "123"),
)
def test_codex_projection_rejects_non_text_description_values(
    tmp_path: Path, invalid_description: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        f"---\nname: fastapi\ndescription: {invalid_description}\n---\n",
    )

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description"):
        plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))


def test_codex_projection_rejects_python_only_double_quote_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        '---\nname: fastapi\ndescription: "f\\141stapi"\n---\n',
    )

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description"):
        plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))


def test_codex_projection_accepts_non_empty_block_scalar_description(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        "---\nname: fastapi\ndescription: |-\n  Apply the project FastAPI conventions.\n---\n",
    )

    plan = plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )


def test_codex_projection_does_not_copy_registry_content_changed_after_resolution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    registry = tmp_path / "registry"
    resolved = _resolved_skill(
        registry,
        "---\n"
        "name: fastapi\n"
        "description: Apply the project FastAPI conventions.\n"
        "---\n\n"
        "# FastAPI\n",
    )
    plan = plan_skill_projection(workspace, resolved, (codex_skill_projection_surface(),))
    (registry / "fastapi" / "SKILL.md").write_text("# changed after resolution\n", encoding="utf-8")

    with pytest.raises(SkillProjectionError, match="registry content changed"):
        apply_skill_projection(plan)

    assert not (workspace / ".agents" / "skills" / "fastapi").exists()


def test_codex_and_cursor_projection_shares_agents_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    skill_text = (
        "---\n"
        "name: fastapi\n"
        "description: Apply the project FastAPI conventions.\n"
        "---\n\n"
        "# FastAPI\n\nPortable instructions.\n"
    )
    resolved = _resolved_skill(tmp_path / "registry", skill_text)
    plan = plan_skill_projection(
        workspace,
        resolved,
        (codex_skill_projection_surface(), cursor_skill_projection_surface()),
    )
    result = apply_skill_projection(plan)

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )
    assert result.materialized == 1
    target = workspace / ".agents" / "skills" / "fastapi"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == skill_text
    assert (target / ".harness-skill.json").is_file()
    assert not (target / "harness.yaml").exists()
    ignored = _git(workspace, "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md")
    assert ignored.returncode == 0
