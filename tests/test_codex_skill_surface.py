from __future__ import annotations

from pathlib import Path, PurePosixPath

from harness.host_adapters import ClaudeCodeAdapter, codex_skill_projection_surface
from harness.skills import plan_skill_projection


def test_codex_skill_projection_surface_uses_documented_repository_root() -> None:
    surface = codex_skill_projection_surface()

    assert surface.profile == "codex"
    assert surface.target_root == PurePosixPath(".agents/skills")
    assert surface.visible_roots == (PurePosixPath(".agents/skills"),)


def test_claude_and_codex_projection_surfaces_require_distinct_native_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude = ClaudeCodeAdapter(Path("/claude"), Path("/python")).skill_projection_surface()
    codex = codex_skill_projection_surface()

    plan = plan_skill_projection(workspace, (), (claude, codex))

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
        PurePosixPath(".claude/skills"),
    )
    assert plan.managed_roots == (
        PurePosixPath(".agents/skills"),
        PurePosixPath(".claude/skills"),
    )
