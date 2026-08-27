# Isolated development and testing

Harness is one global install per user with one canonical daemon. A checkout does not share that process, database, or Unix socket with a separately installed build.

Isolation is done by overriding the XDG bases from [ADR-0007](../decisions/0007-canonical-posix-runtime-paths.md). The product still resolves `$XDG_STATE_HOME/harness/harness.db` and `$XDG_RUNTIME_DIR/harness/harness.sock`; only the bases change.

## What is isolated

| Concern | System install | This checkout (`scripts/dev`) |
| --- | --- | --- |
| CLI / package | `harness` on `PATH` | `uv run --frozen` against this repository |
| Database | `~/.local/state/harness/harness.db` | `.harness/state/harness/harness.db` |
| Socket | `$XDG_RUNTIME_DIR/harness/harness.sock` or `/tmp/harness-<uid>/harness.sock` | `.harness/runtime/harness/harness.sock` |
| Autostart | `python -m harness.daemon_process` of the invoked interpreter | same module from this checkout's environment |
| Host MCP | user-global Cursor/Claude `harness` server | tracked project overlay launching `scripts/dev harness mcp` |
| `install` / `uninstall` | mutates user-global host MCP | refused while `HARNESS_DEV_ROOT` is set |

`.harness/` is gitignored. It may also hold a local `uv` bootstrap under `.harness/tools/`.

## 1. One-time sync

From the repository root:

```bash
chmod +x scripts/dev
scripts/dev sync
```

`scripts/dev` requires `uv 0.12.5`. If that version is not on `PATH`, the wrapper bootstraps it into `.harness/tools/` (needs `curl`). Override the executable with `HARNESS_DEV_UV` when you already have 0.12.5 somewhere else.

This installs Python 3.13 if needed and runs `uv sync --locked --all-groups`.

## 2. Check the isolated runtime

```bash
scripts/dev env
scripts/dev harness --version
scripts/dev harness doctor
```

`scripts/dev env` must print `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` under this repository's `.harness/` directory, not `~/.local/state` or `/run/user/...`.

`harness doctor` without `--database` does not create durable state. After a scan exists:

```bash
scripts/dev harness doctor --database .harness/state/harness/harness.db
```

## 3. Run the implemented CLI against this checkout

These commands autostart an isolated `harnessd` when the local socket is absent. That daemon inherits the wrapper environment, so it binds the repository socket and database.

```bash
scripts/dev harness scan
scripts/dev harness status
scripts/dev harness search indexed_files
```

`scan` / `status` / `search` default `PATH` to the current working directory. `scripts/dev` runs them with cwd set to the repository root.

Foreground daemon (optional; not required after autostart works):

```bash
scripts/dev harnessd serve
```

Stop with Ctrl+C, or stop a background/autostarted isolated daemon with:

```bash
scripts/dev stop
```

`stop` only targets the Unix socket under `.harness/runtime/`. It does not signal a system daemon on the canonical per-user socket.

## 4. Quality gate

`scripts/dev quality` puts the resolved `uv 0.12.5` first on `PATH` so the quality gate's `uv lock --check` does not pick up an older system `uv`.

```bash
scripts/dev quality
```

Equivalent without the wrapper, once `uv 0.12.5` and the project environment exist:

```bash
uv sync --locked --all-groups
uv run --frozen python scripts/quality.py
```

## 5. Prove the system install was not touched

1. If a system `harness` exists, record its database mtime (typically `~/.local/state/harness/harness.db`).
2. Run the commands in sections 2–3.
3. Confirm that mtime is unchanged and that `.harness/state/harness/harness.db` exists.
4. Confirm `command -v harness` (system) is not the executable used by `scripts/dev harness --version`.

`scripts/dev` must be used for development. A system `harness` on `PATH` without this environment still uses canonical per-user paths; that is expected. `uv run --frozen harness` from this checkout without sourcing `scripts/dev-env.sh` is the same mix: checkout code plus the global daemon, and must not be used here. A system `harness scan` of this source checkout is refused because the tracked overlay marks it as isolated-development only. Overlay detection requires `scripts/dev harness mcp` plus `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and no `HARNESS_HOST_PROFILE`; extra host JSON keys do not drop that isolation.

## Optional: source the environment

If you need `uv run` directly:

```bash
. scripts/dev-env.sh
uv run --frozen harness status
```

Sourcing must not change the current working directory. Path discovery uses `scripts/dev-env.sh`'s location, not the caller's. When `.venv/bin` exists, it is prepended to `PATH` so an unsuffixed `harness` in this shell is still the checkout binary with isolated XDG paths. `harness install` / `harness uninstall` remain refused.

With [direnv](https://direnv.net/), `.envrc` sources the same file. Run `direnv allow` once after cloning.

## Explicit `--database` / `--socket`

Those flags still bypass default path selection. Isolated development normally does not need them: the XDG overrides already separate state from a system install.

## MCP

This checkout commits host overlays that shadow a globally installed server named `harness`:

- Cursor: `.cursor/mcp.json` launches `${workspaceFolder}/scripts/dev harness mcp` and sets `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`.
- Claude Code: `.mcp.json` launches `./scripts/dev harness mcp`.

Those entries are intentionally not the production Cursor/Claude adapter signature (`python -m harness.mcp_process` plus `HARNESS_HOST_PROFILE`). Production install/scan/uninstall therefore leaves them alone. After changing Cursor MCP config, fully quit and reopen Cursor.

If a host still launches the user-global production server inside this checkout, that process lists no tools and refuses calls. Cursor's documented root hint is `HARNESS_WORKSPACE_ROOT`; Claude Code's is `CLAUDE_PROJECT_DIR`. Process cwd is used when that hint is absent or unresolvable (the global Cursor user-server case) and is not Workspace identity. The tracked overlay without `HARNESS_HOST_PROFILE` still serves the five tools against `.harness/`. This refuse does not activate when the host starts the user-global server with a cwd outside the overlay worktree.

The overlay inherits `scripts/dev` XDG paths, so agents in this repository talk to the checkout daemon under `.harness/`, not `~/.local/state/harness`. Isolated `scripts/dev harness scan` of this source tree indexes the checkout and skips host/skill reconciliation so it cannot project global skills or rewrite the overlay. A system `harness scan` of this tree is refused.

Do not run `scripts/dev harness install` or `scripts/dev harness uninstall`. Those commands would rewrite user-global host MCP using checkout code; the CLI refuses them while `HARNESS_DEV_ROOT` is set.

Manual equivalent of the Cursor overlay:

```json
{
  "command": "${workspaceFolder}/scripts/dev",
  "args": ["harness", "mcp"],
  "env": {
    "HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"
  }
}
```
