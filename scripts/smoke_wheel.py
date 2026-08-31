import json
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import anyio
import mcp as mcp_package
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

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


def _isolated_wheel_env() -> dict[str, str]:
    """Copy the process environment without the source-checkout overlay identity."""
    values = os.environ.copy()
    values.pop("PYTHONPATH", None)
    for key in (
        "HARNESS_DEV_ROOT",
        "HARNESS_SKILL_REGISTRY",
        "HARNESS_DEV_SAVED_XDG_STATE_HOME",
        "HARNESS_DEV_SAVED_XDG_RUNTIME_DIR",
    ):
        values.pop(key, None)
    return values


def _git_init_with_file(
    path: Path, environment: Mapping[str, str], name: str, content: str
) -> None:
    path.mkdir()
    (path / name).write_text(content, encoding="utf-8")
    _run(("git", "init", "-b", "main"), cwd=path, env=environment)
    _run(("git", "add", "."), cwd=path, env=environment)
    _run(
        (
            "git",
            "-c",
            "user.name=Harness Smoke",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-m",
            "init",
        ),
        cwd=path,
        env=environment,
    )


def _require_no_global_harness(path: Path) -> None:
    if not path.is_file():
        return
    servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
    if "harness" in servers:
        raise RuntimeError(f"global mcpServers.harness is present: {path}")


def _require_cursor_enabled(state_path: Path, *workspaces: Path) -> None:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    enabled = payload.get("enabled", {})
    missing = [
        str(workspace.resolve())
        for workspace in workspaces
        if not enabled.get(str(workspace.resolve()))
    ]
    if missing:
        raise RuntimeError(
            f"Cursor CLI did not enable project harness for {missing!r}: {payload!r}"
        )


def _require_codex_config(
    path: Path, _python: Path, workspace: Path, *, hidden: bool = False
) -> None:
    if not path.is_file():
        raise RuntimeError(f"installed Codex project config is missing: {path}")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    value = config["mcp_servers"]["harness"]
    expected_root = str(workspace.resolve())
    url = value.get("url")
    if (
        not isinstance(url, str)
        or not url.startswith("http://127.0.0.1:")
        or not url.endswith("/mcp")
    ):
        raise RuntimeError(f"installed Codex config has unexpected URL: {path}")
    if "command" in value or "cwd" in value or "env" in value:
        raise RuntimeError(f"installed Codex config still uses stdio: {path}")
    headers = value.get("http_headers")
    if not isinstance(headers, dict):
        raise RuntimeError(f"installed Codex config is missing HTTP headers: {path}")
    if headers.get("X-Harness-Workspace-Root") != expected_root:
        raise RuntimeError(f"installed Codex config has wrong Workspace identity: {path}")
    authorization = headers.get("Authorization")
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise RuntimeError(f"installed Codex config is missing bearer token: {path}")
    if value.get("required") is not True:
        raise RuntimeError(f"installed Codex config is not required: {path}")
    instructions = config.get("developer_instructions")
    if not isinstance(instructions, str) or "project_status" not in instructions:
        raise RuntimeError(f"installed Codex config has no bootstrap instructions: {path}")
    has_hidden = "Durable SCM publication is human-owned" in instructions
    if has_hidden != hidden:
        raise RuntimeError(f"installed Codex config has wrong Hidden policy: {path}")


async def _cross_host_mcp_async(
    harness: Path,
    environment: dict[str, str],
    repo_a: Path,
    repo_b: Path,
    repo_c: Path,
) -> None:
    def host_env(profile: str, workspace: Path) -> dict[str, str]:
        values = dict(environment)
        values["HARNESS_HOST_PROFILE"] = profile
        if profile == "claude-code":
            values["CLAUDE_PROJECT_DIR"] = str(workspace)
            values.pop("HARNESS_WORKSPACE_ROOT", None)
        else:
            values["HARNESS_WORKSPACE_ROOT"] = str(workspace)
            values.pop("CLAUDE_PROJECT_DIR", None)
        return values

    async def open_client(profile: str, workspace: Path) -> Client:
        parameters = StdioServerParameters(
            command=str(harness),
            args=["mcp"],
            env=host_env(profile, workspace),
            cwd=str(workspace),
        )
        return Client(stdio_client(parameters))

    parameters = StdioServerParameters(
        command=str(harness),
        args=["mcp"],
        env=host_env("cursor", repo_a),
        cwd=str(repo_a),
    )
    async with Client(stdio_client(parameters)) as client:
        listed = await client.list_tools()
        names = [tool.name for tool in listed.tools]
        expected = [
            "project_status",
            "project_search",
            "project_context",
            "task_start",
            "task_checkpoint",
        ]
        if names != expected:
            raise RuntimeError(f"installed MCP five-tool surface changed: {names!r}")
        status = await client.call_tool("project_status")
        if status.is_error or status.structured_content is None:
            raise RuntimeError("installed Cursor-profile project_status failed")
        workspace_a = status.structured_content["workspace_id"]
        started = await client.call_tool("task_start", {"title": "Wheel cross-host continuity"})
        if started.is_error or started.structured_content is None:
            raise RuntimeError("installed Cursor-profile task_start failed")
        task_id = started.structured_content["task_id"]
        checkpoint = await client.call_tool(
            "task_checkpoint",
            {
                "task_id": task_id,
                "expected_revision": 1,
                "state": "working",
                "summary": "Persist wheel cross-host Knowledge",
                "knowledge": [
                    {
                        "kind": "behavior",
                        "title": "Wheel host continuity",
                        "body": "Installed wheel preserves Harness domain state across hosts.",
                        "anchors": [{"path": "main.py"}],
                    }
                ],
            },
        )
        if checkpoint.is_error:
            raise RuntimeError("installed Cursor-profile task_checkpoint failed")

    parameters = StdioServerParameters(
        command=str(harness),
        args=["mcp"],
        env=host_env("codex", repo_a),
        cwd=str(repo_b),
    )
    async with Client(stdio_client(parameters)) as client:
        status = await client.call_tool("project_status")
        if status.is_error or status.structured_content is None:
            raise RuntimeError("installed Codex-profile project_status failed")
        if status.structured_content["workspace_id"] != workspace_a:
            raise RuntimeError("Codex did not resolve repo A by exact configured root")
        current = status.structured_content["current_task"]
        if current is None or current["task_id"] != task_id:
            raise RuntimeError("Codex did not continue the Cursor-started Task")

    parameters = StdioServerParameters(
        command=str(harness),
        args=["mcp"],
        env=host_env("cursor", repo_a),
        cwd=str(repo_b),
    )
    async with Client(stdio_client(parameters)) as client:
        status = await client.call_tool("project_status")
        if status.is_error or status.structured_content is None:
            raise RuntimeError("installed Cursor-profile project_status failed")
        if status.structured_content["workspace_id"] != workspace_a:
            raise RuntimeError("Cursor did not resolve repo A by exact configured root")
        current = status.structured_content["current_task"]
        if current is None or current["task_id"] != task_id:
            raise RuntimeError("Cursor did not continue the Cursor-started Task")
        knowledge = await client.call_tool(
            "project_search",
            {"query": "wheel host continuity", "scope": "knowledge", "limit": 5},
        )
        if knowledge.is_error or knowledge.structured_content is None:
            raise RuntimeError("Cursor could not retrieve cross-host Knowledge")
        if not any(
            item["ref"].startswith("knowledge:") for item in knowledge.structured_content["results"]
        ):
            raise RuntimeError("Cursor Knowledge search did not return the persisted card")

    parameters = StdioServerParameters(
        command=str(harness),
        args=["mcp"],
        env=host_env("cursor", repo_b),
        cwd=str(repo_a),
    )
    async with Client(stdio_client(parameters)) as client:
        status = await client.call_tool("project_status")
        if status.is_error or status.structured_content is None:
            raise RuntimeError("installed Cursor repo-B project_status failed")
        workspace_b = status.structured_content["workspace_id"]
        if workspace_b == workspace_a:
            raise RuntimeError("two registered Workspaces were mixed by Cursor root resolution")
        if status.structured_content["current_task"] is not None:
            raise RuntimeError("Workspace-local current Task leaked into linked worktree B")

    parameters = StdioServerParameters(
        command=str(harness),
        args=["mcp"],
        env=host_env("cursor", repo_c),
        cwd=str(repo_a),
    )
    async with Client(stdio_client(parameters)) as client:
        status = await client.call_tool("project_status")
        if status.is_error or status.structured_content is None:
            raise RuntimeError("installed Cursor independent project_status failed")
        workspace_c = status.structured_content["workspace_id"]
        if workspace_c in {workspace_a, workspace_b}:
            raise RuntimeError("independent Workspace mixed with another registered root")
        if status.structured_content["current_task"] is not None:
            raise RuntimeError("Workspace-local current Task leaked into independent Workspace C")

    parameters = StdioServerParameters(
        command=str(harness),
        args=["mcp"],
        env=host_env("cursor", repo_a),
        cwd=str(repo_b),
    )
    async with Client(stdio_client(parameters)) as client:
        status = await client.call_tool("project_status")
        if status.is_error or status.structured_content is None:
            raise RuntimeError("installed Cursor return project_status failed")
        current = status.structured_content["current_task"]
        if current is None or current["task_id"] != task_id:
            raise RuntimeError("Task continuity failed after Codex -> Cursor return")


def _cross_host_mcp(
    harness: Path,
    environment: Mapping[str, str],
    repo_a: Path,
    repo_b: Path,
    repo_c: Path,
) -> None:
    values = dict(environment)
    dependency_root = str(Path(mcp_package.__file__).resolve().parents[1])
    existing = values.get("PYTHONPATH")
    values["PYTHONPATH"] = (
        dependency_root if not existing else dependency_root + os.pathsep + existing
    )
    anyio.run(_cross_host_mcp_async, harness, values, repo_a, repo_b, repo_c)


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

        isolated_env = _isolated_wheel_env()
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
        for expected in (
            "install",
            "uninstall",
            "doctor",
            "backup",
            "restore",
            "status",
            "scan",
            "search",
            "skills",
            "mcp",
        ):
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

        backup_help = _run((str(harness), "backup", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--database", "consistent SQLite snapshot", "existing files are never"):
            if expected not in backup_help.stdout:
                raise RuntimeError(
                    f"installed harness backup --help did not contain {expected!r}: "
                    f"{backup_help.stdout!r}"
                )

        restore_help = _run((str(harness), "restore", "--help"), cwd=workspace, env=isolated_env)
        for expected in ("--database", "--socket", "--allow-runtime-mismatch", "checksum"):
            if expected not in restore_help.stdout:
                raise RuntimeError(
                    f"installed harness restore --help did not contain {expected!r}: "
                    f"{restore_help.stdout!r}"
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

        doctor_env = isolated_env.copy()
        doctor_home = workspace / "doctor-home"
        doctor_home.mkdir()
        doctor_state = workspace / "doctor-state"
        doctor_runtime = workspace / "doctor-runtime"
        doctor_env["HOME"] = str(doctor_home)
        doctor_env["XDG_STATE_HOME"] = str(doctor_state)
        doctor_env["XDG_RUNTIME_DIR"] = str(doctor_runtime)
        doctor = _run((str(harness), "doctor"), cwd=workspace, env=doctor_env)
        for expected in ("SQLite runtime: OK", "FTS5: OK", "0 FAIL"):
            if expected not in doctor.stdout:
                raise RuntimeError(
                    f"installed harness doctor output did not contain {expected!r}: {doctor.stdout!r}"
                )
        if doctor_state.exists() or doctor_runtime.exists() or (doctor_home / ".harness").exists():
            raise RuntimeError("installed clean-machine doctor mutated canonical Harness state")

        leftover_claude_project = workspace / "leftover-claude-project"
        leftover_claude_project.mkdir()
        leftover_probe = _run(
            (
                str(python),
                "-c",
                (
                    "from harness.host_adapters import "
                    "HostIntegrationError,workspace_hints_from_environment; "
                    "try:\n"
                    "    workspace_hints_from_environment(environment={"
                    "'HARNESS_HOST_PROFILE':'claude-code',"
                    f"'CLAUDE_PROJECT_DIR':{str(leftover_claude_project)!r}"
                    "})\n"
                    "except HostIntegrationError as exc:\n"
                    "    print(str(exc))\n"
                    "else:\n"
                    "    raise SystemExit('leftover claude-code profile was accepted')"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if leftover_probe.stdout.splitlines() != [
            "unsupported Harness host profile: claude-code",
        ]:
            raise RuntimeError(
                "installed leftover Claude overlay did not fail closed: "
                f"{leftover_probe.stdout!r}"
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
                    "from harness.host_adapters import "
                    "codex_skill_projection_surface,cursor_skill_projection_surface; "
                    "from harness.skills import "
                    "DetectedProjectStack,apply_skill_projection,load_skill_registry,"
                    "plan_skill_projection,resolve_skills; "
                    f"root=Path({str(skill_project)!r}); registry=Path({str(skill_registry)!r}); "
                    "definitions=load_skill_registry(registry); "
                    "resolved=resolve_skills(definitions,DetectedProjectStack(frozenset(),frozenset(),frozenset()),task_hints=('fastapi',)); "
                    "cursor=cursor_skill_projection_surface(); "
                    "codex=codex_skill_projection_surface(); "
                    "result=apply_skill_projection(plan_skill_projection(root,resolved,(cursor,codex))); "
                    "target=root/'.agents'/'skills'/'fastapi'; "
                    "print(len(resolved)); print(result.materialized); "
                    "print((target/'SKILL.md').is_file()); "
                    "print((target/'.harness-skill.json').is_file()); "
                    "print((target/'harness.yaml').exists())"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if skill_probe.stdout.splitlines() != [
            "1",
            "1",
            "True",
            "True",
            "False",
        ]:
            raise RuntimeError(
                f"installed skill resolver/projection probe was unexpected: {skill_probe.stdout!r}"
            )
        ignored = _run(
            ("git", "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md"),
            cwd=skill_project,
            env=isolated_env,
        )
        if ignored.returncode != 0:
            raise RuntimeError(
                "installed skill projection was not ignored by Git: .agents/skills/fastapi/SKILL.md"
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
                    "from harness.host_adapters import cursor_skill_projection_surface; "
                    "from harness.skills import "
                    "DetectedProjectStack,apply_skill_projection,load_skill_registry,"
                    "plan_skill_projection,resolve_skills; "
                    f"root=Path({str(cursor_project)!r}); registry=Path({str(skill_registry)!r}); "
                    "definitions=load_skill_registry(registry); "
                    "resolved=resolve_skills(definitions,DetectedProjectStack(frozenset(),frozenset(),frozenset()),task_hints=('fastapi',)); "
                    "cursor=cursor_skill_projection_surface(); "
                    "plan=plan_skill_projection(root,resolved,(cursor,)); "
                    "result=apply_skill_projection(plan); "
                    "print(str(plan.targets[0].relative_root)); "
                    "print(','.join(str(root) for root in cursor.visible_roots)); "
                    "print(result.materialized); "
                    "print((root/'.agents'/'skills'/'fastapi'/'SKILL.md').is_file()); "
                    "print((root/'.claude'/'skills'/'fastapi').exists()); "
                    "print((root/'.cursor'/'skills'/'fastapi').exists())"
                ),
            ),
            cwd=workspace,
            env=isolated_env,
        )
        if cursor_probe.stdout.splitlines() != [
            ".agents/skills",
            ".agents/skills,.cursor/skills,.claude/skills,.codex/skills",
            "1",
            "True",
            "False",
            "False",
        ]:
            raise RuntimeError(
                f"installed Cursor skill projection probe was unexpected: {cursor_probe.stdout!r}"
            )
        ignored = _run(
            ("git", "check-ignore", "-q", ".agents/skills/fastapi/SKILL.md"),
            cwd=cursor_project,
            env=isolated_env,
        )
        if ignored.returncode != 0:
            raise RuntimeError(
                "installed Cursor skill projection was not ignored by Git"
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
            fake_bin = workspace / "fake-host-bin"
            fake_bin.mkdir()
            fake_agent = fake_bin / "agent"
            fake_agent_state = workspace / "fake-agent-state.json"
            fake_agent.write_text(
                f"""#!{python}
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["HARNESS_FAKE_AGENT_STATE"])
args = sys.argv[1:]
cwd = str(Path.cwd().resolve())
if state.exists():
    payload = json.loads(state.read_text(encoding="utf-8"))
else:
    payload = {{"enabled": {{}}}}
if args[:3] == ["mcp", "enable", "harness"]:
    payload.setdefault("enabled", {{}})[cwd] = True
    state.write_text(json.dumps(payload), encoding="utf-8")
    print("Enabled MCP server harness")
    raise SystemExit(0)
if args[:3] == ["mcp", "list-tools", "harness"]:
    if not payload.get("enabled", {{}}).get(cwd):
        print("Error: MCP server 'harness' has not been approved yet")
        raise SystemExit(1)
    for name in (
        "project_status",
        "project_search",
        "project_context",
        "task_start",
        "task_checkpoint",
    ):
        print(name)
    raise SystemExit(0)
print("unexpected fake agent invocation: " + repr(args))
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            fake_agent.chmod(0o755)
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                f"#!{python}\nraise SystemExit(0)\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            fake_env = isolated_env.copy()
            fake_env["PATH"] = str(fake_bin) + os.pathsep + isolated_env.get("PATH", "")
            fake_env["HARNESS_FAKE_AGENT_STATE"] = str(fake_agent_state)
            fake_home = workspace / "fake-home"
            fake_home.mkdir()
            canonical_skill = fake_home / ".harness" / "skills" / "python-helper"
            canonical_skill.mkdir(parents=True)
            (fake_home / ".harness" / "skills").chmod(0o700)
            (canonical_skill / "SKILL.md").write_text(
                "---\nname: python-helper\ndescription: Python conventions\n---\n\n"
                "# Python helper\n",
                encoding="utf-8",
            )
            (canonical_skill / "harness.yaml").write_text(
                "id: python-helper\napplies:\n  languages:\n    - python\n",
                encoding="utf-8",
            )
            fake_env["HOME"] = str(fake_home)
            fake_env["XDG_STATE_HOME"] = str(workspace / "fake-state-home")
            fake_env["XDG_RUNTIME_DIR"] = str(workspace / "fake-runtime-home")

            skills_list = _run((str(harness), "skills", "list"), cwd=workspace, env=fake_env)
            if "python-helper" not in skills_list.stdout or "Skills: 1" not in skills_list.stdout:
                raise RuntimeError(
                    f"installed harness skills list was unexpected: {skills_list.stdout!r}"
                )

            install = _run((str(harness), "install"), cwd=workspace, env=fake_env)
            if (
                "MCP registration: changed" not in install.stdout
                or "Diagnostics: harness doctor" not in install.stdout
                or "Harness install: OK" not in install.stdout
            ):
                raise RuntimeError(
                    f"installed harness install lifecycle was unexpected: {install.stdout!r}"
                )
            install_again = _run((str(harness), "install"), cwd=workspace, env=fake_env)
            if "MCP registration: unchanged" not in install_again.stdout:
                raise RuntimeError(
                    f"installed harness install was not idempotent: {install_again.stdout!r}"
                )
            host_state = (
                Path(fake_env["XDG_STATE_HOME"]) / "harness" / "host-integrations.json"
            )
            if json.loads(host_state.read_text(encoding="utf-8")).get("profiles") != [
                "cursor"
            ]:
                raise RuntimeError(
                    "default harness install did not record Cursor host intent: "
                    f"{host_state.read_text(encoding='utf-8')!r}"
                )

            lifecycle_project = workspace / "installed-lifecycle-project"
            lifecycle_project.mkdir()
            (lifecycle_project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
            (lifecycle_project / "AGENTS.md").write_text(
                "# User-owned wheel instructions\n", encoding="utf-8"
            )
            _run(("git", "init", "-b", "main"), cwd=lifecycle_project, env=fake_env)
            _run(("git", "add", "."), cwd=lifecycle_project, env=fake_env)
            _run(
                (
                    "git",
                    "-c",
                    "user.name=Harness Smoke",
                    "-c",
                    "user.email=harness@example.invalid",
                    "commit",
                    "-m",
                    "init",
                ),
                cwd=lifecycle_project,
                env=fake_env,
            )
            scan_a = _run(
                (str(harness), "scan", str(lifecycle_project)),
                cwd=workspace,
                env=fake_env,
            )
            if "Relevant skills: 4" not in scan_a.stdout:
                raise RuntimeError(f"installed repo-A scan was unexpected: {scan_a.stdout!r}")

            independent_project = workspace / "installed-independent-project"
            _git_init_with_file(independent_project, fake_env, "other.py", "OTHER = 1\n")
            scan_c = _run(
                (str(harness), "scan", str(independent_project)),
                cwd=workspace,
                env=fake_env,
            )
            if "Relevant skills: 4" not in scan_c.stdout:
                raise RuntimeError(
                    f"installed independent Workspace scan was unexpected: {scan_c.stdout!r}"
                )

            agents_before_hidden = (lifecycle_project / "AGENTS.md").read_bytes()
            _run(
                (str(harness), "install", "--host", "cursor"),
                cwd=workspace,
                env=fake_env,
            )
            _run(
                (str(harness), "visibility", "hidden", str(lifecycle_project)),
                cwd=workspace,
                env=fake_env,
            )

            cursor_global = fake_home / ".cursor" / "mcp.json"
            codex_install = _run(
                (str(harness), "install", "--host", "codex"), cwd=workspace, env=fake_env
            )
            if "MCP registration: changed" not in codex_install.stdout:
                raise RuntimeError(f"installed Codex registration failed: {codex_install.stdout!r}")
            codex_project_a = lifecycle_project / ".codex" / "config.toml"
            codex_project_c = independent_project / ".codex" / "config.toml"
            _require_codex_config(codex_project_a, python, lifecycle_project, hidden=True)
            _require_codex_config(codex_project_c, python, independent_project)
            if (lifecycle_project / "AGENTS.md").read_bytes() != agents_before_hidden:
                raise RuntimeError("Codex Hidden install changed user-owned AGENTS.md")
            _run(
                (str(harness), "visibility", "normal", str(lifecycle_project)),
                cwd=workspace,
                env=fake_env,
            )
            _require_codex_config(codex_project_a, python, lifecycle_project)
            _run(
                (str(harness), "uninstall", "--host", "cursor"),
                cwd=workspace,
                env=fake_env,
            )

            lifecycle_worktree = workspace / "installed-lifecycle-worktree"
            _run(
                ("git", "worktree", "add", "-b", "wheel-linked", str(lifecycle_worktree)),
                cwd=lifecycle_project,
                env=fake_env,
            )
            (lifecycle_worktree / "linked.py").write_text("LINKED = 1\n", encoding="utf-8")
            scan_b = _run(
                (str(harness), "scan", str(lifecycle_worktree)),
                cwd=workspace,
                env=fake_env,
            )
            if "Relevant skills: 4" not in scan_b.stdout:
                raise RuntimeError(f"installed worktree-B scan was unexpected: {scan_b.stdout!r}")
            codex_project_b = lifecycle_worktree / ".codex" / "config.toml"
            _require_codex_config(codex_project_b, python, lifecycle_worktree)
            codex_projected_skill = lifecycle_project / ".agents" / "skills" / "python-helper"
            if not (codex_projected_skill / "SKILL.md").is_file():
                raise RuntimeError("Codex skill projection is missing")
            codex_language_skill = lifecycle_project / ".agents" / "skills" / "language-engineering"
            if not (codex_language_skill / "references" / "python.md").is_file():
                raise RuntimeError("Codex language reference projection is missing")

            upgrade_venv = workspace / "upgrade-venv"
            _run(
                (uv, "venv", "--python", "3.13", "--no-project", str(upgrade_venv)),
                cwd=workspace,
            )
            upgrade_scripts = _venv_scripts_dir(upgrade_venv)
            upgrade_python = upgrade_scripts / "python"
            _run(
                (
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(upgrade_python),
                    "--no-deps",
                    str(wheel),
                ),
                cwd=workspace,
            )
            upgraded_harness = upgrade_scripts / "harness"
            cursor_upgrade = _run(
                (str(upgraded_harness), "install", "--host", "cursor"),
                cwd=workspace,
                env=fake_env,
            )
            if (
                "MCP registration: changed" not in cursor_upgrade.stdout
                or f"Daemon Python: {upgrade_python.absolute()}" not in cursor_upgrade.stdout
            ):
                raise RuntimeError(
                    "Cursor reinstall from a second interpreter did not replace stale runtime: "
                    f"{cursor_upgrade.stdout!r}"
                )
            upgrade_install = _run(
                (str(upgraded_harness), "install", "--host", "codex"),
                cwd=workspace,
                env=fake_env,
            )
            if f"Daemon Python: {upgrade_python.absolute()}" not in upgrade_install.stdout:
                raise RuntimeError(
                    "Codex reinstall from a second interpreter did not replace stale runtime: "
                    f"{upgrade_install.stdout!r}"
                )
            cursor_upgrade_config = json.loads(
                (lifecycle_project / ".cursor" / "mcp.json").read_text(encoding="utf-8")
            )
            if cursor_upgrade_config["mcpServers"]["harness"]["command"] != str(
                upgrade_python.absolute()
            ):
                raise RuntimeError("upgrade-safe reinstall did not update Cursor Python")
            _require_codex_config(codex_project_a, upgrade_python, lifecycle_project)
            _require_codex_config(codex_project_b, upgrade_python, lifecycle_worktree)
            _require_codex_config(codex_project_c, upgrade_python, independent_project)
            lifecycle_projected_skill = (
                lifecycle_project / ".agents" / "skills" / "python-helper"
            )
            if not (lifecycle_projected_skill / "SKILL.md").is_file():
                raise RuntimeError("Cursor skill projection is missing")
            cursor_language_skill = (
                lifecycle_project / ".agents" / "skills" / "language-engineering"
            )
            if not (cursor_language_skill / "references" / "python.md").is_file():
                raise RuntimeError("Cursor language reference projection is missing")
            lifecycle_harness = upgraded_harness

            _cross_host_mcp(
                lifecycle_harness,
                fake_env,
                lifecycle_project,
                lifecycle_worktree,
                independent_project,
            )

            full_doctor = _run((str(lifecycle_harness), "doctor"), cwd=workspace, env=fake_env)
            for expected in (
                "Daemon: OK",
                "Cursor MCP registration: OK",
                "Codex adapter: OK",
                "Codex host integration: OK",
                "Codex project MCP configs: OK",
                "Projects: OK",
                "Index state: OK",
                "Generated skills: OK",
                "Stale integrations: OK",
                "0 FAIL",
            ):
                if expected not in full_doctor.stdout:
                    raise RuntimeError(
                        f"installed full doctor did not contain {expected!r}: "
                        f"{full_doctor.stdout!r}"
                    )

            uninstall_codex = _run(
                (str(lifecycle_harness), "uninstall", "--host", "codex"),
                cwd=workspace,
                env=fake_env,
            )
            if "Project Intelligence: preserved" not in uninstall_codex.stdout:
                raise RuntimeError(f"Codex uninstall was unexpected: {uninstall_codex.stdout!r}")
            if not lifecycle_projected_skill.is_dir():
                raise RuntimeError("Codex uninstall damaged the remaining Cursor skill projection")
            if codex_project_a.exists() or codex_project_b.exists() or codex_project_c.exists():
                raise RuntimeError("Codex uninstall left Harness-owned project configs")

            cursor_after_codex = _run(
                (str(lifecycle_harness), "doctor"), cwd=workspace, env=fake_env
            )
            if "Cursor MCP registration: OK" not in cursor_after_codex.stdout:
                raise RuntimeError("Cursor was not healthy after Codex uninstall")
            if "Generated skills: OK" not in cursor_after_codex.stdout:
                raise RuntimeError("Cursor skills were not healthy after Codex uninstall")

            cursor_global.parent.mkdir(parents=True, exist_ok=True)
            cursor_global.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "harness": {
                                "type": "stdio",
                                "command": str(python.absolute()),
                                "args": ["-m", "harness.mcp_process"],
                                "env": {"HARNESS_HOST_PROFILE": "cursor"},
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cursor_install = _run(
                (str(lifecycle_harness), "install", "--host", "cursor"),
                cwd=workspace,
                env=fake_env,
            )
            if "MCP registration: changed" not in cursor_install.stdout:
                raise RuntimeError(
                    f"installed Cursor registration failed: {cursor_install.stdout!r}"
                )
            cursor_project_a = lifecycle_project / ".cursor" / "mcp.json"
            cursor_project_b = lifecycle_worktree / ".cursor" / "mcp.json"
            cursor_project_c = independent_project / ".cursor" / "mcp.json"
            for path in (cursor_project_a, cursor_project_b, cursor_project_c):
                if not path.is_file():
                    raise RuntimeError(f"installed Cursor project config is missing: {path}")
                value = json.loads(path.read_text(encoding="utf-8"))
                if value["mcpServers"]["harness"]["command"] != str(upgrade_python.absolute()):
                    raise RuntimeError(f"installed Cursor config has stale Python: {path}")
            _require_no_global_harness(cursor_global)
            _require_cursor_enabled(
                fake_agent_state,
                lifecycle_project,
                lifecycle_worktree,
                independent_project,
            )
            cursor_doctor = _run((str(lifecycle_harness), "doctor"), cwd=workspace, env=fake_env)
            for expected in (
                "Cursor MCP registration: OK",
                "Cursor project MCP overrides: OK",
                "Cursor project MCP tools: OK",
            ):
                if expected not in cursor_doctor.stdout:
                    raise RuntimeError(
                        f"installed Cursor doctor did not contain {expected!r}: "
                        f"{cursor_doctor.stdout!r}"
                    )

            uninstall_all = _run(
                (str(lifecycle_harness), "uninstall", "--host", "all"),
                cwd=workspace,
                env=fake_env,
            )
            if "Project Intelligence: preserved" not in uninstall_all.stdout:
                raise RuntimeError(f"multi-host uninstall was unexpected: {uninstall_all.stdout!r}")
            database = Path(fake_env["XDG_STATE_HOME"]) / "harness" / "harness.db"
            if not database.is_file():
                raise RuntimeError("multi-host uninstall did not preserve Project Intelligence")
            if (
                cursor_project_a.exists()
                or cursor_project_b.exists()
                or cursor_project_c.exists()
            ):
                raise RuntimeError("multi-host uninstall left Harness-owned host registrations")

            _run(
                (str(lifecycle_harness), "install", "--host", "all"),
                cwd=workspace,
                env=fake_env,
            )
            purge = _run(
                (str(lifecycle_harness), "uninstall", "--host", "all", "--purge"),
                cwd=workspace,
                env=fake_env,
            )
            if (
                "Project Intelligence: purged" not in purge.stdout
                or database.exists()
                or (fake_home / ".harness" / "skills").exists()
            ):
                raise RuntimeError(
                    "installed multi-host uninstall --purge lifecycle was unexpected: "
                    f"{purge.stdout!r}"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
