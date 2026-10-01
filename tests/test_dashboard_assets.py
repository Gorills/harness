from __future__ import annotations

from harness.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS


def test_dashboard_has_a_light_workspace_and_responsive_work_surfaces() -> None:
    assert "color-scheme: light;" in DASHBOARD_CSS
    assert "--bg: #f4f6f7;" in DASHBOARD_CSS
    assert "--sidebar: #202a32;" in DASHBOARD_CSS
    assert ".portfolio-layout" in DASHBOARD_CSS
    assert ".directory-scroll" in DASHBOARD_CSS
    assert ".inbox-scroll" in DASHBOARD_CSS
    assert ".task-scopes" in DASHBOARD_CSS
    assert "@media (max-width: 760px)" in DASHBOARD_CSS
    assert "@media (prefers-reduced-motion: reduce)" in DASHBOARD_CSS


def test_dashboard_navigation_is_server_rendered_and_javascript_stays_enhancement_only() -> None:
    assert "LOCAL CONTROL PLANE" not in DASHBOARD_JS
    assert "PROJECT KNOWLEDGE" not in DASHBOARD_JS
    assert "document.querySelectorAll('.mobile-navigation a')" in DASHBOARD_JS
    assert 'input[type="search"]' in DASHBOARD_JS
    assert ".innerHTML" not in DASHBOARD_JS
    assert "location.reload" not in DASHBOARD_JS
    assert "DOMParser" in DASHBOARD_JS
    assert "parseFromString" in DASHBOARD_JS
    assert "currentLayout.replaceWith(nextLayout)" in DASHBOARD_JS


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
    assert "if (!force && hasUnsavedInput())" in DASHBOARD_JS
    assert "window.location.pathname" in DASHBOARD_JS
    assert "cache: 'no-store'" in DASHBOARD_JS
