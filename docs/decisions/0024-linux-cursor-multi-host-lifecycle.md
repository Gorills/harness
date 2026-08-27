# ADR-0024: Linux Cursor MCP integration and multi-host lifecycle

- **Status:** Accepted
- **Date:** 2026-08-26
- **Amended:** 2026-08-27
- **Deciders:** Repository architecture baseline

## Context

The Linux release path previously supported Claude Code only even though the generic MCP bridge and skill planner were host-neutral and Cursor skill visibility was already modeled. Cursor's local IDE and CLI share the same official JSON MCP configuration, but Cursor does not document a `CLAUDE_PROJECT_DIR` equivalent for global stdio servers. Project `.cursor/mcp.json` configuration does document `${workspaceFolder}`, and a project server with the same name takes precedence over the global server. A production adapter therefore needs both a global registration and a complete per-Workspace override so Workspace identity never depends on process cwd.

Cursor also scans Claude and Codex compatibility skill roots. Installing Claude and Cursor independently would therefore risk duplicate Harness skills unless all active host profiles are reconciled as one visibility graph. Install/uninstall and doctor must likewise treat the profiles independently while preserving one shared daemon and Project Intelligence store.

Official Cursor MCP, CLI, and Skills documentation was re-checked on 2026-08-26 before this decision.

## Decision

Harness supports local Linux Cursor IDE and Cursor CLI with one `CursorAdapter` over Cursor's documented JSON configuration. The global registration is `~/.cursor/mcp.json` under `mcpServers.harness` and launches the exact installed Python with `-m harness.mcp_process` plus `HARNESS_HOST_PROFILE=cursor`. For every registered Harness Workspace, the adapter reconciles a complete `.cursor/mcp.json` project override for the same server name. Because project configuration shadows the global server, the override repeats type, command, args, and host profile and additionally sets `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`.

When `HARNESS_HOST_PROFILE=cursor` is present in the MCP bridge process, `HARNESS_WORKSPACE_ROOT` is mandatory and becomes one exact `ROOT` Workspace hint. The bridge does not fall back to process cwd and does not accept another host's root signal. This makes linked worktrees resolve to separate Workspace IDs while retaining their shared Project identity.

Cursor JSON mutation is ownership-aware and fail-closed. Harness preserves unknown top-level configuration and unknown MCP servers, classifies only an entry carrying the exact Harness stdio signature/profile marker as owned, and refuses a same-name foreign entry. Existing files are revalidated immediately before mutation and replaced through atomic no-clobber filesystem operations with recovery copies when a concurrent state change prevents safe completion. Global uninstall removes only `mcpServers.harness`; it never deletes the surrounding global config because file ownership cannot be proven across process lifetimes.

For project config, an untracked pre-existing file is preserved and only the Harness entry is changed. A Git-tracked `.cursor/mcp.json` is never automatically dirtied: an exact current Harness entry is accepted as manual adoption, while any required install/update/removal fails with an actionable manual-adoption/manual-removal error. When Harness creates a project config from absence, it writes a Workspace-local ownership marker and maintains exact Harness-owned entries in Git `info/exclude`; this marker, not the shared exclude file, is the proof that the config container may be deleted when it becomes empty. Linked worktrees share one Git common `info/exclude`, so each Workspace marker records whether that Workspace's creation transaction introduced the shared exclude block. Unknown or ambiguous exclude content is preserved.

The Harness source checkout is a special tracked overlay: `.cursor/mcp.json` launches `${workspaceFolder}/scripts/dev harness mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and without `HARNESS_HOST_PROFILE`. Extra JSON keys on that entry do not drop isolation. That entry shadows a global server named `harness` so agents developing Harness do not attach to a separately installed daemon. Production Cursor install/scan/uninstall must not rewrite or delete it. A system `harness scan` of that checkout is refused unless `HARNESS_DEV_ROOT` matches the overlay root; isolated `scripts/dev harness scan` may index the checkout but skips host/skill reconciliation. Isolated `harness install`/`uninstall` are refused because they would mutate user-global host MCP.

The CLI accepts `harness install --host claude-code|cursor|all` and the same selection on `harness uninstall`. Omitting `--host` remains the previous Claude Code behavior and must not change. The primary Linux local close-out on a real workstation is Cursor (`harness install --host cursor`); Claude Code and explicit `all` stay implemented rather than the first profile this path must close. Explicit `all` requires every selected host to be safely inspectable before registration mutation. Cursor installation updates the global registration and every already registered Workspace override, including after reinstall from a different Python environment. `scan` inspects all current supported Harness registrations, reconciles the scanned Cursor project override when Cursor is active, and submits one combined host-profile set to the daemon's existing skill projection path.

Uninstall preserves other active hosts. If profiles remain, Harness reconciles every registered Workspace's skills against the remaining profile set and leaves the shared daemon running. If no supported host remains, it performs owned skill cleanup and clean daemon shutdown as before. `--purge` is refused while another supported host remains active. Cursor project overrides are removed before its global entry so a failed project cleanup cannot silently strand a shadowing project registration after global removal.

Bare `harness doctor` reports Claude and Cursor MCP state separately. When Cursor is active it additionally checks every bounded live Workspace project override against the exact `${workspaceFolder}` root contract. Generated-skill inspection uses the union of current supported host profiles, so a healthy Claude profile cannot mask a broken Cursor override or duplicate-visible skill state and vice versa.

Cursor Cloud Agents are explicitly outside this local adapter contract. Cloud/team MCP distribution uses separate Cursor cloud/dashboard/API configuration and must be accepted as a different host profile before Harness relies on it.

Claude Code 2.1.109 prints `No MCP server found with name: harness` (unquoted) on stderr for a missing user server; `ClaudeCodeAdapter._run` merges stderr into stdout. Both that unquoted text and the older quoted form classify as `ABSENT`. Claude CLI output drift of that absent-server sentence is not a Cursor doctor/install/uninstall/scan blocker. Unexpected nonzero `claude mcp get` results remain fail-closed (`HostIntegrationError`, doctor FAIL). True absence remains doctor WARN.

Checkout development and `uv tool install` of this repository require `uv` 0.12.5 (`scripts/dev` bootstraps `.harness/tools/uv` when PATH uv is older). System uv 0.12.1 cannot install this repo.

## Consequences

- Cursor IDE and CLI use the same local adapter/configuration path and resolve Workspaces without cwd guessing.
- Claude Code and Cursor can coexist over one Harness daemon, Project registry, Task/Knowledge state, and minimal duplicate-free skill projection.
- Project override upgrades follow the installed Python interpreter, so a stale project shadow cannot defeat a refreshed global registration.
- Tracked team Cursor config is never rewritten automatically; teams can opt into an exact manually adopted Harness entry.
- The Harness source checkout keeps a tracked isolated-development Cursor overlay that shadows the global `harness` server and is not rewritten by production adapters.
- Isolated development refuses `harness install`/`uninstall` while `HARNESS_DEV_ROOT` is set, and refuses a system `harness scan` of that overlay root.
- Cursor is the primary Linux local profile to close; omitted `--host` remains `claude-code`.
- Quoted and unquoted Claude absent-server `mcp get` text both classify as absent, so current Claude CLI output does not fail Cursor lifecycle commands.
- Global Cursor config files may remain as an empty `mcpServers` object after uninstall. Preserving an unowned container is safer than guessing file ownership.
- Local automated acceptance proves the adapter and real MCP subprocess contract, but proprietary Cursor UI/CLI discovery still remains part of the external real-host acceptance matrix.

## Verification

Automated tests and installed-wheel smoke must prove:

- global Cursor registration ownership, idempotence, stale-owned update, foreign collision refusal, and user-config preservation;
- project override creation with the exact `${workspaceFolder}` environment contract and no cwd fallback;
- tracked project config manual adoption/removal behavior with no dirty Git diff;
- Harness-created project config remains ignored without modifying `.gitignore`, including linked worktrees sharing `info/exclude`;
- two linked Workspaces receive distinct project overrides and resolve to distinct Workspace IDs;
- Claude + Cursor scan produces one compatible skill projection rather than duplicate visible copies;
- a real Harness MCP subprocess starts a Task under `claude-code`, continues it under `cursor`, retrieves Task/Knowledge state, switches back to Claude, and does not mix current Task or code/index state between two Workspaces;
- `harness doctor` reports separate Claude/Cursor registrations plus current Cursor project overrides;
- tracked isolated-development overlay is left unchanged by reconcile/remove and does not fail doctor;
- a system `harness scan` of that overlay root is refused, while isolated `scripts/dev harness scan` skips host/skill reconciliation;
- isolated `harness install`/`uninstall` are refused while `HARNESS_DEV_ROOT` is set;
- Claude `mcp get` absent-server text is `ABSENT` whether or not the server name is quoted, and any other nonzero get remains fail-closed;
- installed-wheel lifecycle covers Claude install, Cursor install, two Workspace scans, second-venv upgrade of global and project registrations, cross-host MCP continuity, Cursor-only uninstall with Claude still healthy, Cursor reinstall, and uninstall-all/purge.
