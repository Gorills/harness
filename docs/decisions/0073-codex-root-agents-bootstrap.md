# ADR-0073: Codex bootstrap in a Workspace root AGENTS.override.md

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Operator-requested Codex bootstrap delivery

## Context

The Harness source checkout has a root `AGENTS.md`, and its first-action rule is followed in the
observed Codex conversation. Other registered Workspaces receive the rule through trusted project
`developer_instructions` and MCP server instructions. Codex loads root project instructions at the
start of a run, but a user-owned `AGENTS.md` may already exist. Codex gives a same-directory
`AGENTS.override.md` precedence over `AGENTS.md`, so merely skipping a user file would leave
bootstrap behavior dependent on the repository. Hidden Projects must keep generated instructions
out of Git without changing `.gitignore`.

## Decision

For a Codex registration, reconcile a root `AGENTS.override.md` containing the current Codex
bootstrap and, in Hidden mode, the current Hidden instructions. After `project_status`, it directs
the agent to read and follow any user root `AGENTS.md` before other repository work. Harness never
modifies that user file. A Workspace-bound marker at `.codex/.harness-agents-owner.json` proves
ownership of generated overrides. Reconciliation updates or removes only a recognized generated
body paired with its marker.

An existing user-owned or tracked override containing the exact required bootstrap and instruction
to read root `AGENTS.md` after status is accepted without mutation. An override without these, a
symlinked override, a generated override with a missing ownership marker, or a missing tracked
override blocks registration before config mutation with an actionable error. A modified
Harness-owned override also blocks reconciliation and cleanup. Manually adopted production Codex configs still
receive the generated override; the tracked source-checkout development overlay remains
non-mutating because its own root `AGENTS.md` already supplies the bootstrap.
An existing manual override that still carries Hidden instructions blocks a return to Normal
until its owner updates it.

Both Normal and Hidden generated overrides and their markers use an exact root-anchored block in
the Git common directory's `info/exclude`. This keeps generated files out of Git, including Hidden
transitions, without changing `.gitignore`. The block remains until the last owned linked worktree
is removed. The project Codex config and MCP server instructions remain additional delivery paths.

## Consequences and verification

Install and scan add a root override in registered production Codex Workspaces. Uninstall removes
only marker-owned files and their exact exclude block. Existing Workspaces receive the override on
their next authorized install or scan; running Codex sessions need a restart and fresh conversation
to load it. Linked worktrees share Git excludes, so the root-anchored pattern applies to every
worktree in the common Git directory while any owned copy remains, as with existing Codex config
exclusion.

Root instructions improve delivery but cannot force a model to call `project_status`. Real CLI,
IDE, and desktop behavior remains in the acceptance matrix in `docs/host-compatibility.md`.
Focused tests cover fresh Normal/Hidden projection, user `AGENTS.md` preservation, user override
collision/adoption, Git ignore, modified-owned refusal, concurrent appearance, linked worktree
cleanup, and uninstall.

This amends ADR-0028 and ADR-0030's earlier decision to rely only on project
`developer_instructions` and leave all root AGENTS instruction paths untouched. Ownership and
Hidden requirements continue to apply.
