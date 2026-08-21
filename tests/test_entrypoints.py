import sys

import pytest

from harness.entrypoints import harness_main, harnessd_main


def test_harness_main_reports_bootstrap_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harness"])

    assert harness_main() == 0
    assert "Product commands are not implemented yet." in capsys.readouterr().out


def test_harnessd_main_reports_bootstrap_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["harnessd"])

    assert harnessd_main() == 0
    assert "Harness daemon runtime is not implemented yet." in capsys.readouterr().out
