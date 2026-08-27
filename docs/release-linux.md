# Linux local release and acceptance

The primary Linux local close-out is Cursor IDE/CLI in Normal mode (`harness install --host cursor`). Claude Code and `--host all` remain implemented. Omitting `--host` still selects `claude-code` for compatibility. The package still carries a development version until proprietary-host acceptance is completed; that Cursor UI/CLI matrix stays unchecked.

## Prerequisites

- Linux with a current Git executable.
- Python 3.13.
- `uv` 0.12.5 for the repository installation path (`scripts/dev` bootstraps `.harness/tools/uv` in a checkout). System `uv` 0.12.1 cannot `uv tool install` this repository.
- Claude Code CLI on `PATH` when installing/uninstalling the Claude profile. Cursor local config does not require a Cursor executable for JSON ownership checks. Current Claude `mcp get` absent-server text (quoted or unquoted) is treated as absent, so that CLI output drift is not a Cursor doctor/install/uninstall/scan blocker.
- SQLite in the selected Python runtime with FTS5.

## Install from this repository

```bash
git clone https://github.com/Gorills/harness.git
cd harness
uv tool install --python 3.13 .
harness install --host cursor
harness doctor
```

Claude Code remains available as `harness install` (omitted `--host`) or `harness install --host claude-code`. Install both implemented hosts with `harness install --host all`.

Register and index each Git worktree explicitly:

```bash
cd /path/to/repository
harness scan
# After Harness changes Cursor MCP config, fully quit and reopen Cursor.
# Enable the project harness MCP in Cursor Customize for this repository (once).
agent mcp list
harness status
```

Cursor's current MCP documentation requires restarting Cursor after changing `mcp.json`. Harness prints this reminder after any actual Cursor MCP config mutation. The Cursor CLI uses the same MCP configuration as the editor; when `agent` is installed, `agent mcp list` is a host-side inspection probe. Cursor IDE chat uses profile-scoped `user-harness`, which is not a Workspace server. After `harness install --host cursor`, enable the project `harness` MCP in Customize for each working repository and fully quit/reopen Cursor (window reload is not enough). Do not hardcode a Workspace path in `mcp.json`; doctor would mark that config stale.

`harness scan` reconciles all current supported host profiles together. When Cursor is active it also creates/updates the Workspace `.cursor/mcp.json` override carrying `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. Claude and Cursor therefore share one compatible generated skill projection instead of receiving duplicate copies through Cursor compatibility roots. `harness skills list` shows the canonical skill registry without changing projects.

## Upgrade or reinstall

```bash
cd /path/to/harness

git pull
uv tool install --force --python 3.13 .
harness install --host all   # use the profiles installed on this machine
harness doctor
# If Cursor config changed, fully quit/reopen Cursor, enable project harness MCP in Customize, then:
agent mcp list
```

From a Harness source checkout the same refresh is `make install-global` or `make install-global HOST=all`. That helper leaves isolated-development overlay environment first and uses `--force --reinstall` because the development version stays `0.1.0.dev0`. Do not run it through `scripts/dev`.

The post-upgrade `harness install` is required. It compares the running daemon's frozen schema/version/Python/code identity with the newly installed runtime and cleanly replaces a stale daemon before updating selected host registrations. Cursor global and every registered Workspace override are updated to the new interpreter together with Claude when `--host all` is selected. This covers both a changed virtual-environment path and an in-place reinstall at the same path. Upgrading from the previous Linux release is also explicit: that protocol-v1 daemon does not implement `runtime_diagnostics`, so Harness accepts only its structured unknown-method response, validates legacy `status`, requests the existing clean `shutdown`, then starts and verifies the current runtime. Other IPC/diagnostics failures are not treated as permission to kill or replace a daemon.

## Uninstall

```bash
harness uninstall --host cursor
```

The no-argument form preserves the previous Claude-only behavior. Select Claude Code or both hosts explicitly:

```bash
harness uninstall
harness uninstall --host all
```

Partial uninstall preserves other active supported hosts, reconciles generated skills for the remaining profile set, and leaves the shared daemon running. To explicitly remove the canonical database and canonical external skill registry after removing the last supported host:

```bash
harness uninstall --host all --purge
```

Purge is refused while another supported host remains active and is fail-closed around canonical filesystem ownership/type checks and database singleton locking. Unknown files outside the explicit Harness data roots are preserved. Tracked Cursor project config is never automatically dirtied; required tracked changes are manual-adoption/manual-removal operations.

## Doctor interpretation

Bare `harness doctor` is read-only and operational. `OK` means the inspected invariant holds, `WARN` means absent/lazy/stale-but-non-destructive state that may need attention, and `FAIL` means an integrity, ownership, compatibility, or runtime mismatch. Any `FAIL` makes the command exit nonzero; warnings alone do not. Project/index/skill checks use one SQLite read snapshot. A quiescent WAL database is opened immutably so doctor does not create `-wal`/`-shm` files merely by inspecting it; an existing live WAL is still read through SQLite's normal read-only WAL path so uncheckpointed durable frames remain visible.

For Cursor, doctor reports the configured and expected Python runtime and gives the exact project config path plus remediation for stale/foreign/orphaned overrides, a leftover `HARNESS_WORKSPACE_ROOT` on the global entry, wrong or missing `${workspaceFolder}` on project entries, tracked manual-adoption requirements, malformed ownership metadata, and other unsafe config states. It remains read-only. After correcting a Cursor MCP problem, run `harness install --host cursor`, fully quit/reopen Cursor, enable the project `harness` MCP in Customize, then inspect the host with `agent mcp list` when the CLI is available. Cursor's MCP Logs in the Output panel are the next host-side diagnostic when the server still does not start. An absent Claude MCP registration is a warning, not a Cursor failure.

Index state and Generated skills report `timed out` or `failed` for named Workspaces when live inspection hits the doctor deadline or raises an inspection error. `unavailable` is reserved for Project Git/identity inspection failure of a named Workspace, not for a timeout. Workspaces skipped by the count limit or aggregate time budget are named as `not inspected (doctor budget)`. Timeout and budget-truncation warnings do not fail the command; identity mismatches and other integrity failures still do.

Use `harness doctor --runtime-only` for the old ephemeral SQLite/FTS5 probe and `harness doctor --database PATH` for read-only inspection of one explicitly selected initialized database.

## Automated release gate

`scripts/quality.py` is the repository gate. Its installed-wheel smoke builds and installs the exact wheel, exercises shipping CLI/MCP/daemon behavior, installs the same wheel into a second isolated Python 3.13 environment, verifies stale-daemon replacement, upgrades Claude plus Cursor global/project registrations, scans a repository and linked worktree, requires one duplicate-free shared skill projection, runs real cross-host MCP Task/Knowledge continuity, checks Cursor-only uninstall with Claude still healthy, then verifies uninstall-all and purge.

## Proprietary host acceptance still required

Automation does not prove vendor UI/runtime behavior. Proprietary Cursor IDE/CLI acceptance is not done; before calling a specific Claude Code or Cursor build accepted, verify the matrix in `docs/host-compatibility.md`, including MCP discovery, five tool schemas, host-specific Workspace resolution, restart/cross-host continuity, generated skill visibility/de-duplication, and Harness-owned cleanup. Cursor CLI's shared config inspection commands are useful for that real-host path, but automation does not substitute for the proprietary host. Cursor Cloud Agents use separate cloud/team MCP configuration and are outside this local release profile.
