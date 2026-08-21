# ADR-0002: Isolate host integration and make Workspace resolution explicit

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline

## Context

Harness is globally installed but Project/Workspace context must remain local to the active repository. A globally registered stdio MCP server therefore needs a reliable way to determine which Workspace the host currently means.

Official host documentation proves global/user MCP configuration support across the target hosts, but it does not establish one universal runtime mechanism for propagating active project root.

Examples:

- Claude Code explicitly sets `CLAUDE_PROJECT_DIR` for spawned stdio MCP servers.
- Codex documents global `~/.codex/config.toml` and project `.codex/config.toml`, plus stdio configuration options, but those configuration facts alone are not proof that a globally configured server process always receives the active repository as its current directory.
- Cursor documents global/project MCP configuration and `${workspaceFolder}` interpolation; the documented interpolation describes the folder containing project config, so global-current-workspace semantics require acceptance testing rather than inference.
- Antigravity documents global MCP configuration and workspace `.agents/mcp_config.json`; its skill paths also differ between IDE/global and Antigravity CLI/global documentation.

The same heterogeneity exists for native skills. Cursor scans multiple compatibility roots, so copying one generated skill into every host's project folder can create duplicate visible skills.

## Decision

1. `HostAdapter` owns exact host configuration paths, command construction, workspace hints, bootstrap integration, native skill projection, cleanup, and doctor checks.
2. Core receives normalized integration facts and never branches on host names.
3. Introduce an internal `WorkspaceResolver` orchestration component with ordered, evidence-based hints.
4. A host-specific current-root signal is usable as a correctness input only when established by official documentation or a maintained real-host acceptance test.
5. Failure to resolve a Workspace unambiguously is an actionable error, never a silent fallback to another registered Project.
6. Skills are projected from a canonical external registry through a per-Workspace projection plan that detects collisions/duplicates and tracks Harness ownership.
7. Real-host acceptance is a separate evidence layer from core automated tests.

## Consequences

### Positive

- Fast-changing host details stay out of durable domain code.
- Incorrect workspace attachment becomes detectable instead of implicit.
- Host path changes can be repaired without rewriting search/task/index logic.
- Skill duplication/collision risk becomes a designed contract.

### Costs

- Adapters need maintained compatibility tests/documentation.
- Some workspace behaviors cannot be proven in CI without installed proprietary hosts.
- Integration metadata needs version/capability diagnostics for `doctor`.

## References

- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/skills
- https://developers.openai.com/codex/mcp/
- https://developers.openai.com/codex/skills/
- https://cursor.com/docs/mcp
- https://cursor.com/docs/skills
- https://antigravity.google/docs/mcp
- https://antigravity.google/docs/skills/
