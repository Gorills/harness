import sys

import pytest

from harness.entrypoints import harness_main


def test_harness_search_command_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harness", "search", "needle"])
    with pytest.raises(SystemExit) as exc:
        harness_main()
    assert exc.value.code == 2
    assert "invalid choice: 'search'" in capsys.readouterr().err
