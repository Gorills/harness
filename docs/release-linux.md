# Linux local release and acceptance

The primary Linux local close-out is Cursor IDE/CLI in Normal mode (`harness install --host cursor`). Local Codex project MCP is also implemented; omitted `--host` selects Cursor. `--host all` installs the Codex+Cursor pair. Claude Code is no longer a supported Harness host ([ADR-0039](decisions/0039-retire-claude-code-host.md)). Codex/Cursor proprietary-host acceptance remains open.

## Prerequisites

- Linux with a current Git executable.
- Python 3.13.
- `uv` 0.12.5 for the repository installation path (`scripts/dev` bootstraps `.harness/tools/uv` in a checkout). System `uv` 0.12.1 cannot `uv tool install` this repository.
- Codex CLI on `PATH` for Codex install (cleanup does not require it). Cursor local config does not require a Cursor executable for ownership checks.
- SQLite in the selected Python runtime with FTS5.

## Install from this repository

```bash
git clone https://github.com/Gorills/harness.git
cd harness
uv tool install --python 3.13 .
harness install --host cursor
harness doctor
```

Omitted `--host` installs Cursor. Codex is `harness install --host codex`. `--host all` installs both. Claude Code is not a supported host ([ADR-0039](decisions/0039-retire-claude-code-host.md)).

Register and index each Git worktree explicitly:

```bash
cd /path/to/repository
harness scan
# After Harness changes Cursor MCP config, fully quit and reopen Cursor.
agent mcp list
harness status
```

Cursor's current MCP documentation requires restarting Cursor after changing `mcp.json`. Harness prints this reminder after any actual Cursor MCP config mutation. When `agent` is installed, `harness install --host cursor` and `harness scan` run `agent mcp enable harness` and verify `agent mcp list-tools harness`. Registered Workspace roots that cannot be resolved as directories are skipped and named; they do not fail install or uninstall. Missing `agent` prints `cd <workspace> && agent mcp enable harness` plus a full Cursor quit/reopen (window reload is not enough). Leftover `user-harness` is not Workspace identity and is removed. Do not hardcode a Workspace path in `mcp.json`; doctor would mark that config stale.

`harness scan` reconciles all current supported host profiles together. When Cursor host integration is active it also creates/updates the Workspace `.cursor/mcp.json` override carrying `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and enable/verifies that project MCP. Codex and Cursor therefore share one generated `.agents/skills` projection. `harness skills list` shows the canonical skill registry without changing projects.

When Codex intent is active, scan creates/updates only the ignored marker-owned project
`.codex/config.toml`, with the authenticated daemon Streamable HTTP URL and exact absolute
`X-Harness-Workspace-Root`. Harness never writes project trust or
`~/.codex/config.toml`. Restart Codex and verify from the trusted Workspace with
`codex mcp get harness --json`. For Hidden Projects the same marker-owned config carries exact
`developer_instructions`; install and transitions preserve user `AGENTS.md`. Doctor reports that
this is hygiene-effective policy and does not claim Codex host-blocks Git or pull requests.

## Upgrade or reinstall

```bash
cd /path/to/harness

git pull
uv tool install --force --python 3.13 .
harness install --host codex # repeat for each compatible active profile
harness doctor
# If Cursor config changed, fully quit/reopen Cursor, then:
agent mcp list
```

From a Harness source checkout the same refresh is `make install-global`, `make install-global HOST=cursor`, `make install-global HOST=codex`, or `make install-global HOST=all`. Run the helper once per compatible active profile. It leaves isolated-development overlay environment first and uses `--force --reinstall` because the development version stays `0.1.0.dev0`. Do not run it through `scripts/dev`.

The post-upgrade profile install is required. It verifies the frozen daemon identity before updating registrations and every owned project config to the selected daemon endpoint and capability. `--host all` is valid for the Codex+Cursor pair. Installed-wheel smoke proves Cursor/Codex refresh across runtime upgrades and independent/linked Workspaces.

## Uninstall

```bash
harness uninstall --host cursor
```

The no-argument form uninstalls Cursor. Select any profile or every owned profile explicitly:

```bash
harness uninstall
harness uninstall --host all
```

Partial uninstall preserves other active supported hosts, reconciles generated skills for the remaining profile set, and leaves the shared daemon running. To explicitly remove the canonical database and canonical external skill registry after removing the last supported host:

```bash
harness uninstall --host all --purge
```

Purge is refused while another supported host remains active and is fail-closed around canonical filesystem ownership/type checks and database singleton locking. Unknown files outside explicit Harness roots are preserved. Tracked Cursor/Codex project config is manual-adoption/removal only.

## Doctor interpretation

Bare `harness doctor` is read-only and operational. `OK` means the inspected invariant holds, `WARN` means absent/lazy/stale-but-non-destructive state that may need attention, and `FAIL` means an integrity, ownership, compatibility, or runtime mismatch. Any `FAIL` makes the command exit nonzero; warnings alone do not. Project/index/skill checks use one SQLite read snapshot. A quiescent WAL database is opened immutably so doctor does not create `-wal`/`-shm` files merely by inspecting it; an existing live WAL is still read through SQLite's normal read-only WAL path so uncheckpointed durable frames remain visible.

For Cursor, doctor reports leftover/foreign/absent global `user-harness` separately from on-disk project configs and from Cursor approval/tool catalog. A leftover owned or foreign global `harness` is FAIL. Missing `agent` is WARN with `cd <workspace> && agent mcp enable harness`, not a blanket OK. When `agent` is present, a project without the exact five tools is FAIL for that Workspace. After correcting a Cursor MCP problem, run `harness install --host cursor`, fully quit/reopen Cursor, then inspect with `agent mcp list-tools harness`. Cursor's MCP Logs in the Output panel are the next host-side diagnostic when the server still does not start. Isolated `scripts/dev harness doctor` does not inspect user-global Cursor MCP or `~/.harness/skills`.

For Codex, doctor reports CLI discovery, Harness-owned intent, and each registered Workspace project
config separately, including expected/configured HTTP endpoint, absolute root header, and ownership/preflight
state. It does not claim project trust or connected proprietary-client tools from on-disk TOML.

Index state and Generated skills report `timed out` or `failed` for named Workspaces when live inspection hits the doctor deadline or raises an inspection error. `unavailable` is reserved for Project Git/identity inspection failure of a named Workspace, not for a timeout. Workspaces skipped by the count limit or aggregate time budget are named as `not inspected (doctor budget)`. Timeout and budget-truncation warnings do not fail the command; identity mismatches and other integrity failures still do.

Use `harness doctor --runtime-only` for the old ephemeral SQLite/FTS5 probe and `harness doctor --database PATH` for read-only inspection of one explicitly selected initialized database.

## Automated release gate

`scripts/quality.py` remains the exact-head repository gate. Its installed-wheel smoke installs Codex against an existing Cursor Hidden Project without changing `AGENTS.md`, restores Normal, refreshes owned Codex configs across Python environments and independent/linked Workspaces, and verifies Cursor → Codex Task continuity, doctor, partial cleanup, Cursor lifecycle, uninstall-all, and purge.

## Explicitly authorized Cursor refresh

Checkout agents ordinarily remain isolated. After explicit user authorization they may first run
`make accept-global-codex`, which replaces only the global package and tests it against temporary
Harness/Codex/Workspace state. Live activation still requires
`make install-global HOST=<profile>`. After this project-only Cursor change lands, an operator
should:

1. Run `make install-global` from the Harness checkout; repeat with `HOST=codex` or `HOST=all` for each compatible active profile.
2. Confirm leftover `~/.cursor/mcp.json` `mcpServers.harness` is absent.
3. Fully quit and reopen Cursor.
4. In two working repositories at once (for example Alia and Mangazeya), confirm `agent mcp list-tools harness` shows the five tools and that `project_status` roots/tasks are distinct.
5. In the Harness source checkout, confirm the single generated `harness` HTTP server resolves the checkout Workspace and no legacy Codex `harness-dev` server remains.

## Explicitly authorized Codex acceptance

Ordinary checkout work must not mutate the user-global installation or Codex trust. The automated
preflight below uses two temporary Git Workspaces and temporary trust. An agent may run its
`--global-install` form outside the sandbox only after explicit user authorization; live project
trust and UI restart remain operator-controlled:

For the CLI half, the repository provides an opt-in isolated runner. First inspect its disclosure
without using a model:

```bash
scripts/dev python scripts/accept_codex.py
```

Then run the local-only preflight, which does not contact the model service:

```bash
scripts/dev python scripts/accept_codex.py --preflight-only \
  --evidence /tmp/harness-codex-cli-preflight.json
```

To prove the user-global uv-tool executable without mixing test Projects into canonical state:

```bash
make accept-global-codex
```

After the operator explicitly approves the stated OpenAI destination, fixture/MCP payload, and
account usage, provide `CODEX_API_KEY` to this invocation through a secure shell/secret mechanism
(never paste it into chat or commit it), then run:

```bash
scripts/dev python scripts/accept_codex.py --run-model \
  --evidence /tmp/harness-codex-cli-acceptance.json
```

To classify existing Codex `exec --json` JSONL for search-vs-native-grep behavior without a model
or daemon, pass `--workspace-root` so absolute `rg`/`cat` paths under that root classify as
repo-relative (`accept_codex --run-model` uses the primary Workspace the same way). The CLI writes
the full classifier payload including redacted evidence; `accept_codex --run-model` attaches
metrics-only `search_behavior` (no evidence/argv) to its report:

```bash
scripts/dev python scripts/eval_search_behavior.py events.jsonl \
  --workspace-root /path/to/workspace \
  --output /tmp/harness-search-behavior.json
```

The runner builds and installs the exact current wheel under one temporary directory, uses two
temporary Git Workspaces containing only fixture README/pyproject text, isolates Harness state and
skills, and uses the official MCP SDK to connect to the exact configured Streamable HTTP endpoint and call all five
tools during local-only preflight. Preflight also writes a temporary user-owned
`acceptance-skill` (and a projected negative sibling) into the isolated registry, projects them into
each fixture `.agents/skills`, and proves the runtime nonce lives in the `SKILL.md` body rather than
description/frontmatter or the positive prompt. It does not contact the model;
`skill_read_verified` and `skill_negative_verified` remain false. Model mode additionally requires
the real Codex CLI to select and complete all five MCP calls from JSONL evidence, and additionally
proves native skill discovery → description relevance → `SKILL.md` body read by requiring the
hidden nonce in `skill_marker` / JSONL for the matching synthetic-acceptance prompt, plus a second
exec whose unmatched prompt must not return the negative nonce. Both modes verify schemas, distinct simultaneous
Workspace identities, the exact relevant/no irrelevant generated skill set, doctor, and owned
cleanup, and fail if `~/.codex/config.toml` changes. The runner gives Codex project
trust only through a temporary `CODEX_HOME`, does not use saved Codex authentication, and removes
that temporary state after the run. It does not prove Codex IDE, ChatGPT desktop, Cursor, or other
proprietary UI behavior; those remain open host-compatibility matrix items.

1. Run `make install-global HOST=codex`, then `harness scan` in each Workspace.
2. Trust each Workspace through Codex's own UI, fully quit and reopen the Codex client under test,
   and create a new Task; an existing Task keeps its original instruction snapshot.
3. From each Workspace root, require `codex mcp get harness --json` to show the loopback
   Streamable HTTP URL, bearer authorization, and exact absolute
   `X-Harness-Workspace-Root`. Confirm no user-level `~/.codex/config.toml` Harness server was
   added and no capability appears in logs or acceptance evidence.
4. In each fresh Codex Task, confirm `project_status` is the first project action (before shell
   commands, repository reads/searches, browser inspection, or changes; only Harness tool
   discovery may precede it); then call all five Harness tools and confirm it returns
   the correct distinct Workspace ID/root for simultaneous repositories and linked worktrees.
5. Start/checkpoint a Task and Knowledge in one Codex process; restart Codex and then switch to a
   supported second host. Require the same Task ID/revision and Knowledge to remain available.
6. Repeat discovery/tool/root checks in the current Codex IDE extension and ChatGPT desktop local
   Codex client. Confirm relevant `.agents/skills` appear exactly once and irrelevant skills do not
   appear.
7. Run `harness uninstall --host codex`; after restart, confirm only marker-owned project config
   and generated Codex-only projections were removed, user files/trust/other active hosts remain,
   and `harness doctor` has no FAIL results.

Record exact client versions and results in `docs/host-compatibility.md`; until CLI model, IDE, and
desktop checks are recorded, proprietary Codex acceptance remains open. Automated Codex CLI
preflight/model proof does not complete that matrix.

## Proprietary host acceptance still required

Automation does not prove vendor UI/runtime behavior. Before calling Codex CLI/IDE/desktop or Cursor IDE/CLI accepted, complete the matrix in `docs/host-compatibility.md`: discovery, five tools, exact Workspace identity, restart/cross-host continuity, skill de-duplication, trust/approval behavior, and owned cleanup. Codex cloud and Cursor Cloud Agents remain separate profiles.
