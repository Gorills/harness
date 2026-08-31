# ADR-0039: Retire Claude Code as a Harness host

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amends:** [ADR-0022](0022-linux-claude-installation-lifecycle.md),
  [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md),
  [ADR-0030](0030-codex-project-scoped-workspace-mcp.md),
  [ADR-0032](0032-continuous-project-skill-reconciliation.md)

## Context

Claude Code was a first-class Linux host: user-scope `claude mcp` registration, `CLAUDE_PROJECT_DIR`
Workspace identity, `.claude/skills` projection, and Hidden rules. Cursor loads `.claude/skills` as
a compatibility directory, so the Claude+Codex+Cursor skill graph cannot be projected without
duplicates. `--host all` install was therefore rejected.

The operator no longer uses Claude Code. Keeping it as a supported profile preserves a default
`--host claude-code` CLI, a tracked `.mcp.json` overlay, doctor WARNs for an unused CLI, and the
three-host incompatibility.

## Decision

1. Supported hosts are Codex and Cursor only. `harness install --host claude-code` is refused.
   Omitted `--host` installs Cursor. `--host all` installs the Codex+Cursor pair.
2. `ClaudeCodeAdapter`, Claude skill/Hidden projection, scan/doctor Claude registration, and the
   checkout `.mcp.json` dogfood overlay are removed. Historical ADRs stay as history.
3. Host-integration state that still lists `claude-code` is loaded by dropping that retired
   profile. Writes persist only Codex and Cursor.
4. Cursor still treats `.claude/skills` as a visible compatibility root so leftover Harness-owned
   files collide and can be cleaned. Hidden apply removes leftover Harness-owned
   `.claude/rules/harness-hidden.md` when Claude is not an active profile.
5. `HARNESS_HOST_PROFILE=claude-code` is an unsupported profile. Overlay refuse may still use
   `CLAUDE_PROJECT_DIR` so a leftover production process against this checkout lists no tools.
6. `CLAUDE.md` remains a one-line `@AGENTS.md` pointer for editors; it is not a host integration.

## Consequences

- Codex+Cursor is the only supported multi-host graph; `--host all` install is valid.
- Operators with leftover user-scope Claude MCP must remove it with the Claude CLI. Harness no
  longer registers or unregisters that server.
- Isolated development defaults stay Codex+Cursor. `HARNESS_DEV_SKILL_PROFILES=claude-code` fails
  closed as an unsupported profile.
