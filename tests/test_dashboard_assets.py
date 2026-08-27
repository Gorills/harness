from __future__ import annotations

from harness.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS


def test_dashboard_uses_modern_control_room_visual_language() -> None:
    assert "--canvas: #090b10;" in DASHBOARD_CSS
    assert "--accent: #8b7cff;" in DASHBOARD_CSS
    assert ".control-brief" in DASHBOARD_CSS
    assert ".brief-card" in DASHBOARD_CSS
    assert ".project-chip" in DASHBOARD_CSS
    assert ".context-rail" in DASHBOARD_CSS
    assert "font-family: inherit;" in DASHBOARD_CSS
    assert '"Iowan Old Style"' not in DASHBOARD_CSS
    assert "@media (prefers-color-scheme: light)" in DASHBOARD_CSS
    assert "@media (prefers-reduced-motion: reduce)" in DASHBOARD_CSS


def test_projects_home_enhancement_exposes_architecture_and_project_navigation() -> None:
    assert "const enhanceProjectsHome = () =>" in DASHBOARD_JS
    assert "LOCAL CONTROL PLANE" in DASHBOARD_JS
    assert "PROJECT KNOWLEDGE" in DASHBOARD_JS
    assert "AGENT WORKFLOW" in DASHBOARD_JS
    assert "provenance, anchors и freshness" in DASHBOARD_JS
    assert 'input[name="project_id"]' in DASHBOARD_JS
    assert "encodeURIComponent(projectId)" in DASHBOARD_JS
    assert "Knowledge хранится на уровне проекта" in DASHBOARD_JS
    assert ".innerHTML" not in DASHBOARD_JS


def test_dashboard_refresh_guard_preserves_any_modified_user_input() -> None:
    """A second editable field must not clear another field's dirty state."""
    assert "const hasUnsavedInput = () => Array.from(" in DASHBOARD_JS
    assert "document.querySelectorAll('textarea, input[type=\"search\"]')" in DASHBOARD_JS
    assert ".some((field) => field.value !== field.defaultValue);" in DASHBOARD_JS
    assert "if (hasUnsavedInput())" in DASHBOARD_JS
    assert "let hasUnsavedInput" not in DASHBOARD_JS
    assert "document.addEventListener('input'" not in DASHBOARD_JS
    assert "document.addEventListener('submit'" not in DASHBOARD_JS
