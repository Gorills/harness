from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import harness.skills as skills_module
from harness.host_adapters import cursor_skill_projection_surface
from harness.skills import (
    SKILL_OWNERSHIP_MARKER_NAME,
    DetectedProjectStack,
    ResolvedSkill,
    SkillDefinition,
    SkillProjectionCollisionError,
    SkillProjectionError,
    SkillProjectionSurface,
    apply_skill_projection,
    load_skill_registry,
    plan_skill_projection,
    resolve_skills,
)


def _git_init(root: Path) -> None:
    root.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _surface() -> SkillProjectionSurface:
    return cursor_skill_projection_surface()


def _resolved_fastapi(registry: Path) -> tuple[ResolvedSkill, ...]:
    skill = registry / "fastapi"
    skill.mkdir(parents=True)
    registry.chmod(0o700)
    (skill / "SKILL.md").write_text(
        "---\nname: fastapi\ndescription: Apply FastAPI conventions.\n---\n\n# FastAPI\n",
        encoding="utf-8",
    )
    (skill / "harness.yaml").write_text(
        "id: fastapi\ntask_hints:\n  - fastapi\n",
        encoding="utf-8",
    )
    return resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        explicit_include=("fastapi",),
    )


def test_stale_cleanup_rechecks_ownership_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    resolved = _resolved_fastapi(tmp_path / "registry")
    apply_skill_projection(plan_skill_projection(root, resolved, (surface,)))
    target = root / ".agents" / "skills" / "fastapi"
    empty_plan = plan_skill_projection(root, (), (surface,))
    original_preflight = skills_module._preflight_projection_paths

    def replace_after_preflight(
        workspace_root: Path,
        desired: Mapping[PurePosixPath, SkillDefinition],
        existing_owned: set[PurePosixPath],
    ) -> None:
        original_preflight(workspace_root, desired, existing_owned)
        shutil.rmtree(target)
        target.mkdir()
        (target / "SKILL.md").write_text("# user replacement\n", encoding="utf-8")

    monkeypatch.setattr(skills_module, "_preflight_projection_paths", replace_after_preflight)

    with pytest.raises(SkillProjectionCollisionError, match="changed ownership before mutation"):
        apply_skill_projection(empty_plan)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# user replacement\n"
    assert not (target / SKILL_OWNERSHIP_MARKER_NAME).exists()


def test_stale_cleanup_rejects_marker_for_different_skill(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    target = root / ".agents" / "skills" / "fastapi"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# user content\n", encoding="utf-8")
    marker = {
        "version": 1,
        "skill_id": "different",
        "content_sha256": "0" * 64,
    }
    (target / SKILL_OWNERSHIP_MARKER_NAME).write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillProjectionCollisionError, match="changed ownership before mutation"):
        apply_skill_projection(plan_skill_projection(root, (), (surface,)))

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# user content\n"
    assert (target / SKILL_OWNERSHIP_MARKER_NAME).is_file()


def test_stale_cleanup_rechecks_target_at_atomic_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    resolved = _resolved_fastapi(tmp_path / "registry")
    apply_skill_projection(plan_skill_projection(root, resolved, (surface,)))
    target = root / ".agents" / "skills" / "fastapi"
    empty_plan = plan_skill_projection(root, (), (surface,))
    original_replace = os.replace
    raced = False

    def replace_with_race(source: Path | str, destination: Path | str) -> None:
        nonlocal raced
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not raced
            and source_path == target
            and destination_path.name.startswith(".harness-backup-fastapi-")
        ):
            shutil.rmtree(target)
            target.mkdir()
            (target / "SKILL.md").write_text("# user at rename\n", encoding="utf-8")
            raced = True
        original_replace(source, destination)

    monkeypatch.setattr("harness.skills.os.replace", replace_with_race)

    with pytest.raises(SkillProjectionCollisionError, match="changed during mutation"):
        apply_skill_projection(empty_plan)

    assert raced is True
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# user at rename\n"
    assert not (target / SKILL_OWNERSHIP_MARKER_NAME).exists()


def test_skill_update_rechecks_ownership_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    registry = tmp_path / "registry"
    resolved = _resolved_fastapi(registry)
    apply_skill_projection(plan_skill_projection(root, resolved, (surface,)))
    target = root / ".agents" / "skills" / "fastapi"

    (registry / "fastapi" / "SKILL.md").write_text(
        "---\nname: fastapi\ndescription: Apply FastAPI conventions.\n---\n\n# FastAPI v2\n",
        encoding="utf-8",
    )
    updated = resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        explicit_include=("fastapi",),
    )
    update_plan = plan_skill_projection(root, updated, (surface,))
    original_preflight = skills_module._preflight_projection_paths

    def replace_after_preflight(
        workspace_root: Path,
        desired: Mapping[PurePosixPath, SkillDefinition],
        existing_owned: set[PurePosixPath],
    ) -> None:
        original_preflight(workspace_root, desired, existing_owned)
        shutil.rmtree(target)
        target.mkdir()
        (target / "SKILL.md").write_text("# user replacement\n", encoding="utf-8")

    monkeypatch.setattr(skills_module, "_preflight_projection_paths", replace_after_preflight)

    with pytest.raises(SkillProjectionCollisionError, match="changed ownership before mutation"):
        apply_skill_projection(update_plan)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# user replacement\n"
    assert not (target / SKILL_OWNERSHIP_MARKER_NAME).exists()


def test_rollback_validates_committed_projection_after_atomic_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    registry = tmp_path / "registry"
    resolved = _resolved_fastapi(registry)
    apply_skill_projection(plan_skill_projection(root, resolved, (surface,)))
    target = root / ".agents" / "skills" / "fastapi"

    (registry / "fastapi" / "SKILL.md").write_text(
        "---\nname: fastapi\ndescription: Apply FastAPI conventions.\n---\n\n# FastAPI v2\n",
        encoding="utf-8",
    )
    updated = resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        explicit_include=("fastapi",),
    )
    update_plan = plan_skill_projection(root, updated, (surface,))
    original_commit = skills_module._commit_projection_changes
    original_replace = os.replace
    original_rmtree = shutil.rmtree
    raced = False

    def install_user_replacement() -> None:
        nonlocal raced
        if raced:
            return
        original_rmtree(target)
        target.mkdir()
        (target / "USER.txt").write_text("do not delete\n", encoding="utf-8")
        raced = True

    def fail_after_commit(
        workspace_root: Path,
        prepared: list[skills_module._PreparedProjection],
    ) -> None:
        original_commit(workspace_root, prepared)
        raise SkillProjectionError("forced post-commit failure")

    def replace_with_rollback_race(source: Path | str, destination: Path | str) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not raced
            and source_path == target
            and destination_path.name.startswith(".harness-fastapi-")
        ):
            install_user_replacement()
        original_replace(source, destination)

    def rmtree_with_rollback_race(path: Path | str, *args: Any, **kwargs: Any) -> None:
        if not raced and Path(path) == target:
            install_user_replacement()
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills_module, "_commit_projection_changes", fail_after_commit)
    monkeypatch.setattr("harness.skills.os.replace", replace_with_rollback_race)
    monkeypatch.setattr("harness.skills.shutil.rmtree", rmtree_with_rollback_race)

    with pytest.raises(SkillProjectionError):
        apply_skill_projection(update_plan)

    assert raced is True
    assert (target / "USER.txt").read_text(encoding="utf-8") == "do not delete\n"
    assert not (target / SKILL_OWNERSHIP_MARKER_NAME).exists()


def test_stale_cleanup_rejects_dangling_symlink_created_after_state_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    resolved = _resolved_fastapi(tmp_path / "registry")
    apply_skill_projection(plan_skill_projection(root, resolved, (surface,)))
    target = root / ".agents" / "skills" / "fastapi"
    empty_plan = plan_skill_projection(root, (), (surface,))
    original_state = skills_module._projection_state_sha256
    target_state_reads = 0

    def replace_after_state_check(path: Path) -> str | None:
        nonlocal target_state_reads
        state = original_state(path)
        if path == target:
            target_state_reads += 1
            if target_state_reads == 2:
                shutil.rmtree(target)
                target.symlink_to(root / "missing-user-target", target_is_directory=True)
        return state

    monkeypatch.setattr(skills_module, "_projection_state_sha256", replace_after_state_check)

    with pytest.raises(SkillProjectionCollisionError, match="not a real directory"):
        apply_skill_projection(empty_plan)

    assert target.is_symlink()
    assert target.readlink() == root / "missing-user-target"


def test_uncommitted_restore_does_not_overwrite_dangling_symlink(tmp_path: Path) -> None:
    target = tmp_path / "fastapi"
    backup = tmp_path / ".harness-backup-fastapi"
    target.symlink_to(tmp_path / "concurrent-user-target", target_is_directory=True)
    backup.symlink_to(tmp_path / "moved-user-target", target_is_directory=True)
    item = skills_module._PreparedProjection(
        target=target,
        replacement=None,
        expected_state_sha256=None,
        committed_state_sha256=None,
        backup=backup,
    )

    with pytest.raises(SkillProjectionError, match="moved content could not be restored"):
        skills_module._restore_uncommitted_projection_backup(tmp_path, item)

    assert target.is_symlink()
    assert target.readlink() == tmp_path / "concurrent-user-target"
    assert backup.is_symlink()
    assert backup.readlink() == tmp_path / "moved-user-target"


def test_skill_creation_does_not_overwrite_target_created_after_existence_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _git_init(root)
    surface = _surface()
    resolved = _resolved_fastapi(tmp_path / "registry")
    target = root / ".agents" / "skills" / "fastapi"
    original_exists = skills_module._projection_entry_exists
    raced = False

    def create_target_after_check(path: Path) -> bool:
        nonlocal raced
        exists = original_exists(path)
        if path == target and not exists and not raced:
            target.mkdir(parents=True)
            raced = True
        return exists

    monkeypatch.setattr(skills_module, "_projection_entry_exists", create_target_after_check)

    with pytest.raises(SkillProjectionCollisionError, match="appeared before materialization"):
        apply_skill_projection(plan_skill_projection(root, resolved, (surface,)))

    assert raced is True
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_uncommitted_restore_does_not_overwrite_target_created_after_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "fastapi"
    backup = tmp_path / ".harness-backup-fastapi"
    backup.mkdir()
    (backup / "MOVED.txt").write_text("preserve\n", encoding="utf-8")
    item = skills_module._PreparedProjection(
        target=target,
        replacement=None,
        expected_state_sha256=None,
        committed_state_sha256=None,
        backup=backup,
    )
    original_exists = skills_module._projection_entry_exists
    raced = False

    def create_target_after_check(path: Path) -> bool:
        nonlocal raced
        exists = original_exists(path)
        if path == target and not exists and not raced:
            target.mkdir()
            raced = True
        return exists

    monkeypatch.setattr(skills_module, "_projection_entry_exists", create_target_after_check)

    with pytest.raises(SkillProjectionError, match="moved content could not be restored"):
        skills_module._restore_uncommitted_projection_backup(tmp_path, item)

    assert raced is True
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert (backup / "MOVED.txt").read_text(encoding="utf-8") == "preserve\n"


def test_rollback_restore_does_not_overwrite_target_created_after_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "fastapi"
    staging = tmp_path / ".harness-fastapi-staging"
    staging.mkdir()
    (staging / "MOVED.txt").write_text("preserve\n", encoding="utf-8")
    item = skills_module._PreparedProjection(
        target=target,
        replacement=staging,
        expected_state_sha256=None,
        committed_state_sha256="0" * 64,
    )
    original_exists = skills_module._projection_entry_exists
    raced = False

    def create_target_after_check(path: Path) -> bool:
        nonlocal raced
        exists = original_exists(path)
        if path == target and not exists and not raced:
            target.mkdir()
            raced = True
        return exists

    monkeypatch.setattr(skills_module, "_projection_entry_exists", create_target_after_check)

    with pytest.raises(SkillProjectionError, match="preserved at"):
        skills_module._restore_rollback_candidate(tmp_path, item)

    assert raced is True
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert (staging / "MOVED.txt").read_text(encoding="utf-8") == "preserve\n"
    assert item.replacement is None
