from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from harness.host_adapters import (
    antigravity_cli_skill_projection_surface,
    antigravity_ide_skill_projection_surface,
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


def _resolved_skill(registry: Path, skill_text: str) -> tuple[ResolvedSkill, ...]:
    skill = registry / "fastapi"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (skill / "harness.yaml").write_text(
        "id: fastapi\ntask_hints:\n  - fastapi\n",
        encoding="utf-8",
    )
    return resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _valid_skill_text(*, include_name: bool = True) -> str:
    name = "name: fastapi\n" if include_name else ""
    return (
        "---\n"
        f"{name}"
        "description: Apply the project FastAPI conventions.\n"
        "---\n\n"
        "# FastAPI\n\nPortable instructions.\n"
    )


def test_antigravity_ide_skill_surface_uses_documented_workspace_contract() -> None:
    surface = antigravity_ide_skill_projection_surface()

    assert surface.profile == "antigravity-ide"
    assert surface.target_root == PurePosixPath(".agents/skills")
    assert surface.visible_roots == (
        PurePosixPath(".agents/skills"),
        PurePosixPath(".agent/skills"),
    )
    assert surface.required_frontmatter_fields == ("description",)
    assert surface.recursive_visible_roots == ()


def test_antigravity_ide_projection_allows_omitted_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text(include_name=False))

    plan = plan_skill_projection(workspace, resolved, (antigravity_ide_skill_projection_surface(),))

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )


def test_antigravity_ide_projection_rejects_missing_description(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", "---\nname: fastapi\n---\n")

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description"):
        plan_skill_projection(workspace, resolved, (antigravity_ide_skill_projection_surface(),))


@pytest.mark.parametrize("invalid_description", ("[not, text]", "{kind: mapping}", "true", "123"))
def test_antigravity_ide_projection_rejects_non_text_description(
    tmp_path: Path, invalid_description: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(
        tmp_path / "registry",
        f"---\nname: fastapi\ndescription: {invalid_description}\n---\n",
    )

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description"):
        plan_skill_projection(workspace, resolved, (antigravity_ide_skill_projection_surface(),))


def test_antigravity_ide_only_projection_materializes_agents_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    skill_text = _valid_skill_text(include_name=False)
    resolved = _resolved_skill(tmp_path / "registry", skill_text)

    plan = plan_skill_projection(workspace, resolved, (antigravity_ide_skill_projection_surface(),))
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


def test_antigravity_ide_reuses_agents_root_with_codex_or_cursor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text())
    antigravity = antigravity_ide_skill_projection_surface()

    with_codex = plan_skill_projection(
        workspace,
        resolved,
        (antigravity, codex_skill_projection_surface()),
    )
    with_cursor = plan_skill_projection(
        workspace,
        resolved,
        (antigravity, cursor_skill_projection_surface()),
    )

    expected = (PurePosixPath(".agents/skills"),)
    assert tuple(target.relative_root for target in with_codex.targets) == expected
    assert tuple(target.relative_root for target in with_cursor.targets) == expected


def test_antigravity_ide_refuses_same_id_legacy_agent_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text(include_name=False))
    duplicate = workspace / ".agent" / "skills" / "fastapi"
    duplicate.mkdir(parents=True)
    (duplicate / "SKILL.md").write_text("# user-owned\n", encoding="utf-8")

    with pytest.raises(SkillProjectionCollisionError, match="duplicate Harness projection"):
        apply_skill_projection(
            plan_skill_projection(
                workspace,
                resolved,
                (antigravity_ide_skill_projection_surface(),),
            )
        )

    assert (duplicate / "SKILL.md").read_text(encoding="utf-8") == "# user-owned\n"
    assert not (workspace / ".agents" / "skills" / "fastapi").exists()


def test_antigravity_cli_skill_surface_uses_current_workspace_contract() -> None:
    surface = antigravity_cli_skill_projection_surface()

    assert surface.profile == "antigravity-cli"
    assert surface.target_root == PurePosixPath(".agents/skills")
    assert surface.visible_roots == (PurePosixPath(".agents/skills"),)
    assert surface.required_frontmatter_fields == ("description",)
    assert surface.recursive_visible_roots == ()


def test_antigravity_cli_projection_allows_omitted_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text(include_name=False))

    plan = plan_skill_projection(workspace, resolved, (antigravity_cli_skill_projection_surface(),))

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )


def test_antigravity_cli_projection_rejects_missing_description(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", "---\nname: fastapi\n---\n")

    with pytest.raises(SkillProjectionError, match=r"frontmatter.*description"):
        plan_skill_projection(workspace, resolved, (antigravity_cli_skill_projection_surface(),))


def test_antigravity_cli_only_projection_materializes_folder_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    skill_text = _valid_skill_text(include_name=False)
    resolved = _resolved_skill(tmp_path / "registry", skill_text)

    plan = plan_skill_projection(workspace, resolved, (antigravity_cli_skill_projection_surface(),))
    result = apply_skill_projection(plan)

    assert result.materialized == 1
    target = workspace / ".agents" / "skills" / "fastapi"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == skill_text
    assert (target / ".harness-skill.json").is_file()
    assert not (workspace / ".agents" / "skills" / "fastapi.md").exists()
    assert _git(workspace, "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md").returncode == 0


def test_antigravity_cli_and_ide_reuse_one_agents_projection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text(include_name=False))

    plan = plan_skill_projection(
        workspace,
        resolved,
        (
            antigravity_cli_skill_projection_surface(),
            antigravity_ide_skill_projection_surface(),
        ),
    )

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )


def test_antigravity_cli_does_not_claim_ide_legacy_visibility(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    resolved = _resolved_skill(tmp_path / "registry", _valid_skill_text(include_name=False))
    legacy = workspace / ".agent" / "skills" / "fastapi"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# IDE legacy only\n", encoding="utf-8")

    result = apply_skill_projection(
        plan_skill_projection(workspace, resolved, (antigravity_cli_skill_projection_surface(),))
    )

    assert result.materialized == 1
    assert (workspace / ".agents" / "skills" / "fastapi" / "SKILL.md").is_file()
    assert (legacy / "SKILL.md").read_text(encoding="utf-8") == "# IDE legacy only\n"
