# ADR-0026: Cursor IDE Workspace MCP is project-scoped

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md)

## Context

ADR-0024 registered production Cursor MCP on both `~/.cursor/mcp.json` and every Workspace `.cursor/mcp.json`, and put `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` on both so the connected `user-harness` server could resolve the current window. Overlay detection then emptied the production tool catalog when that interpolated root was the Harness source checkout.

Real-host Cursor IDE 3.10.20 evidence from 2026-08-27: the user-level server is profile-scoped (`user-harness::mcpScope:profile:default`) and labeled `mcp_version=shared_process`. One stdio process serves every window. After `harness install` / `make install-global`, Cursor reloads that shared process from the focused window. When that window is the isolated-development overlay, production MCP lists no tools and writes overlay-refusal instructions into other projects' `user-harness` catalogs. Project servers (`project-0-<folder>-harness`) exist and stay `disconnected` until Customize records them in `approvedProjectMcpServers`.

A profile-scoped process cannot carry Workspace identity. Binding `${workspaceFolder}` on the global entry, then overlay-refusing that process, poisons working repositories whenever Harness is developed in the same Cursor app.

Cursor does not auto-enable project MCP. Harness must not write Cursor's internal approval database or replace the operator's `~/.cursor/permissions.json`.

## Decision

Cursor IDE Workspace identity lives only on project-scoped stdio. The profile-scoped user server is not a Workspace server.

1. Global `~/.cursor/mcp.json` `mcpServers.harness` launches the installed Python with `-m harness.mcp_process` and `HARNESS_HOST_PROFILE=cursor` only. It does not set `HARNESS_WORKSPACE_ROOT`. Install replaces a stale owned global entry that still carries `${workspaceFolder}`.
2. Every registered Workspace `.cursor/mcp.json` override keeps the exact production launch plus `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. That interpolated value remains the only Cursor-profile Workspace hint. cwd, an uninterpolated `${workspaceFolder}` literal, and inherited folder-path environment are still not Workspace identity.
3. The Harness source checkout tracked overlay is `mcpServers.harness-dev`: `${workspaceFolder}/scripts/dev harness mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and without `HARNESS_HOST_PROFILE`. Overlay detection also still recognizes the previous same-launch entry under `mcpServers.harness` so an un-renamed checkout stays isolated. Production install/scan/uninstall must not rewrite or delete either form.
4. A Cursor-profile MCP process without an interpolated `HARNESS_WORKSPACE_ROOT` lists no tools. Instructions tell the model to enable the project `harness` MCP in Cursor Customize (and `harness-dev` in this overlay checkout). They must not say production MCP is refused because the process is the Harness source checkout.
5. Overlay-refuse for production MCP (`HARNESS_HOST_PROFILE` set) activates only when that profile's documented root hint interpolates to the isolated-development overlay. Cursor profile does not consult process cwd for overlay detection. Claude Code keeps `CLAUDE_PROJECT_DIR` and the previous cwd fallback when that hint is absent or unresolvable. Isolated overlay launches without the profile marker still serve the five tools against `.harness/`.
6. `harness install` / `harness doctor` tell the operator to enable the project `harness` MCP once in Customize for each working repository, fully quit/reopen Cursor, and not hardcode a Workspace path. Doctor treats a leftover global `${workspaceFolder}` as stale owned. Harness does not write `approvedProjectMcpServers` or overwrite `permissions.json`.

## Consequences

- Opening the Harness source checkout and running `make install-global` no longer binds the shared `user-harness` process to the overlay root.
- Working-project agents get five production tools only after the project `harness` server is enabled in Customize. That is one host gesture per Workspace, not a per-update ritual.
- Checkout agents use `harness-dev` once that project server is enabled. They must not call `user-harness`.
- Cursor CLI `agent mcp list-tools harness` is no longer proof that IDE chat has five tools; the CLI may still see the user-level server. Project override presence plus doctor remain the adapter contract; proprietary catalog connection stays in the real-host matrix.
- A leftover global entry that still interpolates `${workspaceFolder}` to the overlay can still overlay-refuse until `harness install --host cursor` rewrites it.

## Verification

Automated tests must prove:

- global Cursor registration owns the stdio launch with `HARNESS_HOST_PROFILE=cursor` and without `HARNESS_WORKSPACE_ROOT`; a leftover `${workspaceFolder}` on that entry is stale owned and install removes it;
- project overrides still carry `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and still refuse cwd / uninterpolated / inherited folder-path identity;
- tracked `harness-dev` overlay and a legacy `harness` overlay launch are left unchanged and do not fail doctor;
- Cursor-profile MCP without an interpolated root lists no tools and does not use overlay-refusal copy, including when process cwd is the overlay;
- Cursor-profile MCP whose interpolated `HARNESS_WORKSPACE_ROOT` is the overlay lists no tools and uses overlay-refusal copy;
- the overlay launch without `HARNESS_HOST_PROFILE` still exposes the five tools;
- Claude-profile overlay refuse still follows `CLAUDE_PROJECT_DIR` or cwd when that hint is absent or unresolvable.
