from __future__ import annotations

import sys
from pathlib import Path

import pytest

import harness.runtime_identity as runtime_identity
from harness.runtime_identity import RuntimeIdentityError, current_runtime_identity


def test_runtime_identity_fingerprint_changes_with_installed_code_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "harness"
    package.mkdir()
    marker = package / "runtime_identity.py"
    marker.write_text("VALUE = 1\n", encoding="utf-8")
    other = package / "other.py"
    other.write_text("OTHER = 1\n", encoding="utf-8")
    monkeypatch.setattr(runtime_identity, "__file__", str(marker))
    monkeypatch.setattr(runtime_identity, "distribution_version", lambda _name: "1.2.3")
    monkeypatch.setattr(sys, "executable", "/tool/python")

    first = current_runtime_identity()
    other.write_text("OTHER = 2\n", encoding="utf-8")
    second = current_runtime_identity()

    assert first.package_version == "1.2.3"
    assert first.python_executable == "/tool/python"
    assert len(first.code_sha256) == 64
    assert first.code_sha256 != second.code_sha256


def test_runtime_identity_refuses_symlinked_python_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "harness"
    package.mkdir()
    marker = package / "runtime_identity.py"
    marker.write_text("VALUE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = 1\n", encoding="utf-8")
    (package / "linked.py").symlink_to(outside)
    monkeypatch.setattr(runtime_identity, "__file__", str(marker))
    monkeypatch.setattr(runtime_identity, "distribution_version", lambda _name: "1.2.3")

    with pytest.raises(RuntimeIdentityError, match="unsafe Python source entry"):
        current_runtime_identity()


def test_runtime_identity_refuses_symlinked_package_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "real-harness"
    package.mkdir()
    marker = package / "runtime_identity.py"
    marker.write_text("VALUE = 1\n", encoding="utf-8")
    linked = tmp_path / "linked-harness"
    linked.symlink_to(package, target_is_directory=True)
    monkeypatch.setattr(runtime_identity, "__file__", str(linked / "runtime_identity.py"))

    with pytest.raises(RuntimeIdentityError, match="package root is unsafe"):
        current_runtime_identity()


def test_runtime_identity_refuses_source_set_change_during_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "harness"
    package.mkdir()
    marker = package / "runtime_identity.py"
    marker.write_text("VALUE = 1\n", encoding="utf-8")
    added = package / "added.py"
    added.write_text("ADDED = 1\n", encoding="utf-8")
    monkeypatch.setattr(runtime_identity, "__file__", str(marker))
    monkeypatch.setattr(runtime_identity, "distribution_version", lambda _name: "1.2.3")
    calls = 0

    def changing_source_set(_root: Path) -> tuple[Path, ...]:
        nonlocal calls
        calls += 1
        return (marker,) if calls == 1 else (added, marker)

    monkeypatch.setattr(runtime_identity, "_runtime_source_files", changing_source_set)

    with pytest.raises(RuntimeIdentityError, match="changed during fingerprinting"):
        current_runtime_identity()
