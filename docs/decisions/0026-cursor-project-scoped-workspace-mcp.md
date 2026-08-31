# ADR-0026: Cursor Workspace identity is project-scoped interpolated root

- **Status:** Accepted
- **Date:** 2026-08-27
- **Amended:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md)

## Context

ADR-0024 registered production Cursor MCP on both `~/.cursor/mcp.json` and every Workspace `.cursor/mcp.json`, and put `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` on both so the connected `user-harness` server could resolve the current window. Overlay detection then emptied the production tool catalog when that interpolated root was the Harness source checkout.

Real-host Cursor IDE 3.10.20 evidence from 2026-08-27: the user-level server is profile-scoped (`user-harness::mcpScope:profile:default`) and labeled `mcp_version=shared_process`. One stdio process serves every window. After `harness install` / `make install-global`, Cursor reloads that shared process from the focused window. When that window is the isolated-development overlay, interpolating `${workspaceFolder}` on the global entry makes production MCP list no tools and write overlay-refusal instructions into other projects' `user-harness` catalogs.

A first amendment therefore removed `HARNESS_WORKSPACE_ROOT` from the global entry and treated Cursor-injected `WORKSPACE_FOLDER_PATHS` as user-level Workspace identity so working repositories kept five tools without a Customize gesture.

A later live process after that change still had `WORKSPACE_FOLDER_PATHS=/home/gorills/projects/mangazeya-backend` and `cwd=$HOME`. Cursor injects that folder-path environment on the shared stdio process. It is not official MCP config interpolation, and it names the window that spawned the shared process rather than the calling window.

Real-host Cursor IDE evidence from 2026-08-27: an Alia window (`/home/gorills/projects/alia`) with disconnected project MCP used that same `user-harness` process. `project_status` reported mangazeya-backend (`hotfix/MNG-3938-commercial-cards`). Search against Alia mail was empty, and a From-address PDF Task was written into Mangazeya. ADR-0002 forbids resolving a Workspace by silently attaching another registered Project. Empty tools is required over that class of write.

The same-name global plus project `harness` pair still competed after that identity fix. Live `agent mcp list-tools harness` could report no tools because the empty shared-process catalog won, while project configs stayed disconnected until Cursor recorded them in `approvedProjectMcpServers`. Customize often does not surface that project row. Official Cursor CLI documents `agent mcp enable <identifier>` as the approval path. Harness must not write Cursor's internal approval database or replace the operator's `~/.cursor/permissions.json`.

## Decision

Production Cursor MCP is project-only. Harness must not keep an owned global `user-harness` server. Shared-process global MCP cannot safely distinguish windows and shadows a same-name project server.

1. Do not write `~/.cursor/mcp.json` `mcpServers.harness`. Install/scan/uninstall recognize and remove only a leftover Harness-owned global entry. A foreign global `harness` is a fail-closed collision because it can mask project scope. Harness never deletes an unowned global config container.
2. Cursor activity is Harness-owned host integration state next to the canonical database (`host-integrations.json`), not the presence of a global MCP entry. Install writes that intent before host mutation. Uninstall removes leftover global/project configs, then clears the marker last.
3. Every registered Workspace `.cursor/mcp.json` override is the production launch: installed Python, `-m harness.mcp_process`, `HARNESS_HOST_PROFILE=cursor`, and `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. That interpolated value is the only Cursor-profile Workspace hint. cwd, an uninterpolated `${workspaceFolder}` literal, and `WORKSPACE_FOLDER_PATHS` are never Workspace identity.
4. After writing a project config, Harness runs official `agent mcp enable harness` from that Workspace root, then `agent mcp list-tools harness`, and requires the exact five tools (`project_status`, `project_search`, `project_context`, `task_start`, `task_checkpoint`) with a timeout. Missing `agent` is a manual fallback with the exact `cd <workspace> && agent mcp enable harness` command plus a full Cursor quit/reopen. A present CLI that fails enable or verification fails the operation. Harness does not write `approvedProjectMcpServers` or overwrite `permissions.json`.
5. The Harness source checkout tracked overlay is `mcpServers.harness-dev`: `${workspaceFolder}/scripts/dogfood mcp` with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and without `HARNESS_HOST_PROFILE`. The router defaults to the isolated `scripts/dev` runtime and may select the installed runtime only through explicit ADR-0036 state. Overlay detection also recognizes the legacy direct `scripts/dev harness mcp` launch and the previous same-launch entry under `mcpServers.harness`. Production install/scan/uninstall must not rewrite, delete, or enable that overlay as production `harness`.
6. Cursor-profile Workspace resolution uses interpolated `HARNESS_WORKSPACE_ROOT` when it is a real directory. A process without that interpolated root lists no tools and uses missing-root copy. It must not tell the model that production MCP is refused because the process is the Harness source checkout, and it must not bind another window through `WORKSPACE_FOLDER_PATHS`.
7. Overlay-refuse for Cursor-profile production MCP activates when interpolated `HARNESS_WORKSPACE_ROOT` resolves to the overlay, including when `WORKSPACE_FOLDER_PATHS` names a working repository. A process without that interpolated env never overlay-refuses. Claude Code keeps `CLAUDE_PROJECT_DIR` and the previous cwd fallback when that hint is absent or unresolvable. Isolated overlay launches without the profile marker still serve the five tools against `.harness/`.
8. Doctor splits on-disk project config from Cursor approval/tool catalog, daemon runtime, and Project index. Expected global state is absence of owned `user-harness`. Leftover owned or foreign global collision is FAIL. When `agent` is present, missing five tools or approval is a per-Workspace FAIL. When `agent` is absent, doctor WARNs with the exact enable command instead of reporting a blanket OK. Isolated doctor (`HARNESS_DEV_ROOT`) does not inspect user-global Cursor/Claude MCP or `~/.harness/skills`.
   The official `agent mcp list-tools` probe and its owned-launch fallback share one explicit doctor
   timeout budget; fallback does not receive a second full timeout after a slow agent probe.
9. Checkout agents must not call leftover `user-harness`; they use `harness-dev`. Isolated development uses checkout-local `HARNESS_SKILL_REGISTRY`.

## Consequences

- Opening the Harness source checkout and running `make install-global` cannot interpolate the overlay into a global `user-harness` catalog because that catalog is removed.
- Two open Cursor windows cannot attach the calling window's agent to the spawn window's Project through a shared Harness process.
- Working-project agents get five tools after `agent mcp enable harness` (or the printed manual equivalent) and a full Cursor quit/reopen. Keeping five tools on a connected `user-harness` catalog is not supported.
- Re-running `harness install --host cursor` heals an interrupted update and re-approves project configs after a launch-hash change.
- Checkout agents use `harness-dev`. They must not call leftover `user-harness`.
- A leftover global owned entry is stale and install removes it. A foreign global `harness` remains a collision.

## Verification

Automated tests must prove:

- install/scan never write global `mcpServers.harness`; leftover owned global entries are removed and foreign global `harness` is refused;
- Cursor activity is recorded in Harness host integration state, not inferred from `~/.cursor/mcp.json`;
- project overrides still carry `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` and still refuse cwd / an uninterpolated literal / `WORKSPACE_FOLDER_PATHS` as identity;
- official `agent mcp enable harness` plus `agent mcp list-tools harness` verify the exact five tools; missing CLI is a printed manual fallback; a present CLI that fails enable or verification fails the operation;
- tracked `harness-dev` overlay and a legacy `harness` overlay launch are left unchanged, are not enabled as production `harness`, and do not fail doctor;
- Cursor-profile MCP with `WORKSPACE_FOLDER_PATHS` naming a real non-overlay directory lists no tools and uses missing-root copy when interpolated `HARNESS_WORKSPACE_ROOT` is absent or still the literal `${workspaceFolder}`;
- Cursor-profile MCP whose interpolated `HARNESS_WORKSPACE_ROOT` is a working directory lists the five tools even when `WORKSPACE_FOLDER_PATHS` names a different directory;
- Cursor-profile MCP whose interpolated `HARNESS_WORKSPACE_ROOT` is the overlay lists no tools and uses overlay-refusal copy even when `WORKSPACE_FOLDER_PATHS` names a working repository;
- the overlay launch without `HARNESS_HOST_PROFILE` still exposes the five tools;
- isolated doctor/skills do not inspect user-global Cursor/Claude MCP or `~/.harness/skills`;
- Claude-profile overlay refuse still follows `CLAUDE_PROJECT_DIR` or cwd when that hint is absent or unresolvable.
