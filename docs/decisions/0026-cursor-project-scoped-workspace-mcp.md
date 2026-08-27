# ADR-0026: Cursor user-harness must keep serving working projects

- **Status:** Accepted
- **Date:** 2026-08-27
- **Amended:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md)

## Context

ADR-0024 registered production Cursor MCP on both `~/.cursor/mcp.json` and every Workspace `.cursor/mcp.json`, and put `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` on both so the connected `user-harness` server could resolve the current window. Overlay detection then emptied the production tool catalog when that interpolated root was the Harness source checkout.

Real-host Cursor IDE 3.10.20 evidence from 2026-08-27: the user-level server is profile-scoped (`user-harness::mcpScope:profile:default`) and labeled `mcp_version=shared_process`. One stdio process serves every window. After `harness install` / `make install-global`, Cursor reloads that shared process from the focused window. When that window is the isolated-development overlay, production MCP lists no tools and writes overlay-refusal instructions into other projects' `user-harness` catalogs.

The operator-visible failure is: working repositories such as mangazeya-backend show 0 Harness tools after a global install done while the Harness checkout is open. Cursor does not auto-enable project MCP (`project-0-<folder>-harness` stays `disconnected`). Telling the operator to enable project MCP in Customize does not restore tools on the connected `user-harness` catalog they actually use.

A later live process after removing `${workspaceFolder}` from the global entry still had `WORKSPACE_FOLDER_PATHS=/home/gorills/projects/mangazeya-backend` and `cwd=$HOME`. Cursor injects that folder-path environment on the shared stdio process. It is not official MCP config interpolation, and it can name the window that spawned the shared process rather than every open window. It is the only runtime Workspace signal the connected user-level server actually receives.

Harness must not write Cursor's internal approval database or replace the operator's `~/.cursor/permissions.json`.

## Decision

`user-harness` remains the connected production Cursor server. It must keep exposing the five tools in working repositories after `make install-global`. Project overrides stay for an enabled project identifier; they are not the operator-visible catalog.

1. Global `~/.cursor/mcp.json` `mcpServers.harness` launches the installed Python with `-m harness.mcp_process` and `HARNESS_HOST_PROFILE=cursor` only. It does not set `HARNESS_WORKSPACE_ROOT`. Install replaces a stale owned global entry that still carries `${workspaceFolder}` so a reload from the overlay window cannot interpolate the Harness checkout into the Harness-owned root env.
2. Every registered Workspace `.cursor/mcp.json` override keeps the exact production launch plus `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. That interpolated value is the project-scoped Cursor Workspace hint. cwd and an uninterpolated `${workspaceFolder}` literal are still not Workspace identity.
3. The Harness source checkout tracked overlay is `mcpServers.harness-dev`: `${workspaceFolder}/scripts/dev harness mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and without `HARNESS_HOST_PROFILE`. Overlay detection also still recognizes the previous same-launch entry under `mcpServers.harness`. Production install/scan/uninstall must not rewrite or delete either form.
4. Cursor-profile Workspace resolution uses interpolated `HARNESS_WORKSPACE_ROOT` when it is a real non-overlay directory. Otherwise it uses existing directories from Cursor's `WORKSPACE_FOLDER_PATHS` (comma-separated), preferring a path that is not the isolated-development overlay. A process with neither signal lists no tools. It must not tell the model that production MCP is refused because the process is the Harness source checkout.
5. Overlay-refuse for Cursor-profile production MCP activates only when interpolated `HARNESS_WORKSPACE_ROOT` resolves to the overlay *and* `WORKSPACE_FOLDER_PATHS` has no non-overlay directory. A user-level process without that interpolated env never overlay-refuses. Claude Code keeps `CLAUDE_PROJECT_DIR` and the previous cwd fallback when that hint is absent or unresolvable. Isolated overlay launches without the profile marker still serve the five tools against `.harness/`.
6. `harness install` / `harness doctor` tell the operator to fully quit/reopen Cursor after MCP config changes. Doctor treats a leftover global `${workspaceFolder}` as stale owned. Harness does not write `approvedProjectMcpServers` or overwrite `permissions.json`. Checkout agents must not call `user-harness`; they use `harness-dev`.

## Consequences

- Opening the Harness source checkout and running `make install-global` no longer empties the `user-harness` catalog in working repositories when Cursor still supplies a non-overlay `WORKSPACE_FOLDER_PATHS`.
- Working-project agents keep five production tools on the connected `user-harness` server without a Customize gesture.
- Shared-process identity can still follow the spawn window rather than every open window. That is a Cursor host limit, not a reason to list zero tools.
- Checkout agents use `harness-dev`. They must not call `user-harness`.
- A leftover global entry that interpolates `${workspaceFolder}` to the overlay still overlay-refuses only when there is no non-overlay folder-path alternative.

## Verification

Automated tests must prove:

- global Cursor registration owns the stdio launch with `HARNESS_HOST_PROFILE=cursor` and without `HARNESS_WORKSPACE_ROOT`; a leftover `${workspaceFolder}` on that entry is stale owned and install removes it;
- project overrides still carry `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and still refuse cwd / an uninterpolated literal as identity;
- tracked `harness-dev` overlay and a legacy `harness` overlay launch are left unchanged and do not fail doctor;
- Cursor-profile MCP with `WORKSPACE_FOLDER_PATHS` naming a real non-overlay directory lists the five tools even when interpolated `HARNESS_WORKSPACE_ROOT` is the overlay or is absent;
- Cursor-profile MCP whose interpolated `HARNESS_WORKSPACE_ROOT` is the overlay and that has no non-overlay folder-path lists no tools and uses overlay-refusal copy;
- Cursor-profile MCP with neither interpolated root nor folder paths lists no tools and does not use overlay-refusal copy;
- the overlay launch without `HARNESS_HOST_PROFILE` still exposes the five tools;
- Claude-profile overlay refuse still follows `CLAUDE_PROJECT_DIR` or cwd when that hint is absent or unresolvable.
