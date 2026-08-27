from __future__ import annotations

from harness.dashboard_i18n import (
    event_count_label,
    omitted_events_label,
    ru_plural,
    workspace_count_label,
)


def test_russian_plural_forms() -> None:
    assert ru_plural(1, "копия", "копии", "копий") == "копия"
    assert ru_plural(2, "копия", "копии", "копий") == "копии"
    assert ru_plural(4, "копия", "копии", "копий") == "копии"
    assert ru_plural(5, "копия", "копии", "копий") == "копий"
    assert ru_plural(11, "копия", "копии", "копий") == "копий"
    assert ru_plural(21, "копия", "копии", "копий") == "копия"


def test_count_labels() -> None:
    assert workspace_count_label(1) == "1 рабочая копия"
    assert workspace_count_label(3) == "3 рабочие копии"
    assert workspace_count_label(12) == "12 рабочих копий"
    assert event_count_label(1) == "1 событие"
    assert omitted_events_label(2) == "Ещё 2 события скрыты"
