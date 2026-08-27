# ADR-0028: Hygiene-effective Hidden without host SCM-write enforcement

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0003](0003-normal-and-hidden-visibility-modes.md)
- **Does not replace:** [ADR-0004](0004-hidden-mode-transition-barrier.md), [ADR-0005](0005-hidden-transition-recovery-safety.md), [ADR-0006](0006-hidden-transition-crash-atomicity.md)

## Context

ADR-0003 defines Hidden as fail-closed host-enforced SCM invisibility. Cursor still has no proven project-local hard denial of agent-originated git/PR publication. Operators nonetheless need Hidden for repositories where agent work must not appear in Git: always-on host rules plus Git-local exclusion of Harness-owned artifacts.

Reporting Cursor Hidden as *enforced* would be false. Refusing Hidden on Cursor would make the mode unusable on the primary Linux local host. This decision records an explicit hygiene-effective slice.

## Decision

An operator may set durable Project `visibility_mode=hidden` through non-model-facing surfaces (CLI and dashboard). MCP still serves the five tools. `project_status` continues to expose only the `normal`/`hidden` enum.

While Hidden is effective, Harness MUST:

1. Project host-local always-on Hidden instructions for each active installed host profile (Cursor `.cursor/rules/harness-hidden.mdc` with `alwaysApply: true`; Claude Code `.claude/rules/harness-hidden.md`). Files are Harness-owned, fail closed on tracked or unknown user-owned collisions, and never replace tracked instruction files such as `AGENTS.md` or `CLAUDE.local.md`.
2. Keep those exact Harness-owned paths untracked and ignored through `git rev-parse --git-path info/exclude` without modifying `.gitignore`.
3. Tell the model, via `visibility_mode` plus MCP instructions and the projected rule, not to perform durable SCM mutations or add agent-identifying Git artifacts.

Cursor `scm_write_enforcement` remains **unsupported**. Dashboard and doctor MUST say that Cursor does not host-block `git commit`/`push`/PR. That diagnostic MUST NOT appear in model-visible MCP payloads.

Prompt rules, MCP instructions, and `info/exclude` remain defense-in-depth. `git add -f` can still stage ignored files. This slice does not implement live Normal-capability revocation (ADR-0004/0005) or a durable transition journal (ADR-0006). Commit ordering for this slice's side effects is:

- `normal → hidden`: project instructions and exclude entries, verify, then persist `visibility_mode=hidden`.
- `hidden → normal`: persist `visibility_mode=normal`, then remove only Harness-owned Hidden files and exclude entries.

Doctor WARNs when Normal still has leftover owned Hidden files, and FAILs when Hidden is missing required ignored instructions.

## Consequences

- Operators can hide Harness rules/artifacts from ordinary Git status on Cursor and Claude without claiming host SCM enforcement.
- Agents see `hidden` and are instructed not to publish; a Cursor agent can still commit if the human/host allows it.
- Full enforced Hidden remains a later slice when a profile can deny SCM writes, prove policy integrity, and satisfy ADR-0004/0005/0006.

## Verification

Automated tests must prove:

- operator CLI/dashboard can set Hidden; no MCP tool can;
- Hidden projection leaves `.gitignore` unchanged and owned paths untracked/ignored;
- tracked and user-owned collisions fail without changing the durable mode;
- linked worktrees share `info/exclude` and receive the rule in each worktree;
- restore to Normal removes only Harness-owned Hidden files/exclude entries;
- `git add -f` can still add an ignored Hidden rule;
- `project_status` does not leak enforcement internals;
- the isolated-development Cursor overlay `mcp.json` is not rewritten.
