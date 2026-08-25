import os
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.1.0.dev0"


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(command)}", flush=True)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout, end="")
    result.check_returncode()
    return result


def _venv_scripts_dir(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def main() -> int:
    """Build a wheel, install it in isolation, and execute shipping console behavior."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for the wheel smoke test")

    with tempfile.TemporaryDirectory(prefix="harness-wheel-smoke-") as temp_dir:
        workspace = Path(temp_dir)
        dist = workspace / "dist"
        venv = workspace / "venv"

        _run(
            (
                uv,
                "build",
                "--wheel",
                "--no-sources",
                "--no-build-isolation",
                "--out-dir",
                str(dist),
            ),
            cwd=PROJECT_ROOT,
        )

        wheels = list(dist.glob("harness-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one Harness wheel, found {len(wheels)}")
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise RuntimeError("wheel must contain exactly one dist-info/METADATA file")
            metadata = archive.read(metadata_names[0]).decode("utf-8")
        if "Requires-Dist: mcp==2.0.0" not in metadata:
            raise RuntimeError(
                "wheel metadata does not pin the official MCP SDK runtime dependency"
            )

        _run((uv, "venv", "--python", "3.13", "--no-project", str(venv)), cwd=workspace)
        scripts_dir = _venv_scripts_dir(venv)
        python = scripts_dir / ("python.exe" if os.name == "nt" else "python")
        _run(
            (uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)),
            cwd=workspace,
        )

        isolated_env = os.environ.copy()
        isolated_env.pop("PYTHONPATH", None)
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("harness", "harnessd"):
            executable = scripts_dir / f"{name}{suffix}"
            result = _run((str(executable), "--version"), cwd=workspace, env=isolated_env)
            expected = f"{name} {EXPECTED_VERSION}\n"
            if result.stdout != expected:
                raise RuntimeError(
                    f"unexpected {name} --version output: {result.stdout!r}; expected {expected!r}"
                )

        harness = scripts_dir / f"harness{suffix}"
        harnessd = scripts_dir / f"harnessd{suffix}"
        help_result = _run((str(harness), "--help"), cwd=workspace, env=isolated_env)
        for expected in ("doctor", "status", "scan", "search", "mcp"):
            if expected not in help_result.stdout:
                raise RuntimeError(
                    f"installed harness --help did not contain {expected!r}: {help_result.stdout!r}"
                )

        status_help = _run((str(harness), "status", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--socket", "canonical per-user"):
            if expected not in status_help.stdout:
                raise RuntimeError(
                    f"installed harness status --help did not contain {expected!r}: "
                    f"{status_help.stdout!r}"
                )

        scan_help = _run((str(harness), "scan", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--socket", "deterministic", "Git Workspace"):
            if expected not in scan_help.stdout:
                raise RuntimeError(
                    f"installed harness scan --help did not contain {expected!r}: "
                    f"{scan_help.stdout!r}"
                )

        search_help = _run((str(harness), "search", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--socket", "--limit", "bounded path or identifier query"):
            if expected not in search_help.stdout:
                raise RuntimeError(
                    f"installed harness search --help did not contain {expected!r}: "
                    f"{search_help.stdout!r}"
                )

        mcp_help = _run((str(harness), "mcp", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("model-facing MCP", "stdio"):
            if expected not in mcp_help.stdout:
                raise RuntimeError(
                    f"installed harness mcp --help did not contain {expected!r}: "
                    f"{mcp_help.stdout!r}"
                )

        serve_help = _run((str(harnessd), "serve", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--database", "--socket", "canonical per-user", "search", "scan"):
            if expected not in serve_help.stdout:
                raise RuntimeError(
                    f"installed harnessd serve --help did not contain {expected!r}: "
                    f"{serve_help.stdout!r}"
                )

        doctor = _run((str(harness), "doctor"), cwd=workspace, env=isolated_env)
        for expected in ("SQLite runtime: OK", "FTS5: OK"):
            if expected not in doctor.stdout:
                raise RuntimeError(
                    f"installed harness doctor output did not contain {expected!r}: {doctor.stdout!r}"
                )

        claude_project = workspace / "claude-project"
        claude_project.mkdir()
        host_probe = _run(
            (
                str(python),
                "-c",
                (
                    "from harness.host_adapters import workspace_hints_from_environment; "
                    "h=workspace_hints_from_environment(environment={"
                    "'HARNESS_HOST_PROFILE':'claude-code',"
                    f"'CLAUDE_PROJECT_DIR':{str(claude_project)!r}"
                    "}); "
                    "print(h[0].source); print(h[0].match_mode.value); print(h[0].path)"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if host_probe.stdout.splitlines() != [
            "claude-project-dir",
            "root",
            str(claude_project.resolve()),
        ]:
            raise RuntimeError(
                f"installed Claude host adapter produced unexpected root hint: {host_probe.stdout!r}"
            )

        skill_project = workspace / "skill-project"
        skill_project.mkdir()
        _run(("git", "init", "-b", "main"), cwd=skill_project, env=isolated_env)
        skill_registry = workspace / "skill-registry"
        skill = skill_registry / "fastapi"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: fastapi\n"
            "description: Apply the project FastAPI conventions.\n"
            "---\n\n"
            "# FastAPI\n\nUse the project conventions.\n",
            encoding="utf-8",
        )
        (skill / "harness.yaml").write_text(
            "id: fastapi\ntask_hints:\n  - fastapi\n", encoding="utf-8"
        )
        skill_probe = _run(
            (
                str(python),
                "-c",
                (
                    "from pathlib import Path; "
                    "from harness.host_adapters import ClaudeCodeAdapter,codex_skill_projection_surface; "
                    "from harness.skills import "
                    "DetectedProjectStack,apply_skill_projection,load_skill_registry,"
                    "plan_skill_projection,resolve_skills; "
                    f"root=Path({str(skill_project)!r}); registry=Path({str(skill_registry)!r}); "
                    "definitions=load_skill_registry(registry); "
                    "resolved=resolve_skills(definitions,DetectedProjectStack(frozenset(),frozenset(),frozenset()),task_hints=('fastapi',)); "
                    "claude=ClaudeCodeAdapter(Path('/claude'),Path('/python')).skill_projection_surface(); "
                    "codex=codex_skill_projection_surface(); "
                    "result=apply_skill_projection(plan_skill_projection(root,resolved,(claude,codex))); "
                    "claude_target=root/'.claude'/'skills'/'fastapi'; "
                    "codex_target=root/'.agents'/'skills'/'fastapi'; "
                    "print(len(resolved)); print(result.materialized); "
                    "print((claude_target/'SKILL.md').is_file()); "
                    "print((codex_target/'SKILL.md').is_file()); "
                    "print((claude_target/'.harness-skill.json').is_file()); "
                    "print((codex_target/'.harness-skill.json').is_file()); "
                    "print((codex_target/'harness.yaml').exists())"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if skill_probe.stdout.splitlines() != [
            "1",
            "2",
            "True",
            "True",
            "True",
            "True",
            "False",
        ]:
            raise RuntimeError(
                f"installed skill resolver/projection probe was unexpected: {skill_probe.stdout!r}"
            )
        for projected_skill in (
            ".claude/skills/fastapi/SKILL.md",
            ".agents/skills/fastapi/SKILL.md",
        ):
            ignored = _run(
                ("git", "check-ignore", "-q", projected_skill),
                cwd=skill_project,
                env=isolated_env,
            )
            if ignored.returncode != 0:
                raise RuntimeError(
                    f"installed skill projection was not ignored by Git: {projected_skill}"
                )

        cursor_project = workspace / "cursor-skill-project"
        cursor_project.mkdir()
        _run(("git", "init", "-b", "main"), cwd=cursor_project, env=isolated_env)
        cursor_probe = _run(
            (
                str(python),
                "-c",
                (
                    "from pathlib import Path; "
                    "from harness.host_adapters import "
                    "ClaudeCodeAdapter,cursor_skill_projection_surface; "
                    "from harness.skills import "
                    "DetectedProjectStack,apply_skill_projection,load_skill_registry,"
                    "plan_skill_projection,resolve_skills; "
                    f"root=Path({str(cursor_project)!r}); registry=Path({str(skill_registry)!r}); "
                    "definitions=load_skill_registry(registry); "
                    "resolved=resolve_skills(definitions,DetectedProjectStack(frozenset(),frozenset(),frozenset()),task_hints=('fastapi',)); "
                    "claude=ClaudeCodeAdapter(Path('/claude'),Path('/python')).skill_projection_surface(); "
                    "cursor=cursor_skill_projection_surface(); "
                    "plan=plan_skill_projection(root,resolved,(claude,cursor)); "
                    "result=apply_skill_projection(plan); "
                    "print(','.join(str(target.relative_root) for target in plan.targets)); "
                    "print(result.materialized); "
                    "print((root/'.claude'/'skills'/'fastapi'/'SKILL.md').is_file()); "
                    "print((root/'.agents'/'skills'/'fastapi').exists()); "
                    "print((root/'.cursor'/'skills'/'fastapi').exists())"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if cursor_probe.stdout.splitlines() != [
            ".claude/skills",
            "1",
            "True",
            "False",
            "False",
        ]:
            raise RuntimeError(
                f"installed Cursor skill projection probe was unexpected: {cursor_probe.stdout!r}"
            )
        ignored = _run(
            ("git", "check-ignore", "-q", ".claude/skills/fastapi/SKILL.md"),
            cwd=cursor_project,
            env=isolated_env,
        )
        if ignored.returncode != 0:
            raise RuntimeError(
                "installed Cursor-compatible skill projection was not ignored by Git"
            )

        antigravity_project = workspace / "antigravity-skill-project"
        antigravity_project.mkdir()
        _run(("git", "init", "-b", "main"), cwd=antigravity_project, env=isolated_env)
        antigravity_probe = _run(
            (
                str(python),
                "-c",
                (
                    "from pathlib import Path; "
                    "from harness.host_adapters import antigravity_ide_skill_projection_surface; "
                    "from harness.skills import "
                    "DetectedProjectStack,apply_skill_projection,load_skill_registry,"
                    "plan_skill_projection,resolve_skills; "
                    f"root=Path({str(antigravity_project)!r}); registry=Path({str(skill_registry)!r}); "
                    "definitions=load_skill_registry(registry); "
                    "resolved=resolve_skills(definitions,DetectedProjectStack(frozenset(),frozenset(),frozenset()),task_hints=('fastapi',)); "
                    "surface=antigravity_ide_skill_projection_surface(); "
                    "plan=plan_skill_projection(root,resolved,(surface,)); "
                    "result=apply_skill_projection(plan); "
                    "target=root/'.agents'/'skills'/'fastapi'; "
                    "print(surface.profile); "
                    "print(','.join(str(item.relative_root) for item in plan.targets)); "
                    "print(result.materialized); "
                    "print((target/'SKILL.md').is_file()); "
                    "print((target/'.harness-skill.json').is_file()); "
                    "print((target/'harness.yaml').exists())"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if antigravity_probe.stdout.splitlines() != [
            "antigravity-ide",
            ".agents/skills",
            "1",
            "True",
            "True",
            "False",
        ]:
            raise RuntimeError(
                "installed Antigravity IDE skill projection probe was unexpected: "
                f"{antigravity_probe.stdout!r}"
            )
        ignored = _run(
            ("git", "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md"),
            cwd=antigravity_project,
            env=isolated_env,
        )
        if ignored.returncode != 0:
            raise RuntimeError("installed Antigravity IDE skill projection was not ignored by Git")

        antigravity_cli_project = workspace / "antigravity-cli-skill-project"
        antigravity_cli_project.mkdir()
        _run(("git", "init", "-b", "main"), cwd=antigravity_cli_project, env=isolated_env)
        antigravity_cli_probe = _run(
            (
                str(python),
                "-c",
                (
                    "from pathlib import Path; "
                    "from harness.host_adapters import antigravity_cli_skill_projection_surface; "
                    "from harness.skills import "
                    "DetectedProjectStack,apply_skill_projection,load_skill_registry,"
                    "plan_skill_projection,resolve_skills; "
                    f"root=Path({str(antigravity_cli_project)!r}); registry=Path({str(skill_registry)!r}); "
                    "definitions=load_skill_registry(registry); "
                    "resolved=resolve_skills(definitions,DetectedProjectStack(frozenset(),frozenset(),frozenset()),task_hints=('fastapi',)); "
                    "surface=antigravity_cli_skill_projection_surface(); "
                    "plan=plan_skill_projection(root,resolved,(surface,)); "
                    "result=apply_skill_projection(plan); "
                    "target=root/'.agents'/'skills'/'fastapi'; "
                    "print(surface.profile); "
                    "print(','.join(str(item.relative_root) for item in plan.targets)); "
                    "print(result.materialized); "
                    "print((target/'SKILL.md').is_file()); "
                    "print((target/'.harness-skill.json').is_file()); "
                    "print((root/'.agents'/'skills'/'fastapi.md').exists())"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if antigravity_cli_probe.stdout.splitlines() != [
            "antigravity-cli",
            ".agents/skills",
            "1",
            "True",
            "True",
            "False",
        ]:
            raise RuntimeError(
                "installed Antigravity CLI skill projection probe was unexpected: "
                f"{antigravity_cli_probe.stdout!r}"
            )
        ignored = _run(
            ("git", "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md"),
            cwd=antigravity_cli_project,
            env=isolated_env,
        )
        if ignored.returncode != 0:
            raise RuntimeError("installed Antigravity CLI skill projection was not ignored by Git")

        if os.name != "nt":
            fake_bin = workspace / "fake-claude-bin"
            fake_bin.mkdir()
            fake_claude = fake_bin / "claude"
            fake_state = workspace / "fake-claude-state.json"
            fake_claude.write_text(
                f"""#!{python}
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["HARNESS_FAKE_CLAUDE_STATE"])
args = sys.argv[1:]
if args == ["mcp", "get", "harness"]:
    if not state.exists():
        print('No MCP server found with name: "harness"')
        raise SystemExit(1)
    value = json.loads(state.read_text(encoding="utf-8"))
    print("harness:")
    print("  Scope: User config (available in all your projects)")
    print("  Status: ✓ Connected")
    print(f"  Type: {{value['type']}}")
    print(f"  Command: {{value['command']}}")
    print("  Args: " + " ".join(value.get("args", [])))
    print("  Environment:")
    for key, item in value.get("env", {{}}).items():
        print(f"    {{key}}={{item}}")
    raise SystemExit(0)
if args[:3] == ["mcp", "add-json", "harness"] and args[4:] == ["--scope", "user"]:
    if state.exists():
        print("MCP server harness already exists in user config")
        raise SystemExit(1)
    state.write_text(args[3], encoding="utf-8")
    print("Added stdio MCP server harness")
    raise SystemExit(0)
if args == ["mcp", "remove", "harness", "--scope", "user"]:
    if not state.exists():
        print('No MCP server found with name: "harness"')
        raise SystemExit(1)
    state.unlink()
    print("Removed MCP server harness")
    raise SystemExit(0)
print("unexpected fake claude invocation: " + repr(args))
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            fake_env = isolated_env.copy()
            fake_env["PATH"] = str(fake_bin)
            fake_env["HARNESS_FAKE_CLAUDE_STATE"] = str(fake_state)
            registration_probe = _run(
                (
                    str(python),
                    "-c",
                    (
                        "from harness.host_adapters import "
                        "IntegrationChange, discover_claude_code_adapter; "
                        "a=discover_claude_code_adapter(); assert a is not None; "
                        "print(a.python_executable); "
                        "print(a.register_mcp().value); "
                        "print(a.register_mcp().value); "
                        "print(a.unregister_mcp().value); "
                        "print(a.unregister_mcp().value)"
                    ),
                ),
                cwd=workspace,
                env=fake_env,
            )
            if registration_probe.stdout.splitlines() != [
                str(python.absolute()),
                "changed",
                "unchanged",
                "changed",
                "unchanged",
            ]:
                raise RuntimeError(
                    "installed Claude host adapter registration round-trip was unexpected: "
                    f"{registration_probe.stdout!r}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
