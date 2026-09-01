# ADR-0032: Reconcile project skills continuously from durable host intent

- **Status:** Accepted
- **Date:** 2026-08-30
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0002](0002-host-integration-and-workspace-resolution.md), [ADR-0022](0022-linux-claude-installation-lifecycle.md), [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md), [ADR-0029](0029-quality-discipline-verification-and-response-economy.md)
- **Amends:** ADR-0024's decision that isolated `scan` skips skill reconciliation; production
  host-configuration isolation is unchanged.
- **Host-retirement amendment:** [ADR-0039](0039-retire-claude-code-host.md) retires Claude Code.
  Durable intent and isolated development default to Codex+Cursor only. Claude-only projection and
  the three-host incompatibility in Decision 5 are historical.

## Context

The resolver already combines the Structural Index with current Task `stack_hints`, but project
projection ran only during explicit `harness scan` and installation lifecycle operations. The
Workspace watcher refreshed the index without refreshing skills, and `task_start` did not
invalidate skill state. A greenfield Task could therefore persist `stack_hints` successfully while
the relevant native skill remained absent until a later manual scan. The isolated Harness checkout
had a second gap: installation is correctly refused there, its local registry started empty, and
its scan path skipped both host and skill reconciliation, so the tracked Codex/Cursor development
overlays could never discover Harness's own built-in skills.

The watcher needs the complete active visibility graph to avoid duplicate projection. Codex and
Cursor already persist Harness-owned integration intent, while Claude Code activity was inferred
only from the proprietary CLI during foreground commands and was unavailable to the daemon.

## Decision

1. Persist daemon-adjacent host integration intent for every installed supported profile,
   including Claude Code. Registration/configuration remains adapter-owned; the intent file is only
   the daemon's durable input for projection planning and diagnostics.
2. After each watcher-owned authoritative Workspace scan, resolve and reconcile skills using the
   current durable profile set. Filesystem/manifest changes therefore update the relevant subset
   without a manual CLI scan.
3. A successful `task_start` queues the selected Workspace for watcher reconciliation. Task state
   remains committed independently; projection is the subsequent serialized repairable step. A
   projection collision does not roll back or duplicate a Task and remains visible to `doctor`.
4. Foreground `harness scan` keeps its synchronous projection and bounded result so operators can
   request immediate convergence and see materialized/removed counts.
5. In isolated development, `scan` reconciles the built-in pack into the checkout-local registry
   and projects the relevant subset without touching user-global host state or production project
   MCP configuration. The default compatible graph is Codex + Cursor, sharing `.agents/skills`.
   `HARNESS_DEV_SKILL_PROFILES` may select another compatible comma-separated set; using Claude
   Code alone selects `.claude/skills`. The incompatible Claude + Codex + Cursor graph still fails
   closed.
6. Skill hot reload remains host-owned. Reconciliation guarantees correct files for the next host
   discovery boundary; current official Codex behavior may detect them live, and restart remains
   the fallback.

## Consequences

- Greenfield Task hints and later manifest changes now converge to native project skills.
- A daemon restart or periodic watcher reconciliation repairs missing generated projections.
- Generated paths remain ownership-marked and Git-locally excluded by the existing projection
  transaction; no `.gitignore` or tracked project instructions are changed.
- Existing installations gain continuous Claude projection after the next idempotent install
  records Claude intent. Explicit scans remain compatible before that repair.
- The isolated checkout exposes relevant Harness skills to Codex/Cursor while retaining complete
  separation from the user-global daemon, registry, and host configuration.

## Verification

Automated tests must prove Task hints trigger eventual native projection, watcher scans invoke the
resolver for the complete compatible profile set, Claude install/uninstall records and removes
durable intent, isolated scan seeds only the local registry and projects through `.agents/skills`,
and incompatible development profile sets fail before filesystem projection.
