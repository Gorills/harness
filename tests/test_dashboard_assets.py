from __future__ import annotations

from harness.dashboard_assets import DASHBOARD_CSS, DASHBOARD_JS


def test_dashboard_headings_stay_document_scale() -> None:
    assert "font-size: clamp(42px" not in DASHBOARD_CSS
    assert "font-size: clamp(36px" not in DASHBOARD_CSS
    assert "max-width: 16ch" not in DASHBOARD_CSS
    assert "line-height: .98" not in DASHBOARD_CSS
    assert ".hero h1.task-title" in DASHBOARD_CSS
    assert "font-size: clamp(24px, 2.2vw, 30px)" in DASHBOARD_CSS
    assert "font-size: clamp(22px, 2vw, 28px)" in DASHBOARD_CSS
    assert "font-size: clamp(20px, 1.8vw, 26px)" in DASHBOARD_CSS
    assert ".section-title { margin: 0; font-size: 18px; line-height: 1.25; }" in DASHBOARD_CSS


def test_dashboard_refresh_guard_preserves_any_modified_user_input() -> None:
    """A second editable field must not clear another field's dirty state."""
    assert "const hasUnsavedInput = () => Array.from(" in DASHBOARD_JS
    assert "document.querySelectorAll('textarea, input[type=\"search\"]')" in DASHBOARD_JS
    assert ".some((field) => field.value !== field.defaultValue);" in DASHBOARD_JS
    assert "if (hasUnsavedInput())" in DASHBOARD_JS
    assert "let hasUnsavedInput" not in DASHBOARD_JS
    assert "document.addEventListener('input'" not in DASHBOARD_JS
    assert "document.addEventListener('submit'" not in DASHBOARD_JS
