from pathlib import Path

import pytest

from harness.workspace_resolution import (
    AmbiguousWorkspaceError,
    WorkspaceCandidate,
    WorkspaceHint,
    WorkspaceNotFoundError,
    WorkspaceResolver,
)


def test_resolves_exact_registered_workspace(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    resolver = WorkspaceResolver([WorkspaceCandidate("workspace-1", root)])

    resolution = resolver.resolve([WorkspaceHint(root, "explicit-root")])

    assert resolution.workspace_id == "workspace-1"
    assert resolution.workspace_root == root.resolve()
    assert resolution.hint_source == "explicit-root"
    assert resolution.matched_path == root.resolve()


def test_stronger_unmatched_hint_fails_instead_of_falling_through(tmp_path: Path) -> None:
    registered = tmp_path / "registered"
    fallback = tmp_path / "fallback"
    registered.mkdir()
    fallback.mkdir()
    resolver = WorkspaceResolver(
        [
            WorkspaceCandidate("registered", registered),
            WorkspaceCandidate("fallback", fallback),
        ]
    )

    with pytest.raises(WorkspaceNotFoundError, match="strong-explicit-root"):
        resolver.resolve(
            [
                WorkspaceHint(tmp_path / "unknown", "strong-explicit-root"),
                WorkspaceHint(fallback, "cwd"),
            ]
        )


def test_first_matching_hint_wins_over_weaker_hint(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    resolver = WorkspaceResolver(
        [
            WorkspaceCandidate("first", first),
            WorkspaceCandidate("second", second),
        ]
    )

    resolution = resolver.resolve(
        [
            WorkspaceHint(first, "explicit-root"),
            WorkspaceHint(second, "cwd"),
        ]
    )

    assert resolution.workspace_id == "first"
    assert resolution.hint_source == "explicit-root"


def test_nested_workspace_is_more_specific_for_descendant_hint(tmp_path: Path) -> None:
    outer = tmp_path / "repo"
    nested = outer / "nested"
    cwd = nested / "src"
    cwd.mkdir(parents=True)
    resolver = WorkspaceResolver(
        [
            WorkspaceCandidate("outer", outer),
            WorkspaceCandidate("nested", nested),
        ]
    )

    resolution = resolver.resolve([WorkspaceHint(cwd, "cwd")])

    assert resolution.workspace_id == "nested"
    assert resolution.workspace_root == nested.resolve()


def test_normalizes_parent_segments_before_matching(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    child = root / "src"
    child.mkdir(parents=True)
    resolver = WorkspaceResolver([WorkspaceCandidate("workspace-1", root)])

    resolution = resolver.resolve([WorkspaceHint(child / ".." / "src", "cwd")])

    assert resolution.matched_path == child.resolve()


def test_ambiguous_equal_roots_fail_instead_of_falling_through(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    fallback = tmp_path / "fallback"
    root.mkdir()
    fallback.mkdir()
    resolver = WorkspaceResolver(
        [
            WorkspaceCandidate("workspace-a", root),
            WorkspaceCandidate("workspace-b", root),
            WorkspaceCandidate("fallback", fallback),
        ]
    )

    with pytest.raises(AmbiguousWorkspaceError, match="workspace-a, workspace-b"):
        resolver.resolve(
            [
                WorkspaceHint(root, "explicit-root"),
                WorkspaceHint(fallback, "cwd"),
            ]
        )


def test_unmatched_hint_error_reports_source_and_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    resolver = WorkspaceResolver([WorkspaceCandidate("workspace-1", root)])
    unknown = tmp_path / "unknown"

    with pytest.raises(WorkspaceNotFoundError) as exc_info:
        resolver.resolve([WorkspaceHint(unknown, "explicit-root")])

    message = str(exc_info.value)
    assert "explicit-root" in message
    assert str(unknown.resolve()) in message


def test_rejects_resolution_without_hints(tmp_path: Path) -> None:
    resolver = WorkspaceResolver([WorkspaceCandidate("workspace-1", tmp_path)])

    with pytest.raises(WorkspaceNotFoundError, match="no workspace hints"):
        resolver.resolve([])
