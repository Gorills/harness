from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_skill_relevance_reconciliation import _registered_workspace, _write_skill

from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.skill_runtime import cleanup_projected_skills, reconcile_workspace_skills


@pytest.mark.parametrize(
    "malformed_overlay", (False, True), ids=("valid-overlay", "malformed-overlay")
)
def test_global_cleanup_preserves_development_projection_and_cleans_ordinary_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed_overlay: bool
) -> None:
    root, _, connection, workspace_id = _registered_workspace(tmp_path)
    registry = tmp_path / "skills"
    _write_skill(registry, "baseline", facets=("software-project",))
    monkeypatch.setenv("HARNESS_SKILL_REGISTRY", str(registry))
    overlay = root / ".cursor" / "mcp.json"
    overlay.parent.mkdir()
    overlay_payload = (
        {"mcpServers": 7}
        if malformed_overlay
        else {
            "mcpServers": {
                "harness-dev": {
                    "command": "${workspaceFolder}/scripts/dogfood",
                    "args": ["mcp"],
                    "env": {"HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"},
                }
            }
        }
    )
    overlay.write_text(json.dumps(overlay_payload))
    overlay_before = overlay.read_bytes()
    ordinary_root = tmp_path / "ordinary"
    ordinary_root.mkdir()
    try:
        project = create_project(connection)
        ordinary = register_workspace(connection, project_id=project.project_id, path=ordinary_root)
        scan_workspace(connection, ordinary.workspace_id)
        monkeypatch.setenv("HARNESS_DEV_ROOT", str(root))
        reconcile_workspace_skills(connection, workspace_id, ("codex",))
        reconcile_workspace_skills(connection, ordinary.workspace_id, ("codex",))
        source_skill = root / ".agents" / "skills" / "baseline" / "SKILL.md"
        before = source_skill.read_bytes()
        monkeypatch.delenv("HARNESS_DEV_ROOT")
        result = cleanup_projected_skills(connection, ("codex",))
        assert result.workspace_count == 2
        assert result.skipped_workspace_count == 1
        assert result.cleaned_workspace_count == 1
        assert result.removed == 1
        assert source_skill.read_bytes() == before
        assert overlay.read_bytes() == overlay_before
        assert not (ordinary_root / ".agents" / "skills" / "baseline").exists()
    finally:
        connection.close()
