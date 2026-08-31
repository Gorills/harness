from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness.codex_adapter import (
    CODEX_BOOTSTRAP_INSTRUCTION_BODY,
    CODEX_MCP_FORWARD_ENV_VARS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = (
    "project_status",
    "project_search",
    "project_context",
    "task_start",
    "task_checkpoint",
)
EXPECTED_GENERATED_SKILLS = ("secure-by-design", "testing-strategy")
EXPECTED_TOOL_INPUT_PROPERTIES = {
    "project_status": frozenset(),
    "project_search": frozenset({"query", "scope", "limit"}),
    "project_context": frozenset({"refs"}),
    "task_start": frozenset({"title", "stack_hints", "task_id", "expected_revision"}),
    "task_checkpoint": frozenset(
        {
            "task_id",
            "expected_revision",
            "state",
            "summary",
            "next_step",
            "wait_reason",
            "verification",
            "knowledge",
        }
    ),
}
_DEFAULT_TIMEOUT_SECONDS = 300
_MODEL_USAGE_DISCLOSURE = (
    "External destination: the OpenAI Codex service selected by the Codex CLI.\n"
    "Account effect: one model run consumes usage for the explicitly supplied CODEX_API_KEY. "
    "The key is inherited by the temporary Codex process and may be inherited by its trusted "
    "Harness MCP child; the runner never prints or stores it outside temporary Codex state.\n"
    "Payload: the fixed acceptance prompt; metadata from a temporary Git repository containing "
    "only README.md and pyproject.toml fixture text; and Harness MCP results containing temporary "
    "Workspace/Task IDs, path metadata, and acceptance Task text. No user repository source is "
    "included.\n"
    "Local effects: the exact Harness wheel, daemon state, generated skills, project Codex config, "
    "and Task data live under one temporary directory and are removed after the run. The runner "
    "uses a temporary trusted CODEX_HOME, does not read saved Codex authentication or write user "
    "trust/~/.codex/config.toml, and fails if that user config's bytes change."
)
_GLOBAL_INSTALL_DISCLOSURE = (
    "Machine effect: replace the user-global uv-tool Harness executable with the exact current "
    "checkout before acceptance. Canonical Harness database/socket/skills and real project "
    "Codex configuration are not used by the synthetic test; those operations receive temporary "
    "XDG, Harness, Codex, and Git Workspace roots. Live daemon/host activation is a separate "
    "explicit command.\n"
)


class CodexAcceptanceError(RuntimeError):
    """Raised when real Codex CLI acceptance cannot be proven."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: int = 120,
    show_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {shlex.join(command)}", flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodexAcceptanceError(f"command timed out: {shlex.join(command)}") from exc
    if show_output and completed.stdout:
        print(completed.stdout, end="")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise CodexAcceptanceError(
            f"command failed ({completed.returncode}): {shlex.join(command)}: {detail}"
        )
    return completed


def _isolated_environment(root: Path, codex: Path) -> dict[str, str]:
    values = os.environ.copy()
    for key in (
        "CODEX_API_KEY",
        "PYTHONPATH",
        "HARNESS_DEV_ROOT",
        "HARNESS_DEV_SAVED_XDG_STATE_HOME",
        "HARNESS_DEV_SAVED_XDG_RUNTIME_DIR",
        "HARNESS_HOST_PROFILE",
        "HARNESS_WORKSPACE_ROOT",
        "CLAUDE_PROJECT_DIR",
        "WORKSPACE_FOLDER_PATHS",
    ):
        values.pop(key, None)
    values["XDG_STATE_HOME"] = str(root / "state")
    values["XDG_RUNTIME_DIR"] = str(root / "runtime")
    values["HARNESS_SKILL_REGISTRY"] = str(root / "skills")
    values["PATH"] = str(codex.parent) + os.pathsep + values.get("PATH", "")
    return values


def _prepare_temporary_codex_home(root: Path, workspaces: Sequence[Path]) -> Path:
    codex_home = root / "codex-home"
    codex_home.mkdir(mode=0o700)
    config = codex_home / "config.toml"
    trust = "\n".join(
        f'[projects.{json.dumps(str(workspace.resolve()))}]\ntrust_level = "trusted"\n'
        for workspace in workspaces
    )
    config.write_text(
        trust,
        encoding="utf-8",
    )
    config.chmod(0o600)
    return codex_home


def _optional_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _codex_user_config(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _init_repository(
    path: Path,
    environment: Mapping[str, str],
    *,
    project_name: str,
) -> None:
    path.mkdir()
    (path / "README.md").write_text(
        "# Temporary Python package\n\n"
        "Business requirement: the package version is 0.0.0 and it requires Python 3.13.\n",
        encoding="utf-8",
    )
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{project_name}"\nversion = "0.0.0"\nrequires-python = ">=3.13"\n',
        encoding="utf-8",
    )
    _run(("git", "init", "-b", "main"), cwd=path, environment=environment)
    _run(("git", "add", "."), cwd=path, environment=environment)
    _run(
        (
            "git",
            "-c",
            "user.name=Harness Acceptance",
            "-c",
            "user.email=harness@example.invalid",
            "commit",
            "-m",
            "acceptance fixture",
        ),
        cwd=path,
        environment=environment,
    )


def _verify_project_config(
    path: Path,
    python: Path,
    workspace: Path,
    local_environment: Mapping[str, str],
) -> dict[str, str]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        entry = value["mcp_servers"]["harness"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise CodexAcceptanceError(f"generated Codex project config is invalid: {path}") from exc
    expected_root = str(workspace.resolve())
    if value.get("developer_instructions") != CODEX_BOOTSTRAP_INSTRUCTION_BODY:
        raise CodexAcceptanceError(
            f"generated Codex project config has no exact Harness bootstrap instructions: {path}"
        )
    static_environment: dict[str, str] = {
        "HARNESS_HOST_PROFILE": "codex",
        "HARNESS_WORKSPACE_ROOT": expected_root,
    }
    expected: dict[str, object] = {
        "command": str(python.absolute()),
        "args": ["-m", "harness.mcp_process"],
        "env_vars": list(CODEX_MCP_FORWARD_ENV_VARS),
        "cwd": expected_root,
        "required": True,
        "env": static_environment,
    }
    if entry != expected:
        raise CodexAcceptanceError(
            f"generated Codex project config does not match the acceptance runtime: {entry!r}"
        )
    server_environment = static_environment.copy()
    for name in CODEX_MCP_FORWARD_ENV_VARS:
        forwarded_value = local_environment.get(name)
        if forwarded_value is not None:
            server_environment[name] = forwarded_value
    return server_environment


def _verify_codex_inspection(payload: object, python: Path, workspace: Path) -> None:
    if not isinstance(payload, dict):
        raise CodexAcceptanceError("codex mcp get did not return an object")
    expected_root = str(workspace.resolve())
    expected_transport = {
        "type": "stdio",
        "command": str(python.absolute()),
        "args": ["-m", "harness.mcp_process"],
        "env": {
            "HARNESS_HOST_PROFILE": "codex",
            "HARNESS_WORKSPACE_ROOT": expected_root,
        },
        "env_vars": list(CODEX_MCP_FORWARD_ENV_VARS),
        "cwd": expected_root,
    }
    if payload.get("name") != "harness" or payload.get("transport") != expected_transport:
        raise CodexAcceptanceError(f"Codex loaded an unexpected Harness MCP transport: {payload!r}")
    if payload.get("enabled") is not True or payload.get("disabled_reason") is not None:
        raise CodexAcceptanceError(f"Codex did not enable the Harness MCP server: {payload!r}")


def prompt_input_contains_bootstrap(value: object) -> bool:
    """Return whether rendered model input contains the exact Harness bootstrap text."""
    if isinstance(value, str):
        return CODEX_BOOTSTRAP_INSTRUCTION_BODY in value
    if isinstance(value, Mapping):
        return any(prompt_input_contains_bootstrap(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(prompt_input_contains_bootstrap(item) for item in value)
    return False


def _verify_codex_prompt_input(
    codex: Path,
    workspace: Path,
    environment: Mapping[str, str],
) -> None:
    rendered = _run(
        (str(codex), "debug", "prompt-input", "Harness acceptance prompt probe"),
        cwd=workspace,
        environment=environment,
        show_output=False,
    )
    try:
        payload = json.loads(rendered.stdout)
    except json.JSONDecodeError as exc:
        raise CodexAcceptanceError("codex debug prompt-input emitted invalid JSON") from exc
    if not prompt_input_contains_bootstrap(payload):
        raise CodexAcceptanceError(
            "Codex model-visible prompt input omitted the Harness bootstrap instructions"
        )


def _verify_untrusted_project_fail_closed(
    codex: Path,
    workspace: Path,
    environment: Mapping[str, str],
) -> None:
    untrusted_home = Path(environment["CODEX_HOME"]).parent / "codex-home-untrusted"
    untrusted_home.mkdir(mode=0o700)
    untrusted_environment = dict(environment)
    untrusted_environment["CODEX_HOME"] = str(untrusted_home)
    command = (str(codex), "mcp", "get", "harness", "--json")
    print(f"+ {shlex.join(command)} # expect untrusted refusal", flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=untrusted_environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodexAcceptanceError("untrusted Codex project inspection timed out") from exc
    if completed.returncode == 0:
        raise CodexAcceptanceError(
            "Codex loaded the Harness project MCP server without temporary project trust"
        )
    if (untrusted_home / "config.toml").exists():
        raise CodexAcceptanceError("untrusted inspection unexpectedly created Codex config")


def _parse_json_lines(value: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for position, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAcceptanceError(
                f"codex exec emitted invalid JSONL at line {position}"
            ) from exc
        if not isinstance(event, dict):
            raise CodexAcceptanceError(
                f"codex exec emitted a non-object JSONL event at line {position}"
            )
        events.append(event)
    if not events:
        raise CodexAcceptanceError("codex exec emitted no JSONL events")
    return events


def completed_harness_tool_calls(events: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Extract successful real Harness MCP calls from Codex JSONL events."""
    calls: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        server = item.get("server") or item.get("server_name")
        tool = item.get("tool") or item.get("name")
        if server != "harness" or not isinstance(tool, str):
            continue
        status = item.get("status")
        if status not in {None, "completed"} or item.get("error") not in {None, ""}:
            raise CodexAcceptanceError(f"Harness MCP tool call failed: {item!r}")
        calls.append(tool)
    return tuple(calls)


def project_actions_before_harness_status(
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return repository/tool actions completed before the first Harness project_status call."""
    actions: list[str] = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "mcp_tool_call":
            server = item.get("server") or item.get("server_name")
            tool = item.get("tool") or item.get("name")
            if server == "harness" and tool == "project_status":
                return tuple(actions)
            actions.append(f"mcp:{server}:{tool}")
        elif item_type in {"command_execution", "file_change"}:
            actions.append(str(item_type))
    return tuple(actions)


def _validate_wire_tools(tools: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    names = tuple(str(tool.get("name")) for tool in tools)
    if names != EXPECTED_TOOLS:
        raise CodexAcceptanceError(f"installed MCP five-tool surface changed: {names!r}")
    serialized = json.dumps(tools, sort_keys=True)
    for forbidden in ("content_sha256", "baseline_head", "source_checkpoint_id"):
        if forbidden in serialized:
            raise CodexAcceptanceError(
                f"installed MCP schema disclosed forbidden field: {forbidden}"
            )
    for tool in tools:
        name = str(tool["name"])
        description = tool.get("description")
        schema = tool.get("inputSchema")
        if not isinstance(description, str) or not description:
            raise CodexAcceptanceError(f"installed MCP tool has no description: {name}")
        if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
            raise CodexAcceptanceError(f"installed MCP tool schema is not fail-closed: {name}")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise CodexAcceptanceError(f"installed MCP tool schema has no properties: {name}")
        observed = frozenset(str(key) for key in properties)
        if observed != EXPECTED_TOOL_INPUT_PROPERTIES[name]:
            raise CodexAcceptanceError(
                f"installed MCP input properties changed for {name}: {sorted(observed)!r}"
            )
    return names


def _structured_result(name: str, result: Any) -> dict[str, Any]:
    if result.is_error:
        raise CodexAcceptanceError(f"installed MCP {name} call returned an error")
    content = result.structured_content
    if not isinstance(content, dict):
        raise CodexAcceptanceError(f"installed MCP {name} returned no structured content")
    return content


async def _verify_mcp_wire_async(
    python: Path,
    workspace: Path,
    environment: Mapping[str, str],
) -> tuple[tuple[str, ...], str]:
    parameters = StdioServerParameters(
        command=str(python.absolute()),
        args=["-m", "harness.mcp_process"],
        env=dict(environment),
        cwd=str(workspace.resolve()),
    )
    async with Client(stdio_client(parameters)) as client:
        listed = await client.list_tools()
        tools = tuple(tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools)
        names = _validate_wire_tools(tools)

        status = _structured_result("project_status", await client.call_tool("project_status"))
        workspace_id = status.get("workspace_id")
        if status.get("workspace_root") != str(workspace.resolve()) or not isinstance(
            workspace_id, str
        ):
            raise CodexAcceptanceError(
                f"installed MCP resolved the wrong Workspace identity: {status!r}"
            )
        searched = _structured_result(
            "project_search",
            await client.call_tool(
                "project_search", {"query": "pyproject", "scope": "code", "limit": 5}
            ),
        )
        results = searched.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise CodexAcceptanceError("installed MCP project_search returned no fixture result")
        selected_ref = results[0].get("ref")
        if not isinstance(selected_ref, str):
            raise CodexAcceptanceError("installed MCP project_search returned no usable ref")
        context = _structured_result(
            "project_context",
            await client.call_tool("project_context", {"refs": [selected_ref]}),
        )
        items = context.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise CodexAcceptanceError("installed MCP project_context returned no fixture item")
        if items[0].get("ref") != selected_ref:
            raise CodexAcceptanceError("installed MCP project_context returned the wrong ref")

        started = _structured_result(
            "task_start",
            await client.call_tool(
                "task_start",
                {"title": "Локальная проверка Codex MCP", "stack_hints": ["python"]},
            ),
        )
        task_id = started.get("task_id")
        revision = started.get("revision")
        if not isinstance(task_id, str) or not isinstance(revision, int):
            raise CodexAcceptanceError("installed MCP task_start returned invalid Task identity")
        checkpoint = _structured_result(
            "task_checkpoint",
            await client.call_tool(
                "task_checkpoint",
                {
                    "task_id": task_id,
                    "expected_revision": revision,
                    "state": "completed",
                    "summary": "Пять MCP-инструментов проверены локальным wire-клиентом",
                    "next_step": None,
                    "verification": [
                        {
                            "name": "Installed MCP wire acceptance",
                            "status": "passed",
                            "evidence": "Official MCP SDK completed all five calls",
                        }
                    ],
                },
            ),
        )
        if (
            checkpoint.get("task_id") != task_id
            or checkpoint.get("state") != "completed"
            or checkpoint.get("revision") != revision + 1
        ):
            raise CodexAcceptanceError("installed MCP task_checkpoint returned invalid continuity")
        return names, workspace_id


async def _verify_mcp_read_only_async(
    python: Path,
    workspace: Path,
    environment: Mapping[str, str],
) -> tuple[tuple[str, ...], str]:
    """Verify the production catalog and status without creating Task or Project state."""
    parameters = StdioServerParameters(
        command=str(python.absolute()),
        args=["-m", "harness.mcp_process"],
        env=dict(environment),
        cwd=str(workspace.resolve()),
    )
    async with Client(stdio_client(parameters)) as client:
        listed = await client.list_tools()
        tools = tuple(tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools)
        names = _validate_wire_tools(tools)
        status = _structured_result("project_status", await client.call_tool("project_status"))
        workspace_id = status.get("workspace_id")
        if status.get("workspace_root") != str(workspace.resolve()) or not isinstance(
            workspace_id, str
        ):
            raise CodexAcceptanceError(
                f"installed MCP resolved the wrong Workspace identity: {status!r}"
            )
        return names, workspace_id


def verify_mcp_read_only(
    python: Path,
    workspace: Path,
    environment: Mapping[str, str],
) -> tuple[tuple[str, ...], str]:
    return anyio.run(_verify_mcp_read_only_async, python, workspace, environment)


def _verify_mcp_wire(
    python: Path,
    workspace: Path,
    environment: Mapping[str, str],
) -> tuple[tuple[str, ...], str]:
    return anyio.run(_verify_mcp_wire_async, python, workspace, environment)


def _acceptance_prompt() -> str:
    return (
        "Audit this temporary Python package as a normal repository task. Determine whether "
        "pyproject.toml matches the business requirement in README.md. Do not edit files. Use the "
        "project's configured context and continuity mechanisms exactly as you would in normal "
        "work: inspect current state, find and expand the relevant project context, create a "
        "Russian-titled work record with only the affected stack hint, and complete it with a "
        "passed verification. Return one short JSON object with the observed workspace identity, "
        "work-record identity, final revision, and audit result. Never invent a result if a tool "
        "call fails."
    )


def _build_installed_wheel(
    root: Path, uv: Path, environment: Mapping[str, str]
) -> tuple[Path, Path]:
    dist = root / "dist"
    venv = root / "venv"
    _run(
        (
            str(uv),
            "build",
            "--wheel",
            "--no-sources",
            "--no-build-isolation",
            "--out-dir",
            str(dist),
        ),
        cwd=PROJECT_ROOT,
        environment=environment,
    )
    wheels = tuple(dist.glob("harness-*.whl"))
    if len(wheels) != 1:
        raise CodexAcceptanceError(f"expected one Harness wheel, found {len(wheels)}")
    _run(
        (str(uv), "venv", "--python", "3.13", "--no-project", str(venv)),
        cwd=root,
        environment=environment,
    )
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    _run(
        (str(uv), "pip", "install", "--python", str(python), str(wheels[0])),
        cwd=root,
        environment=environment,
    )
    return scripts / ("harness.exe" if os.name == "nt" else "harness"), python


def _global_install_environment() -> dict[str, str]:
    values = os.environ.copy()
    values.pop("CODEX_API_KEY", None)
    values.pop("PYTHONPATH", None)
    return values


def _installed_python_from_console_script(script: Path) -> Path:
    try:
        first_line = script.read_bytes().splitlines()[0]
    except (IndexError, OSError) as exc:
        raise CodexAcceptanceError(
            f"global Harness console script could not be inspected: {script}"
        ) from exc
    prefix = b"#!"
    if not first_line.startswith(prefix):
        raise CodexAcceptanceError(f"global Harness console script has no shebang: {script}")
    try:
        python = Path(os.fsdecode(first_line.removeprefix(prefix)))
    except UnicodeError as exc:
        raise CodexAcceptanceError(
            f"global Harness console script has an unusable interpreter: {script}"
        ) from exc
    if not python.is_absolute():
        raise CodexAcceptanceError(
            f"global Harness console script interpreter is not absolute: {python}"
        )
    if not python.is_file() or not os.access(python, os.X_OK):
        raise CodexAcceptanceError(f"global Harness interpreter is not executable: {python}")
    return python


def _install_and_resolve_global_runtime(
    uv: Path,
    environment: Mapping[str, str],
) -> tuple[Path, Path]:
    if os.name == "nt":
        raise CodexAcceptanceError("global Harness machine acceptance currently requires POSIX")
    _run(
        (str(PROJECT_ROOT / "scripts" / "install-global"), "--package-only"),
        cwd=PROJECT_ROOT,
        environment=environment,
        timeout=300,
    )
    tool_dir = _run(
        (str(uv), "tool", "dir", "--bin"),
        cwd=PROJECT_ROOT,
        environment=environment,
        show_output=False,
    ).stdout.strip()
    if not tool_dir:
        raise CodexAcceptanceError("uv returned no user-global tool bin directory")
    harness = Path(tool_dir) / "harness"
    if not harness.is_file() or not os.access(harness, os.X_OK):
        raise CodexAcceptanceError(f"global Harness executable is missing: {harness}")
    return harness, _installed_python_from_console_script(harness)


def run_acceptance(
    *,
    codex: Path,
    uv: Path,
    timeout: int,
    model: str | None,
    evidence_path: Path | None,
    run_model: bool,
    global_install: bool = False,
) -> dict[str, object]:
    user_config = _codex_user_config(os.environ)
    user_config_before = _optional_bytes(user_config)
    global_runtime = (
        _install_and_resolve_global_runtime(uv, _global_install_environment())
        if global_install
        else None
    )
    with tempfile.TemporaryDirectory(prefix="harness-codex-real-host-") as temporary:
        root = Path(temporary)
        environment = _isolated_environment(root, codex)
        harness, python = (
            global_runtime
            if global_runtime is not None
            else _build_installed_wheel(root, uv, environment)
        )
        workspaces = (root / "workspace-a", root / "workspace-b")
        for index, workspace in enumerate(workspaces, start=1):
            _init_repository(
                workspace,
                environment,
                project_name=f"harness-codex-acceptance-{index}",
            )
        primary_workspace = workspaces[0]
        environment["CODEX_HOME"] = str(_prepare_temporary_codex_home(root, workspaces))
        installed = False
        primary_error: BaseException | None = None
        report: dict[str, object] = {}
        try:
            _run(
                (str(harness), "install", "--host", "codex"),
                cwd=primary_workspace,
                environment=environment,
            )
            installed = True
            wire_calls: tuple[str, ...] = ()
            wire_workspace_ids: list[str] = []
            projected_skill_names: tuple[str, ...] | None = None
            for workspace in workspaces:
                _run(
                    (str(harness), "scan", str(workspace)),
                    cwd=workspace,
                    environment=environment,
                )
                if workspace == primary_workspace:
                    _verify_untrusted_project_fail_closed(codex, workspace, environment)
                config = workspace / ".codex" / "config.toml"
                mcp_environment = _verify_project_config(
                    config,
                    python,
                    workspace,
                    environment,
                )
                skills = tuple(
                    sorted(
                        path.parent.name
                        for path in (workspace / ".agents" / "skills").glob("*/SKILL.md")
                    )
                )
                if skills != EXPECTED_GENERATED_SKILLS:
                    raise CodexAcceptanceError(
                        f"Codex received unexpected relevant/irrelevant project skills: {skills!r}"
                    )
                if projected_skill_names is None:
                    projected_skill_names = skills
                elif skills != projected_skill_names:
                    raise CodexAcceptanceError("Codex skill projection differs across fixtures")
                status = _run(
                    ("git", "status", "--porcelain", "--untracked-files=all"),
                    cwd=workspace,
                    environment=environment,
                    show_output=False,
                )
                if status.stdout:
                    raise CodexAcceptanceError(
                        f"Harness Codex artifacts are not ignored by Git: {status.stdout!r}"
                    )

                inspected = _run(
                    (str(codex), "mcp", "get", "harness", "--json"),
                    cwd=workspace,
                    environment=environment,
                    show_output=False,
                )
                try:
                    inspection_payload = json.loads(inspected.stdout)
                except json.JSONDecodeError as exc:
                    raise CodexAcceptanceError("codex mcp get emitted invalid JSON") from exc
                _verify_codex_inspection(inspection_payload, python, workspace)
                _verify_codex_prompt_input(codex, workspace, environment)

                observed_calls, wire_workspace_id = _verify_mcp_wire(
                    python,
                    workspace,
                    mcp_environment,
                )
                if wire_calls and observed_calls != wire_calls:
                    raise CodexAcceptanceError("installed MCP catalogs differ across Workspaces")
                wire_calls = observed_calls
                wire_workspace_ids.append(wire_workspace_id)
            if len(set(wire_workspace_ids)) != len(workspaces):
                raise CodexAcceptanceError(
                    f"installed MCP confused simultaneous Workspaces: {wire_workspace_ids!r}"
                )
            if projected_skill_names is None:
                raise CodexAcceptanceError("Codex generated no project skills")

            model_calls: tuple[str, ...] = ()
            if run_model:
                command: list[str] = [
                    str(codex),
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "--cd",
                    str(primary_workspace),
                    "-c",
                    "mcp_servers.harness.required=true",
                ]
                if model is not None:
                    command.extend(("--model", model))
                command.append(_acceptance_prompt())
                model_environment = environment.copy()
                model_environment["CODEX_API_KEY"] = os.environ["CODEX_API_KEY"]
                executed = _run(
                    command,
                    cwd=primary_workspace,
                    environment=model_environment,
                    timeout=timeout,
                    show_output=False,
                )
                events = _parse_json_lines(executed.stdout)
                model_calls = completed_harness_tool_calls(events)
                premature_actions = project_actions_before_harness_status(events)
                if premature_actions:
                    raise CodexAcceptanceError(
                        "Codex performed project actions before Harness project_status: "
                        f"{premature_actions!r}"
                    )
                if not model_calls or model_calls[0] != "project_status":
                    raise CodexAcceptanceError(
                        f"Codex did not use Harness project_status first: {model_calls!r}"
                    )
                missing = [name for name in EXPECTED_TOOLS if name not in model_calls]
                if missing:
                    observed_types = sorted(
                        {
                            str(item.get("type"))
                            for event in events
                            if isinstance((item := event.get("item")), dict)
                        }
                    )
                    raise CodexAcceptanceError(
                        f"Codex did not complete every Harness MCP tool; missing {missing!r}; "
                        f"observed item types: {observed_types!r}"
                    )
            doctor = _run(
                (str(harness), "doctor"),
                cwd=primary_workspace,
                environment=environment,
                show_output=False,
            )
            if "0 FAIL" not in doctor.stdout:
                raise CodexAcceptanceError(
                    "harness doctor reported a failure after Codex execution"
                )
            version = _run(
                (str(codex), "--version"),
                cwd=primary_workspace,
                environment=environment,
                show_output=False,
            ).stdout.strip()
            report = {
                "schema_version": 2,
                "runtime_source": "user-global-uv-tool" if global_install else "temporary-wheel",
                "codex_version": version,
                "workspace_identities": [str(workspace.resolve()) for workspace in workspaces],
                "wire_workspace_ids": wire_workspace_ids,
                "workspace_isolation_verified": True,
                "untrusted_project_fail_closed_verified": True,
                "project_configs_verified": len(workspaces),
                "codex_project_config_discoveries_verified": len(workspaces),
                "codex_prompt_bootstrap_verified": True,
                "generated_skill_names": list(projected_skill_names),
                "generated_skill_count_per_workspace": len(projected_skill_names),
                "wire_verified_harness_tool_calls": list(wire_calls),
                "model_completed_harness_tool_calls": list(model_calls),
                "model_run": run_model,
                "all_five_wire_tools_verified": True,
                "all_five_model_tools_verified": run_model,
                "doctor_zero_fail": True,
            }
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if installed:
                try:
                    _run(
                        (str(harness), "uninstall", "--host", "codex", "--purge"),
                        cwd=primary_workspace,
                        environment=environment,
                        show_output=primary_error is None,
                    )
                except BaseException:
                    if primary_error is None:
                        raise
            surviving_configs = [
                workspace / ".codex" / "config.toml"
                for workspace in workspaces
                if (workspace / ".codex" / "config.toml").exists()
            ]
            surviving_skills = [
                workspace / ".agents" / "skills" / "testing-strategy" / "SKILL.md"
                for workspace in workspaces
                if (workspace / ".agents" / "skills" / "testing-strategy" / "SKILL.md").exists()
            ]
            if primary_error is None and (surviving_configs or surviving_skills):
                raise CodexAcceptanceError(
                    "owned Codex project artifacts survived uninstall: "
                    f"{surviving_configs + surviving_skills!r}"
                )
            if _optional_bytes(user_config) != user_config_before:
                raise CodexAcceptanceError(
                    f"real-host acceptance changed user Codex config: {user_config}"
                )
        report["owned_cleanup_verified"] = True
        report["user_codex_config_unchanged"] = True
        if evidence_path is not None:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run opt-in real Codex CLI acceptance against a temporary install of the exact "
            "Harness wheel without mutating global Harness state or Codex config/trust."
        )
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--run-model",
        action="store_true",
        help="acknowledge external model usage; requires CODEX_API_KEY in the environment",
    )
    execution.add_argument(
        "--preflight-only",
        action="store_true",
        help="verify exact wheel/config/skills/doctor/cleanup without invoking a model",
    )
    parser.add_argument("--codex", type=Path, help="Codex CLI executable (default: PATH)")
    parser.add_argument("--uv", type=Path, help="uv executable (default: PATH)")
    parser.add_argument(
        "--global-install",
        action="store_true",
        help=(
            "replace the user-global uv-tool package, then run acceptance with temporary "
            "Harness/Codex/Workspace state"
        ),
    )
    parser.add_argument("--model", help="optional exact Codex model override")
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"codex exec timeout in seconds (default: {_DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument("--evidence", type=Path, help="write a sanitized JSON acceptance report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.run_model and not args.preflight_only:
        print(_MODEL_USAGE_DISCLOSURE, flush=True)
        print("Codex real-host acceptance was not run. Pass --run-model only after approval.")
        return 2
    if args.run_model and not os.environ.get("CODEX_API_KEY"):
        print(_MODEL_USAGE_DISCLOSURE, flush=True)
        print(
            "Codex real-host acceptance: FAIL (set CODEX_API_KEY for this invocation through "
            "a secure shell or secret mechanism; do not paste it into chat)",
            flush=True,
        )
        return 1
    codex_value = str(args.codex) if args.codex is not None else shutil.which("codex")
    uv_value = str(args.uv) if args.uv is not None else shutil.which("uv")
    if codex_value is None:
        print("Codex real-host acceptance: FAIL (Codex CLI not found)", flush=True)
        return 1
    if uv_value is None:
        print("Codex real-host acceptance: FAIL (uv not found)", flush=True)
        return 1
    if args.timeout <= 0:
        print("Codex real-host acceptance: FAIL (--timeout must be positive)", flush=True)
        return 1
    if args.global_install:
        print(_GLOBAL_INSTALL_DISCLOSURE, flush=True)
    try:
        report = run_acceptance(
            codex=Path(codex_value).resolve(),
            uv=Path(uv_value).resolve(),
            timeout=args.timeout,
            model=args.model,
            evidence_path=args.evidence,
            run_model=args.run_model,
            global_install=args.global_install,
        )
    except (CodexAcceptanceError, OSError) as exc:
        print(f"Codex real-host acceptance: FAIL ({exc})", flush=True)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    print("Codex real-host acceptance: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
