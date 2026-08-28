from __future__ import annotations

from harness.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS


def test_dashboard_uses_modern_product_workspace_visual_language() -> None:
    assert "--bg: #0b0d12;" in DASHBOARD_CSS
    assert "--accent: #748cff;" in DASHBOARD_CSS
    assert ".app-layout" in DASHBOARD_CSS
    assert ".app-sidebar" in DASHBOARD_CSS
    assert ".project-navigation" in DASHBOARD_CSS
    assert ".project-section" in DASHBOARD_CSS
    assert ".workspace-layout" in DASHBOARD_CSS
    assert '"Segoe UI Variable"' in DASHBOARD_CSS
    assert '"Iowan Old Style"' not in DASHBOARD_CSS
    assert "@media (prefers-color-scheme: light)" in DASHBOARD_CSS
    assert "@media (prefers-reduced-motion: reduce)" in DASHBOARD_CSS


def test_dashboard_navigation_is_server_rendered_and_javascript_stays_enhancement_only() -> None:
    assert "LOCAL CONTROL PLANE" not in DASHBOARD_JS
    assert "PROJECT KNOWLEDGE" not in DASHBOARD_JS
    assert "document.querySelectorAll('.mobile-navigation a')" in DASHBOARD_JS
    assert 'input[type="search"]' in DASHBOARD_JS
    assert ".innerHTML" not in DASHBOARD_JS


def test_dashboard_refresh_guard_preserves_any_modified_user_input() -> None:
    """A second editable field must not clear another field's dirty state."""
    assert "const fieldHasChanged = (field) =>" in DASHBOARD_JS
    assert "option.selected !== option.defaultSelected" in DASHBOARD_JS
    assert "const hasUnsavedInput = () => Array.from(" in DASHBOARD_JS
    assert (
        "document.querySelectorAll('textarea, input:not([type=\"hidden\"]), select')"
        in DASHBOARD_JS
    )
    assert ".some(fieldHasChanged);" in DASHBOARD_JS
    assert "if (hasUnsavedInput())" in DASHBOARD_JS
