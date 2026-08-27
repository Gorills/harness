# ADR-0024: Linux Cursor MCP integration and multi-host lifecycle

- **Status:** Accepted
- **Date:** 2026-08-26
- **Amended:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Later amendment:** [ADR-0026](0026-cursor-project-scoped-workspace-mcp.md) supersedes owned global `user-harness`. Production Cursor MCP is project-only `.cursor/mcp.json` `harness`, enabled with official `agent mcp enable harness`. `WORKSPACE_FOLDER_PATHS` is not Workspace identity. Overlay-refuse on a Cursor-profile process applies only when interpolated `HARNESS_WORKSPACE_ROOT` is the overlay.

## Context

The Linux release path previously supported Claude Code only even though the generic MCP bridge and skill planner were host-neutral and Cursor skill visibility was already modeled. Cursor's local IDE and CLI share the same official JSON MCP configuration, but Cursor does not document a `CLAUDE_PROJECT_DIR` equivalent for global stdio servers. Project `.cursor/mcp.json` configuration does document `${workspaceFolder}`, and official merge notes say a same-name project server takes precedence over the global server. Real-host Cursor IDE evidence from 2026-08-27 is that the agent catalog still uses the user-level identifier (`user-harness`) unless the distinct project identifier is approved, so the connected server must itself carry an interpolatable `${workspaceFolder}`. A production adapter therefore needs both a global registration and a complete per-Workspace override so Workspace identity never depends on process cwd.

Cursor also scans Claude and Codex compatibility skill roots. Installing Claude and Cursor independently would therefore risk duplicate Harness skills unless all active host profiles are reconciled as one visibility graph. Install/uninstall and doctor must likewise treat the profiles independently while preserving one shared daemon and Project Intelligence store.

Official Cursor MCP, CLI, and Skills documentation was re-checked on 2026-08-26 before this decision.

## Decision

**Amendment 2026-08-27:** [ADR-0026](0026-cursor-project-scoped-workspace-mcp.md) supersedes putting `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` on the global `user-harness` process and using `WORKSPACE_FOLDER_PATHS` as Workspace identity. The Decision text below is historical; implement ADR-0026 for current Cursor Workspace identity.

Harness supports local Linux Cursor IDE and Cursor CLI with one `CursorAdapter` over Cursor's documented JSON configuration. The global registration is `~/.cursor/mcp.json` under `mcpServers.harness` and launches the exact installed Python with `-m harness.mcp_process` plus `HARNESS_HOST_PROFILE=cursor` and `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. For every registered Harness Workspace, the adapter reconciles a complete `.cursor/mcp.json` project override for the same server name. The override repeats type, command, args, host profile, and the same `${workspaceFolder}` root hint so a later-approved project identifier has the same contract.

When `HARNESS_HOST_PROFILE=cursor` is present in the MCP bridge process, `HARNESS_WORKSPACE_ROOT` is mandatory and becomes one exact `ROOT` Workspace hint. The bridge does not fall back to process cwd, does not treat an uninterpolated `${workspaceFolder}` literal as a root, and does not accept another host's root signal or undocumented inherited environment such as `WORKSPACE_FOLDER_PATHS`. This makes linked worktrees resolve to separate Workspace IDs while retaining their shared Project identity.

Real-host Cursor IDE evidence from 2026-08-27: the agent catalog uses the user-level server (`user-harness`). A same-name project override is a distinct identifier (`project-0-<folder>-harness`) and stays disconnected until Cursor records it in `approvedProjectMcpServers`; Customize often does not surface that project row. Cursor interpolates user-level `${workspaceFolder}` with `configurationResolverService.resolveAsync` against the current window's first workspace folder, not the directory that contains `~/.cursor/mcp.json`. Process cwd for that user-level stdio server is the user home and is not Workspace identity. Therefore both the global `~/.cursor/mcp.json` entry and each project override set `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. When that value is missing or still the literal `${workspaceFolder}`, production MCP lists no tools. Hardcoding a filesystem path is refused because doctor would classify that config as stale.

Cursor JSON mutation is ownership-aware and fail-closed. Harness preserves unknown top-level configuration and unknown MCP servers, classifies only an entry carrying the exact Harness stdio signature/profile marker as owned, and refuses a same-name foreign entry. Existing files are revalidated immediately before mutation and replaced through atomic no-clobber filesystem operations with recovery copies when a concurrent state change prevents safe completion. Global uninstall removes only `mcpServers.harness`; it never deletes the surrounding global config because file ownership cannot be proven across process lifetimes.

For project config, an untracked pre-existing file is preserved and only the Harness entry is changed. A Git-tracked `.cursor/mcp.json` is never automatically dirtied: an exact current Harness entry is accepted as manual adoption, while any required install/update/removal fails with an actionable manual-adoption/manual-removal error. When Harness creates a project config from absence, it writes a Workspace-local ownership marker and maintains exact Harness-owned entries in Git `info/exclude`; this marker, not the shared exclude file, is the proof that the config container may be deleted when it becomes empty. Linked worktrees share one Git common `info/exclude`, so each Workspace marker records whether that Workspace's creation transaction introduced the shared exclude block. Unknown or ambiguous exclude content is preserved.

The Harness source checkout is a special tracked overlay: `.cursor/mcp.json` launches `${workspaceFolder}/scripts/dev harness mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and without `HARNESS_HOST_PROFILE`. Extra JSON keys on that entry do not drop isolation. That overlay is the intended checkout-local server; Cursor IDE still connects global `user-harness` unless the project overlay is approved. Production MCP with `HARNESS_HOST_PROFILE` against this overlay lists no tools, so a user-level process whose interpolated root is the checkout also refuses. Production Cursor install/scan/uninstall must not rewrite or delete the overlay. A system `harness scan` of that checkout is refused unless `HARNESS_DEV_ROOT` matches the overlay root; isolated `scripts/dev harness scan` may index the checkout but skips host/skill reconciliation. Isolated `harness install`/`uninstall` are refused because they would mutate user-global host MCP.

A production MCP process (`HARNESS_HOST_PROFILE` set, typically `python -m harness.mcp_process`) must not expose tools when it is launched against that overlay checkout. The Cursor documented root hint is `HARNESS_WORKSPACE_ROOT`; Claude Code's is `CLAUDE_PROJECT_DIR`. Overlay detection consults process cwd when that hint is absent or does not resolve to an existing path; cwd is not Workspace identity. The tracked overlay without `HARNESS_HOST_PROFILE` still serves the five tools against checkout-local state once that project server is the process Cursor launched. If a host launches the user-global server with a documented root that is not the overlay worktree, this refuse does not activate.

The CLI accepts `harness install --host claude-code|cursor|all` and the same selection on `harness uninstall`. Omitting `--host` remains the previous Claude Code behavior and must not change. The primary Linux local close-out on a real workstation is Cursor (`harness install --host cursor`); Claude Code and explicit `all` stay implemented rather than the first profile this path must close. Explicit `all` requires every selected host to be safely inspectable before registration mutation. Cursor installation updates the global registration and every already registered Workspace override, including after reinstall from a different Python environment. `scan` inspects all current supported Harness registrations, reconciles the scanned Cursor project override when Cursor is active, and submits one combined host-profile set to the daemon's existing skill projection path.

Uninstall preserves other active hosts. If profiles remain, Harness reconciles every registered Workspace's skills against the remaining profile set and leaves the shared daemon running. If no supported host remains, it performs owned skill cleanup and clean daemon shutdown as before. `--purge` is refused while another supported host remains active. Cursor project overrides are removed before its global entry so a failed project cleanup cannot silently strand a shadowing project registration after global removal.

Bare `harness doctor` reports Claude and Cursor MCP state separately. When Cursor is active it additionally checks every bounded live Workspace project override against the exact `${workspaceFolder}` root contract. Generated-skill inspection uses the union of current supported host profiles, so a healthy Claude profile cannot mask a broken Cursor override or duplicate-visible skill state and vice versa.

Cursor Cloud Agents are explicitly outside this local adapter contract. Cloud/team MCP distribution uses separate Cursor cloud/dashboard/API configuration and must be accepted as a different host profile before Harness relies on it.

Claude Code 2.1.109 prints `No MCP server found with name: harness` (unquoted) on stderr for a missing user server; `ClaudeCodeAdapter._run` merges stderr into stdout. Both that unquoted text and the older quoted form classify as `ABSENT`. Claude CLI output drift of that absent-server sentence is not a Cursor doctor/install/uninstall/scan blocker. Unexpected nonzero `claude mcp get` results remain fail-closed (`HostIntegrationError`, doctor FAIL). True absence remains doctor WARN.

Checkout development and `uv tool install` of this repository require `uv` 0.12.5 (`scripts/dev` bootstraps `.harness/tools/uv` when PATH uv is older). System uv 0.12.1 cannot install this repo.

## Consequences

- Cursor IDE and CLI use the same local adapter/configuration path and resolve Workspaces without cwd guessing.
- **Superseded by ADR-0026:** production Cursor MCP is project-only. Owned global `user-harness` is removed. A Cursor-profile process without an interpolated project root lists no tools. Project MCP is enabled with official `agent mcp enable harness`.
- Claude Code and Cursor can coexist over one Harness daemon, Project registry, Task/Knowledge state, and minimal duplicate-free skill projection.
- Project override upgrades follow the installed Python interpreter, so a stale project shadow cannot defeat a refreshed install.
- Tracked team Cursor config is never rewritten automatically; teams can opt into an exact manually adopted Harness entry.
- The Harness source checkout keeps a tracked isolated-development Cursor overlay (`harness-dev`) that is the intended checkout-local server and is not rewritten or enabled as production `harness` by production adapters.
- Isolated development refuses `harness install`/`uninstall` while `HARNESS_DEV_ROOT` is set, and refuses a system `harness scan` of that overlay root.
- Production MCP with `HARNESS_HOST_PROFILE` lists no tools and refuses calls when launched against that overlay checkout; the `scripts/dev` overlay without the profile marker remains.
- Cursor is the primary Linux local profile to close; omitted `--host` remains `claude-code`.
- Quoted and unquoted Claude absent-server `mcp get` text both classify as absent, so current Claude CLI output does not fail Cursor lifecycle commands.
- Global Cursor config files may remain as an empty `mcpServers` object after uninstall. Preserving an unowned container is safer than guessing file ownership.
- Local automated acceptance proves the adapter and real MCP subprocess contract, but proprietary Cursor UI/CLI discovery still remains part of the external real-host acceptance matrix.

## Verification

Automated tests and installed-wheel smoke must prove:

- global Cursor registration ownership, idempotence, stale-owned update, foreign collision refusal, and user-config preservation;
- project override creation with the exact `${workspaceFolder}` environment contract and no cwd fallback;
- production Cursor MCP with `HARNESS_HOST_PROFILE` lists no tools and refuses calls when `HARNESS_WORKSPACE_ROOT` is absent or still the literal `${workspaceFolder}`, including when process cwd or `WORKSPACE_FOLDER_PATHS` names a real Workspace (Workspace identity on that process is superseded by [ADR-0026](0026-cursor-project-scoped-workspace-mcp.md): global registration must not carry `${workspaceFolder}`);
- tracked project config manual adoption/removal behavior with no dirty Git diff;
- Harness-created project config remains ignored without modifying `.gitignore`, including linked worktrees sharing `info/exclude`;
- two linked Workspaces receive distinct project overrides and resolve to distinct Workspace IDs;
- Claude + Cursor scan produces one compatible skill projection rather than duplicate visible copies;
- a real Harness MCP subprocess starts a Task under `claude-code`, continues it under `cursor`, retrieves Task/Knowledge state, switches back to Claude, and does not mix current Task or code/index state between two Workspaces;
- `harness doctor` reports separate Claude/Cursor registrations plus current Cursor project overrides;
- tracked isolated-development overlay is left unchanged by reconcile/remove and does not fail doctor;
- a system `harness scan` of that overlay root is refused, while isolated `scripts/dev harness scan` skips host/skill reconciliation;
- isolated `harness install`/`uninstall` are refused while `HARNESS_DEV_ROOT` is set;
- production MCP with `HARNESS_HOST_PROFILE` lists no tools and refuses calls when the profile's documented root hint, or process cwd if that hint is absent or unresolvable, resolves to the isolated-development overlay; the same overlay launch without the profile marker still exposes the five tools; a production profile whose documented root is a normal Workspace still exposes tools even when process cwd is the overlay;
- Claude `mcp get` absent-server text is `ABSENT` whether or not the server name is quoted, and any other nonzero get remains fail-closed;
- installed-wheel lifecycle covers Claude install, Cursor install, two Workspace scans, second-venv upgrade of global and project registrations, cross-host MCP continuity, Cursor-only uninstall with Claude still healthy, Cursor reinstall, and uninstall-all/purge.
