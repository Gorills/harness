from __future__ import annotations

from harness.dashboard_assets import DASHBOARD_JS


def test_dashboard_refresh_guard_preserves_any_modified_user_input() -> None:
    """A second editable field must not clear another field's dirty state."""
    assert "const hasUnsavedInput = () => Array.from(" in DASHBOARD_JS
    assert "document.querySelectorAll('textarea, input[type=\"search\"]')" in DASHBOARD_JS
    assert ".some((field) => field.value !== field.defaultValue);" in DASHBOARD_JS
    assert "if (hasUnsavedInput())" in DASHBOARD_JS
    assert "let hasUnsavedInput" not in DASHBOARD_JS
    assert "document.addEventListener('input'" not in DASHBOARD_JS
    assert "document.addEventListener('submit'" not in DASHBOARD_JS
