from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def path_without_agent(*prefixes: Path) -> str:
    """Return PATH with optional prefixes and without an `agent` executable."""
    parts = [str(path) for path in prefixes]
    for item in os.environ.get("PATH", "").split(os.pathsep):
        if not item:
            continue
        if Path(item, "agent").exists():
            continue
        parts.append(item)
    return os.pathsep.join(parts)


def write_fake_cursor_agent(bin_dir: Path, state_path: Path) -> Path:
    """Install a PATH `agent` that records per-cwd enable and lists five tools."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "agent"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json
import os
import sys
from pathlib import Path

state = Path(os.environ["HARNESS_FAKE_AGENT_STATE"])
args = sys.argv[1:]
cwd = str(Path.cwd().resolve())
if state.exists():
    payload = json.loads(state.read_text(encoding="utf-8"))
else:
    payload = {"enabled": {}}
if args[:3] == ["mcp", "enable", "harness"]:
    if os.environ.get("HARNESS_FAKE_AGENT_FAIL_ENABLE"):
        print("failed to enable harness")
        raise SystemExit(1)
    payload.setdefault("enabled", {})[cwd] = True
    state.write_text(json.dumps(payload), encoding="utf-8")
    print("Enabled MCP server harness")
    raise SystemExit(0)
if args[:3] == ["mcp", "list-tools", "harness"]:
    if os.environ.get("HARNESS_FAKE_AGENT_FAIL_TOOLS"):
        print("No tools available for 'harness'.")
        raise SystemExit(1)
    if not payload.get("enabled", {}).get(cwd):
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
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable
