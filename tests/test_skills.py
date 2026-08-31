from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from time import monotonic

import pytest

import harness.skills as skills_module
from harness.host_adapters import (
    codex_skill_projection_surface,
    cursor_skill_projection_surface,
)
from harness.index import scan_workspace
from harness.registry import create_project, register_workspace
from harness.skills import (
    SKILL_METADATA_FILE_NAME,
    SKILL_OWNERSHIP_MARKER_NAME,
    DetectedProjectStack,
    ResolvedSkill,
    SkillProjectionCollisionError,
    SkillProjectionError,
    SkillRegistryError,
    SkillResolutionError,
    SkillResolutionPolicy,
    apply_skill_projection,
    default_skill_registry,
    detect_workspace_stack,
    inspect_skill_projection,
    load_skill_registry,
    plan_skill_projection,
    resolve_skills,
    resolve_workspace_skills,
)
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_start
from harness.tasks import get_task_stack_hints


def _git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=check,
        capture_output=True,
    )


def _make_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir()
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(path, "init", "-b", "main")
    _git(path, "add", ".")
    _git(
        path,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )


def _registered_workspace(
    tmp_path: Path,
    files: dict[str, str],
) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    _make_repo(root, files)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id


def _write_skill(
    registry: Path,
    skill_id: str,
    *,
    languages: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
    manifests: tuple[str, ...] = (),
    facets: tuple[str, ...] = (),
    task_hints: tuple[str, ...] = (),
    skill_text: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    directory = registry / skill_id
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        skill_text
        or (
            f"---\nname: {skill_id}\ndescription: Portable {skill_id} instructions.\n"
            f"---\n\n# {skill_id}\n\nPortable instructions.\n"
        ),
        encoding="utf-8",
    )
    lines = [f"id: {skill_id}"]
    if languages or dependencies or manifests or facets:
        lines.append("applies:")
        for key, values in (
            ("languages", languages),
            ("dependencies", dependencies),
            ("manifests", manifests),
            ("facets", facets),
        ):
            if values:
                lines.append(f"  {key}:")
                lines.extend(f"    - {value}" for value in values)
    if task_hints:
        lines.append("task_hints:")
        lines.extend(f"  - {value}" for value in task_hints)
    (directory / SKILL_METADATA_FILE_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    for relative, content in (extra_files or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


def _ids(resolved: Sequence[ResolvedSkill]) -> tuple[str, ...]:
    return tuple(item.definition.skill_id for item in resolved)


def test_default_registry_and_strict_metadata_loading(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert default_skill_registry(home=home) == home / ".harness" / "skills"
    assert (
        default_skill_registry(
            home=home,
            environment={"HARNESS_SKILL_REGISTRY": str(tmp_path / "skills")},
        )
        == tmp_path / "skills"
    )
    assert (
        default_skill_registry(
            home=home,
            environment={"HARNESS_DEV_ROOT": str(tmp_path / "checkout")},
        )
        == tmp_path / "checkout" / ".harness" / "skills"
    )
    assert load_skill_registry(home / "missing") == ()

    registry = home / ".harness" / "skills"
    _write_skill(
        registry,
        "fastapi",
        languages=("python",),
        dependencies=("fastapi",),
        manifests=("pyproject.toml",),
        facets=("backend-service",),
        task_hints=("fastapi", "python-api"),
        extra_files={"references/notes.md": "details\n"},
    )

    definitions = load_skill_registry(registry)

    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.skill_id == "fastapi"
    assert definition.applies.languages == ("python",)
    assert definition.applies.dependencies == ("fastapi",)
    assert definition.applies.manifests == ("pyproject.toml",)
    assert definition.applies.facets == ("backend-service",)
    assert definition.task_hints == ("fastapi", "python-api")
    assert definition.portable_files == (
        PurePosixPath("SKILL.md"),
        PurePosixPath("references/notes.md"),
    )


def test_registry_rejects_symlinks_and_unknown_metadata(tmp_path: Path) -> None:
    registry = tmp_path / "skills"
    skill = _write_skill(registry, "fastapi")
    (skill / "unsafe").symlink_to(skill / "SKILL.md")
    with pytest.raises(SkillRegistryError, match="unsafe file entry"):
        load_skill_registry(registry)

    (skill / "unsafe").unlink()
    (skill / SKILL_METADATA_FILE_NAME).write_text(
        "id: fastapi\nunknown: value\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillRegistryError, match="unsupported skill metadata field"):
        load_skill_registry(registry)


def test_resolver_selects_only_relevant_legacy_stack(tmp_path: Path) -> None:
    package = {
        "dependencies": {"next": "15.0.0", "pg": "8.0.0"},
        "devDependencies": {"@playwright/test": "1.0.0"},
    }
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "package.json": json.dumps(package),
            "src/app.tsx": "export default function App() { return null }\n",
        },
    )
    registry = tmp_path / "registry"
    _write_skill(registry, "nextjs", dependencies=("next",))
    _write_skill(registry, "postgres", dependencies=("pg",))
    _write_skill(registry, "playwright", dependencies=("@playwright/test",))
    _write_skill(registry, "godot", task_hints=("godot",))
    _write_skill(registry, "unity", task_hints=("unity",))
    _write_skill(registry, "fastapi", dependencies=("fastapi",))
    try:
        stack = detect_workspace_stack(connection, workspace_id)
        resolved = resolve_workspace_skills(connection, workspace_id, load_skill_registry(registry))
    finally:
        connection.close()

    assert stack.languages == frozenset({"typescript"})
    assert {"next", "pg", "@playwright/test"} <= stack.dependencies
    assert _ids(resolved) == ("nextjs", "playwright", "postgres")


def test_stack_detection_recognizes_static_frontend_sources(tmp_path: Path) -> None:
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "index.html": "<main>Home</main>\n",
            "styles/site.scss": "main { display: block; }\n",
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()

    assert stack.languages == frozenset({"css", "html"})
    assert {"software-project", "web-frontend"} <= stack.facets


def test_stack_detection_classifies_expo_as_mobile_not_web(tmp_path: Path) -> None:
    package = {
        "dependencies": {
            "expo": "55.0.0",
            "react": "19.0.0",
            "react-dom": "19.0.0",
            "react-native": "0.83.0",
            "react-native-web": "0.22.0",
        }
    }
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "mobile/package.json": json.dumps(package),
            "mobile/src/global.css": "body { color: black; }\n",
            "mobile/stitch_design/code.html": "<main>design export</main>\n",
            "mobile/src/app.tsx": "export default function App() { return null }\n",
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()

    assert {"expo", "react-dom", "react-native"} <= stack.dependencies
    assert {"mobile-app", "software-project"} <= stack.facets
    assert "web-frontend" not in stack.facets


def test_stack_detection_keeps_web_and_mobile_facets_for_real_monorepo(
    tmp_path: Path,
) -> None:
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "apps/mobile/package.json": json.dumps(
                {"dependencies": {"expo": "55", "react-native": "0.83", "react-dom": "19"}}
            ),
            "apps/site/package.json": json.dumps(
                {"dependencies": {"next": "16", "react": "19", "react-dom": "19"}}
            ),
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()

    assert {"mobile-app", "software-project", "web-frontend"} <= stack.facets


def test_stack_detection_reads_composer_and_detects_laravel_backend(tmp_path: Path) -> None:
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "composer.json": json.dumps(
                {
                    "require": {
                        "php": "^8.4",
                        "laravel/framework": "^12",
                    },
                    "require-dev": {"phpunit/phpunit": "^12"},
                }
            ),
            "app/Http/Controllers/HomeController.php": "<?php\n",
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()

    assert {"laravel/framework", "phpunit/phpunit"} <= stack.dependencies
    assert {"backend-service", "database-backed", "software-project"} <= stack.facets


def test_stack_detection_uses_normalized_go_modules_for_backend_facets(tmp_path: Path) -> None:
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "go.mod": """\
module example.invalid/service

go 1.25

require (
    github.com/go-chi/chi/v5 v5.2.2
    github.com/jackc/pgx/v5 v5.9.2
)
""",
            "cmd/api/main.go": "package main\n",
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()

    assert {
        "github-com/go-chi/chi/v5",
        "github-com/jackc/pgx/v5",
    } <= stack.dependencies
    assert {"backend-service", "database-backed", "software-project"} <= stack.facets


def test_stack_detection_recognizes_godot_shell_ci_and_deployment_facets(
    tmp_path: Path,
) -> None:
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {
            "project.godot": '[application]\nconfig/name="Game"\n',
            "src/player.gd": "extends Node\n",
            "scripts/export.sh": "#!/bin/sh\nexit 0\n",
            ".github/workflows/ci.yml": "name: ci\n",
            "deploy/nginx.conf": "events {}\n",
            "deploy/game.service": "[Service]\nExecStart=/srv/game\n",
        },
    )
    try:
        stack = detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()

    assert {"gdscript", "shell"} <= stack.languages
    assert {
        "ci-pipeline",
        "deployment-ops",
        "godot-project",
        "software-project",
    } <= stack.facets


def test_greenfield_task_hints_activate_skills_before_manifest_exists(tmp_path: Path) -> None:
    _, connection, workspace_id = _registered_workspace(tmp_path, {"README.md": "greenfield\n"})
    registry = tmp_path / "registry"
    _write_skill(registry, "fastapi", task_hints=("fastapi",))
    _write_skill(registry, "postgres", task_hints=("postgres",))
    _write_skill(registry, "godot", task_hints=("godot",))
    try:
        task = task_start(
            connection,
            workspace_id,
            "Create API",
            stack_hints=(" FastAPI ", "POSTGRES"),
        )
        resolved = resolve_workspace_skills(connection, workspace_id, load_skill_registry(registry))
        hints = get_task_stack_hints(connection, task.task_id)
    finally:
        connection.close()

    assert hints == ("fastapi", "postgres")
    assert _ids(resolved) == ("fastapi", "postgres")


def test_recognized_task_hints_suppress_unrelated_stack_only_skills(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    _write_skill(
        registry,
        "mobile",
        facets=("mobile-app",),
        task_hints=("expo", "android"),
    )
    _write_skill(
        registry,
        "server",
        facets=("backend-service",),
        task_hints=("fastapi",),
    )
    _write_skill(registry, "testing", facets=("software-project",))
    definitions = load_skill_registry(registry)
    stack = DetectedProjectStack(
        languages=frozenset({"python", "typescript"}),
        dependencies=frozenset({"expo", "fastapi"}),
        manifests=frozenset({"package.json", "pyproject.toml"}),
        facets=frozenset({"backend-service", "mobile-app", "software-project"}),
    )

    assert _ids(resolve_skills(definitions, stack)) == ("mobile", "server", "testing")
    assert _ids(resolve_skills(definitions, stack, task_hints=("expo",))) == ("mobile",)
    assert _ids(
        resolve_skills(
            definitions,
            stack,
            task_hints=("expo",),
            explicit_include=("server",),
        )
    ) == ("server", "mobile")

    # Novel hints must not accidentally remove the applicable project baseline.
    assert _ids(resolve_skills(definitions, stack, task_hints=("apk-signing",))) == (
        "mobile",
        "server",
        "testing",
    )


def test_manifest_detection_fails_closed_when_index_is_stale(tmp_path: Path) -> None:
    root, connection, workspace_id = _registered_workspace(
        tmp_path,
        {"package.json": json.dumps({"dependencies": {"next": "1"}})},
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"fastapi": "not-real"}}),
        encoding="utf-8",
    )
    try:
        with pytest.raises(SkillResolutionError, match="Structural Index is stale"):
            detect_workspace_stack(connection, workspace_id)
    finally:
        connection.close()


def test_workspace_stack_resolution_honors_expired_deadline(tmp_path: Path) -> None:
    _, connection, workspace_id = _registered_workspace(
        tmp_path,
        {"package.json": json.dumps({"dependencies": {"next": "1"}})},
    )
    try:
        with pytest.raises(SkillResolutionError, match="deadline exceeded"):
            detect_workspace_stack(connection, workspace_id, deadline=monotonic() - 1.0)
    finally:
        connection.close()


def test_resolver_budget_is_bounded_deterministic_and_explicit_wins(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    for skill_id in ("alpha", "beta", "gamma"):
        _write_skill(registry, skill_id, languages=("python",))
    definitions = load_skill_registry(registry)
    stack = DetectedProjectStack(
        languages=frozenset({"python"}),
        dependencies=frozenset(),
        manifests=frozenset(),
    )

    resolved = resolve_skills(
        definitions,
        stack,
        explicit_include=("gamma",),
        policy=SkillResolutionPolicy(max_visible_skills=2),
    )

    assert _ids(resolved) == ("gamma", "alpha")
    with pytest.raises(SkillResolutionError, match="both explicitly included and excluded"):
        resolve_skills(
            definitions,
            stack,
            explicit_include=("alpha",),
            explicit_exclude=("alpha",),
        )


def test_projection_planner_shares_agents_root_for_codex_and_cursor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    registry = tmp_path / "registry"
    _write_skill(registry, "shared", task_hints=("shared",))
    definition = load_skill_registry(registry)[0]
    resolved = resolve_skills(
        (definition,),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("shared",),
    )
    cursor = cursor_skill_projection_surface()
    codex = codex_skill_projection_surface()

    plan = plan_skill_projection(root, resolved, (codex, cursor))

    assert tuple(target.relative_root for target in plan.targets) == (
        PurePosixPath(".agents/skills"),
    )
    assert PurePosixPath(".claude/skills") in cursor.visible_roots


def test_projection_reconciles_leftover_claude_skills_visible_to_cursor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    registry = tmp_path / "registry"
    _write_skill(registry, "shared", task_hints=("shared",))
    resolved = resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("shared",),
    )
    cursor = cursor_skill_projection_surface()

    leftover = root / ".claude" / "skills" / "shared"
    leftover.mkdir(parents=True)
    (leftover / "SKILL.md").write_text("# leftover\n", encoding="utf-8")
    (leftover / SKILL_OWNERSHIP_MARKER_NAME).write_text(
        json.dumps(
            {"content_sha256": "0" * 64, "skill_id": "shared", "version": 1},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    changed = apply_skill_projection(plan_skill_projection(root, resolved, (cursor,)))

    assert changed.materialized == 1
    assert changed.removed == 1
    assert not leftover.exists()
    assert (root / ".agents" / "skills" / "shared").is_dir()

    apply_skill_projection(plan_skill_projection(root, (), (cursor,)))
    user_duplicate = root / ".claude" / "skills" / "shared"
    user_duplicate.mkdir(parents=True)
    (user_duplicate / "SKILL.md").write_text("# user duplicate\n", encoding="utf-8")
    with pytest.raises(SkillProjectionCollisionError, match="duplicate Harness projection"):
        apply_skill_projection(plan_skill_projection(root, resolved, (cursor,)))
    assert (user_duplicate / "SKILL.md").read_text(encoding="utf-8") == "# user duplicate\n"
    assert not (root / ".agents" / "skills" / "shared").exists()


def test_projection_inspection_honors_expired_deadline_before_git_or_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    surface = cursor_skill_projection_surface()
    plan = plan_skill_projection(root, (), (surface,))

    with pytest.raises(SkillProjectionError, match="inspection deadline exceeded"):
        inspect_skill_projection(plan, deadline=0.0)

    assert not (root / ".agents").exists()


def test_projection_is_idempotent_owned_only_and_git_local(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {".gitignore": "user-cache", "README.md": "repo\n"})
    original_gitignore = (root / ".gitignore").read_bytes()
    exclude_path = Path(
        _git(root, "rev-parse", "--git-path", "info/exclude").stdout.decode().strip()
    )
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    original_exclude = exclude_path.read_bytes()

    registry = tmp_path / "registry"
    _write_skill(
        registry,
        "fastapi",
        task_hints=("fastapi",),
        extra_files={"references/example.md": "example\n"},
    )
    definitions = load_skill_registry(registry)
    resolved = resolve_skills(
        definitions,
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )
    surface = cursor_skill_projection_surface()
    plan = plan_skill_projection(root, resolved, (surface,))

    first = apply_skill_projection(plan)
    second = apply_skill_projection(plan)

    target = root / ".agents" / "skills" / "fastapi"
    assert first.materialized == 1
    assert first.removed == 0
    assert first.exclude_changed is True
    assert second.materialized == 0
    assert second.unchanged == 1
    assert second.exclude_changed is False
    assert (target / "SKILL.md").is_file()
    assert (target / "references" / "example.md").is_file()
    assert not (target / SKILL_METADATA_FILE_NAME).exists()
    assert (target / SKILL_OWNERSHIP_MARKER_NAME).is_file()
    assert (root / ".gitignore").read_bytes() == original_gitignore
    assert _git(root, "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md").returncode == 0
    assert _git(root, "status", "--porcelain", "--untracked-files=all").stdout == b""

    user_skill = root / ".agents" / "skills" / "user-owned"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("# user\n", encoding="utf-8")
    empty_plan = plan_skill_projection(root, (), (surface,))
    removed = apply_skill_projection(empty_plan)

    assert removed.removed == 1
    assert not target.exists()
    assert (user_skill / "SKILL.md").read_text(encoding="utf-8") == "# user\n"
    assert exclude_path.read_bytes() == original_exclude
    assert (root / ".gitignore").read_bytes() == original_gitignore


def test_projection_rechecks_target_state_immediately_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    registry = tmp_path / "registry"
    _write_skill(registry, "fastapi", task_hints=("fastapi",))
    resolved = resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )
    surface = cursor_skill_projection_surface()
    plan = plan_skill_projection(root, resolved, (surface,))
    original_build = skills_module._build_projected_skill

    def race(parent: Path, definition: skills_module.SkillDefinition) -> Path:
        replacement = original_build(parent, definition)
        target = parent / definition.skill_id
        target.mkdir()
        (target / "SKILL.md").write_text("# late user\n", encoding="utf-8")
        return replacement

    monkeypatch.setattr(skills_module, "_build_projected_skill", race)

    with pytest.raises(SkillProjectionCollisionError, match="changed before mutation"):
        apply_skill_projection(plan)

    target = root / ".agents" / "skills" / "fastapi"
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# late user\n"
    assert not (target / SKILL_OWNERSHIP_MARKER_NAME).exists()


def test_projection_rollback_does_not_overwrite_concurrent_target_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    registry = tmp_path / "registry"
    _write_skill(registry, "fastapi", task_hints=("fastapi",))
    resolved = resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )
    surface = cursor_skill_projection_surface()
    plan = plan_skill_projection(root, resolved, (surface,))
    target = root / ".agents" / "skills" / "fastapi"

    def fail_after_concurrent_change(path: Path, original: bytes, updated: bytes) -> None:
        del path, original, updated
        shutil.rmtree(target)
        target.mkdir()
        (target / "SKILL.md").write_text("# concurrent user\n", encoding="utf-8")
        raise SkillProjectionError("forced exclude failure")

    monkeypatch.setattr(skills_module, "_replace_file_if_unchanged", fail_after_concurrent_change)

    with pytest.raises(SkillProjectionError, match="prior generated state could not be restored"):
        apply_skill_projection(plan)

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# concurrent user\n"
    assert not (target / SKILL_OWNERSHIP_MARKER_NAME).exists()


def test_projection_uses_git_path_info_exclude_for_linked_worktree(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _make_repo(primary, {"README.md": "repo\n"})
    worktree = tmp_path / "worktree"
    _git(primary, "worktree", "add", "-b", "feature", str(worktree))
    assert (worktree / ".git").is_file()

    registry = tmp_path / "registry"
    _write_skill(registry, "fastapi", task_hints=("fastapi",))
    resolved = resolve_skills(
        load_skill_registry(registry),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )
    surface = cursor_skill_projection_surface()
    plan = plan_skill_projection(worktree, resolved, (surface,))
    raw_exclude = _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.decode().strip()
    exclude_path = Path(raw_exclude)
    if not exclude_path.is_absolute():
        exclude_path = worktree / exclude_path
    original = exclude_path.read_bytes()

    apply_skill_projection(plan)

    assert _git(worktree, "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md").returncode == 0
    assert (worktree / ".git").is_file()
    apply_skill_projection(plan_skill_projection(worktree, (), (surface,)))
    assert exclude_path.read_bytes() == original


def test_projection_refuses_user_owned_or_tracked_target_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _make_repo(root, {"README.md": "repo\n"})
    registry = tmp_path / "registry"
    _write_skill(registry, "fastapi", task_hints=("fastapi",))
    definition = load_skill_registry(registry)[0]
    resolved = resolve_skills(
        (definition,),
        DetectedProjectStack(frozenset(), frozenset(), frozenset()),
        task_hints=("fastapi",),
    )
    surface = cursor_skill_projection_surface()
    plan = plan_skill_projection(root, resolved, (surface,))
    user_target = root / ".agents" / "skills" / "fastapi"
    user_target.mkdir(parents=True)
    (user_target / "SKILL.md").write_text("# user\n", encoding="utf-8")

    with pytest.raises(SkillProjectionCollisionError, match="user-owned"):
        apply_skill_projection(plan)
    assert (user_target / "SKILL.md").read_text(encoding="utf-8") == "# user\n"

    shutil.rmtree(root / ".agents")
    user_target.mkdir(parents=True)
    (user_target / "SKILL.md").write_text("# tracked\n", encoding="utf-8")
    _git(root, "add", "-f", ".agents/skills/fastapi/SKILL.md")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "tracked skill",
    )

    with pytest.raises(SkillProjectionCollisionError, match="tracked by Git"):
        apply_skill_projection(plan)
    assert (user_target / "SKILL.md").read_text(encoding="utf-8") == "# tracked\n"
