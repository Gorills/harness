# Isolated development and testing

Harness is one global install per user with one canonical daemon. By default a checkout does not
share that process, database, or Unix socket with a separately installed build. ADR-0036 adds an
explicit global-dogfood route; it never changes the default isolated test runtime.

Isolation is done by overriding the XDG bases from [ADR-0007](../decisions/0007-canonical-posix-runtime-paths.md). The product still resolves `$XDG_STATE_HOME/harness/harness.db` and `$XDG_RUNTIME_DIR/harness/harness.sock`; only the bases change.

## What is isolated

| Concern | System install | This checkout (`scripts/dev`) |
| --- | --- | --- |
| CLI / package | `harness` on `PATH` | `uv run --frozen` against this repository |
| Database | `~/.local/state/harness/harness.db` | `.harness/state/harness/harness.db` |
| Socket | `$XDG_RUNTIME_DIR/harness/harness.sock` or `/tmp/harness-<uid>/harness.sock` | `.harness/runtime/harness/harness.sock` |
| Autostart | `python -m harness.daemon_process` of the invoked interpreter | same module from this checkout's environment |
| Host MCP | project-only Codex/Cursor config | tracked Cursor router overlay plus generated private Codex HTTP config; isolated by default |
| Skill registry | `~/.harness/skills` | `.harness/skills` via `HARNESS_SKILL_REGISTRY` |
| Project skill profiles | installed host intent | `codex,cursor` via `HARNESS_DEV_SKILL_PROFILES` |
| `install` / `uninstall` | mutates user-global host MCP | refused while `HARNESS_DEV_ROOT` is set |

`.harness/` is gitignored. It may also hold a local `uv` bootstrap under `.harness/tools/` and
the checkout-local uv cache under `.harness/uv-cache/`. Keeping `UV_CACHE_DIR` inside the writable
checkout lets Codex start the overlay when its sandbox exposes the user cache read-only.

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

`scripts/dev env` must print `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`, and `HARNESS_SKILL_REGISTRY` under this repository's `.harness/` directory, not `~/.local/state`, `/run/user/...`, or `~/.harness/skills`.

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

`scan` / `status` / `search` default `PATH` to the current working directory. `scripts/dev` runs them with cwd set to the repository root. The isolated dashboard listener starts with that daemon on `127.0.0.1:17374`. `scripts/dev harness dashboard` prints the current private URL. The same URL is also in `.harness/runtime/harness/dashboard.url` while the daemon is running.

The first isolated `scan` also reconciles the current built-in skill pack into the checkout-local
registry (the count is not an invariant; see
[ADR-0029](../decisions/0029-quality-discipline-verification-and-response-economy.md))
and projects only the relevant subset into `.agents/skills`. Codex and Cursor share that root, so
the default development profile set is `codex,cursor`. Claude Code is not a supported host.
Generated skills are Harness-owned and excluded through the
checkout's Git-local `info/exclude`, not `.gitignore`.

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

`scripts/dev` must be used for current-code development. A system `harness` on `PATH` without this environment still uses canonical per-user paths; that is expected. `uv run --frozen harness` from this checkout without sourcing `scripts/dev-env.sh` is the forbidden mix of checkout code plus the global daemon. A plain system `harness scan` of this source checkout is refused because the tracked overlay marks it as source-development only. Overlay detection accepts the current `scripts/dogfood mcp` router and the legacy `scripts/dev harness mcp` launch with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and no `HARNESS_HOST_PROFILE`; extra host JSON keys do not drop that classification.

## 6. Opt-in global dogfood

The tracked Cursor project MCP entry launches `scripts/dogfood`. Codex uses a generated
authenticated HTTP entry. With no marker,
that router delegates to `scripts/dev harness`, so a fresh checkout remains isolated. After the
exact tool-installed package has been refreshed through an explicitly authorized acceptance or
installation path, inspect and enable the global route with:

```bash
scripts/dogfood mode
scripts/dogfood enable-global
```

Enable first runs the tool-installed command
`harness scan --global-dogfood <checkout>`. That special scan is accepted only for the detected
source overlay, outside `HARNESS_DEV_ROOT`, using an interpreter outside this checkout. It registers
and indexes a Normal checkout in the selected global daemon but returns before host configuration
or skill projection, so tracked MCP files and `.agents/skills` are not rewritten. Hidden activation
is refused because host policy is not reconciled. Only after that scan succeeds does the router
atomically write the exact opt-in marker under ignored `.harness/`.

After restarting the active host, its one source-checkout MCP server uses the tool-installed
Harness and canonical Project/Task/Knowledge/index state. The same route is available to terminal
commands:

```bash
scripts/dogfood status
scripts/dogfood search "workspace resolution"
scripts/dogfood scan
scripts/dogfood dashboard
```

Return to isolated MCP/CLI state with:

```bash
scripts/dogfood disable-global
```

Disabling removes only the local route marker and preserves global Project, Workspace, Task,
Knowledge, and index data. Restart the host after either transition. Invalid, non-regular, or
symlinked marker state fails closed. The router never forwards `install` or `uninstall`; global
package replacement and live host activation retain their separate authorization boundary. It also
refuses global-dogfood `visibility` changes; disable the route before changing checkout visibility.

Global dogfood is not a substitute for testing current checkout code: it intentionally runs the
installed executable. Use `scripts/dev` and the quality gate for current-code proof, and use
machine acceptance for the exact installed wheel.

## 7. Machine acceptance and user-global activation

Isolated `scripts/dev harness install` is refused on purpose. Synthetic machine acceptance is a
separate, explicitly authorized lane:

```bash
make accept-global-codex
```

This replaces only the user-global uv-tool package, then runs that installed executable with
temporary XDG state/runtime, Harness skills, Codex home/trust, and Git Workspaces. Test Projects
never enter the canonical database. Cleanup runs after success and failure, and the target verifies
that the user Codex config is byte-unchanged. Agents may run this target outside the sandbox only
after the user explicitly requests global installation or real-host acceptance.

Live activation is deliberately separate. To update host MCP, an operator—or an explicitly
authorized agent—runs:

```bash
make install-global
make install-global HOST=codex
make doctor-global
```

`make install-global` is `scripts/install-global`: it unsets `HARNESS_DEV_ROOT`, restores pre-overlay `XDG_STATE_HOME` / `XDG_RUNTIME_DIR` from `HARNESS_DEV_SAVED_XDG_STATE_HOME` and `HARNESS_DEV_SAVED_XDG_RUNTIME_DIR` (saved by `scripts/dev-env.sh` so the user-global daemon stays on `/run/user/<uid>` rather than falling back to `/tmp`), drops checkout `.venv/bin` from `PATH`, reinstalls with `uv tool install --force --reinstall --python 3.13 .` using uv 0.12.5 (the package version stays `0.1.0.dev0`, so `--reinstall` is required), then runs that tool-installed `harness install --host cursor` by default. `HOST=codex` and
`HOST=cursor,codex` select those profiles; explicitly authorized agents must always name the
profile set. Codex reconciliation writes the daemon HTTP URL, private bearer capability, and exact
Workspace root into the ignored project config. `scripts/install-global --package-only` is reserved for the acceptance target and never runs host lifecycle or doctor. It never uses `scripts/dev` or `.venv/bin/harness`. After MCP changes, restart the affected host.

This repository's tracked `.cursor/mcp.json` is the intended source-checkout overlay
(`harness-dev` launching `scripts/dogfood mcp`). Production Cursor MCP is project-only and never
rewrites it. Checkout agents must not call leftover `user-harness`; the router is the only supported
global-dogfood surface. Enable `harness-dev` in Cursor Customize for this checkout. Production MCP
with an interpolated overlay root lists no tools, including when `WORKSPACE_FOLDER_PATHS` names a
working repository. After live `make install-global`, the installed Harness migrates leftover
`user-harness`, re-approves project configs, and does not touch checkout `.harness/`. Synthetic
machine acceptance uses temporary Git Workspaces outside this checkout. An explicitly authorized
agent may activate one named live profile only after isolated machine acceptance succeeds.

Codex uses a locally generated, ignored, mode-`0600` `.codex/config.toml`. It connects directly to
the daemon-owned authenticated Streamable HTTP endpoint and carries the exact absolute Workspace
root. The repository tracks only `.codex/config.toml.example`; it cannot track the real config
because that file contains the private loopback capability. For global dogfood run
`make install-global HOST=codex`; for an isolated acceptance fixture use the Codex installation
flow under isolated XDG state. Trust remains a Codex/operator decision, and Codex must be fully
restarted after config or dogfood mode changes.

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

This checkout commits a Cursor stdio overlay. Codex uses the generated production-shaped HTTP config:

- Cursor: `.cursor/mcp.json` names `harness-dev` and launches `${workspaceFolder}/scripts/dogfood mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`.
- Codex: `.codex/config.toml` is generated locally with the daemon HTTP URL, bearer capability,
  and exact absolute `X-Harness-Workspace-Root`.

Production install/scan/uninstall leaves the tracked Cursor source-checkout overlay alone and reconciles the ignored Codex HTTP config. Cursor overlay detection matches that launch under `harness-dev` or the previous `harness` name; extra host JSON keys do not drop isolation. After changing either host config, fully restart the affected Codex or Cursor client.

Global Cursor leftover `user-harness` is profile-scoped and does not set `HARNESS_WORKSPACE_ROOT`. Cursor IDE still injects `WORKSPACE_FOLDER_PATHS` on that process; production MCP does not use it as Workspace identity. Isolated `scripts/dev harness doctor` does not inspect user-global Cursor MCP or `~/.harness/skills`. A production process whose interpolated overlay root is this checkout lists no tools even when `WORKSPACE_FOLDER_PATHS` names a working repository. A leftover `HARNESS_HOST_PROFILE=claude-code` process still refuses tools when `CLAUDE_PROJECT_DIR` is this checkout. Cursor-profile overlay refuse does not use process cwd. Codex identity is the authenticated HTTP request header and never process cwd.

In the default mode the router inherits `scripts/dev` XDG paths, so agents in this repository talk
to the checkout daemon under `.harness/`, not `~/.local/state/harness`. Isolated
`scripts/dev harness scan` of this source tree indexes the checkout, seeds the local registry, and
reconciles only the configured development skill profiles. It still skips production
host-configuration reconciliation, so it cannot project global skills or rewrite the overlay. A
plain system `harness scan` of this tree is refused; only the installed
`scan --global-dogfood` index-only path may register it globally.

Do not run `scripts/dev harness install` or `scripts/dev harness uninstall`. Those commands would rewrite user-global host MCP using checkout code; the CLI refuses them while `HARNESS_DEV_ROOT` is set.

Manual equivalent of the Cursor overlay:

```json
{
  "mcpServers": {
    "harness-dev": {
      "command": "${workspaceFolder}/scripts/dogfood",
      "args": ["mcp"],
      "env": {
        "HARNESS_WORKSPACE_ROOT": "${workspaceFolder}"
      }
    }
  }
}
```
