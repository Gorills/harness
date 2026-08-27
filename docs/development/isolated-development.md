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
scripts/dev harness dashboard
```

`scan` / `status` / `search` default `PATH` to the current working directory. `scripts/dev` runs them with cwd set to the repository root. The isolated dashboard listener starts with that daemon; `scripts/dev harness dashboard` prints the current private URL. The same URL is also in `.harness/runtime/harness/dashboard.url` while the daemon is running.

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

## 6. Refresh the user-global install (human-only)

Isolated `scripts/dev harness install` is refused on purpose. To copy this checkout into the user-global `uv tool` install and update host MCP, operators run:

```bash
make install-global
make install-global HOST=all
make doctor-global
```

`make install-global` is `scripts/install-global`: it unsets `HARNESS_DEV_ROOT`, restores pre-overlay `XDG_STATE_HOME` / `XDG_RUNTIME_DIR` from `HARNESS_DEV_SAVED_XDG_STATE_HOME` and `HARNESS_DEV_SAVED_XDG_RUNTIME_DIR` (saved by `scripts/dev-env.sh` so the user-global daemon stays on `/run/user/<uid>` rather than falling back to `/tmp`), drops checkout `.venv/bin` from `PATH`, reinstalls with `uv tool install --force --reinstall --python 3.13 .` using uv 0.12.5 (the package version stays `0.1.0.dev0`, so `--reinstall` is required), then runs that tool-installed `harness install --host cursor` by default. It never uses `scripts/dev` or `.venv/bin/harness`. After MCP changes, fully quit and reopen Cursor.

This repository's tracked `.cursor/mcp.json` is the intended checkout-local overlay (`harness-dev` launching `scripts/dev harness mcp`). Cursor IDE still connects global `user-harness` as the production server for other repositories; checkout agents must not call it. Enable `harness-dev` in Cursor Customize for this checkout. Production MCP with an interpolated overlay root and no non-overlay `WORKSPACE_FOLDER_PATHS` lists no tools. The global install is tested from another Git worktree, not from agents in this checkout. Checkout agents must not run `make install-global`.

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

This checkout commits host overlays that do not use the production Cursor/Claude adapter signature (`python -m harness.mcp_process` plus `HARNESS_HOST_PROFILE`):

- Cursor: `.cursor/mcp.json` names `harness-dev` and launches `${workspaceFolder}/scripts/dev harness mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`.
- Claude Code: `.mcp.json` still names `harness` and launches `./scripts/dev harness mcp`.

Production install/scan/uninstall leaves those overlays alone. Overlay detection matches that launch under `harness-dev` or the previous `harness` name; extra host JSON keys do not drop isolation. After changing Cursor MCP config, fully quit and reopen Cursor.

Global Cursor `user-harness` is profile-scoped and does not set `HARNESS_WORKSPACE_ROOT`. Cursor IDE still injects `WORKSPACE_FOLDER_PATHS` on that process; production MCP uses that as the working-project Workspace when the interpolated Harness-owned root is absent or is this overlay. A production process whose interpolated `HARNESS_WORKSPACE_ROOT` is this overlay *and* that has no non-overlay folder-path lists no tools. Claude `CLAUDE_PROJECT_DIR` overlay refuse is unchanged. Cursor-profile overlay refuse does not use process cwd. The tracked overlay without `HARNESS_HOST_PROFILE` still serves the five tools against `.harness/` once Cursor has enabled `harness-dev`.

The overlay inherits `scripts/dev` XDG paths, so agents in this repository talk to the checkout daemon under `.harness/`, not `~/.local/state/harness`. Isolated `scripts/dev harness scan` of this source tree indexes the checkout and skips host/skill reconciliation so it cannot project global skills or rewrite the overlay. A system `harness scan` of this tree is refused.

Do not run `scripts/dev harness install` or `scripts/dev harness uninstall`. Those commands would rewrite user-global host MCP using checkout code; the CLI refuses them while `HARNESS_DEV_ROOT` is set.

Manual equivalent of the Cursor overlay:

```json
{
  "mcpServers": {
    "harness-dev": {
      "command": "${workspaceFolder}/scripts/dev",
      "args": ["harness", "mcp"],
      "env": {
        "HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```
