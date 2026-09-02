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
- **Relevance-key amendment (2026-09-01):** any committed Task mutation that changes the skill
  relevance key queues watcher reconciliation, not only `task_start`.
- **Superseded in part by:** [ADR-0042](0042-project-stack-skill-selection.md). Task mutations no
  longer change skill relevance or enqueue skill reconciliation. Continuous reconciliation after
  project/index changes remains.
- **Task-enqueue amendment (2026-09-02):** Decision 3 withdrawn. Decision 2 and Decision 4 remain.

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
3. After a committed Task mutation, compare the Workspace skill-relevance key
   `(relevant_task_id, relevant_task_stack_hints)` before and after the write.
   If the key changed, queue the selected Workspace on the existing watcher
   invalidation channel so skill reconciliation runs. `task_start` create/resume
   is one such mutation, not the only one. Task state remains committed
   independently; projection is the subsequent serialized repairable step. A
   projection collision does not roll back or duplicate a Task and remains
   visible to `doctor`. A failed Task mutation does not enqueue. A mutation that
   leaves the relevance key unchanged, including idempotent resume of the already
   relevant working Task, does not enqueue.
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

- Greenfield Task hints and later relevant-Task identity or hint changes now converge to native
  project skills without a manual scan.
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
and incompatible development profile sets fail before filesystem projection. They must also prove
that any committed mutation changing `(relevant_task_id, relevant_task_stack_hints)` enqueues the
existing watcher invalidation path, that a key-preserving mutation or failed CAS does not enqueue,
and that a later projection failure leaves the committed Task row intact.

## 2026-09-01 amendment: reconcile on skill-relevance key change

Decision 3 originally queued watcher skill reconciliation only after a successful `task_start`.
The skill resolver's durable input is the current relevant Task (working first, else newest
waiting) and that Task's `stack_hints`. Completion, cancel, reopen, operator accept, operator
feedback, and any other committed mutation can change that input without going through
`task_start`. Encoding a brittle matrix of transition types would miss later lifecycle actions.

The contract is therefore:

1. Capture `skill_relevance_key(workspace)` before the mutation.
2. Commit the Task mutation.
3. Capture the key again. If it changed, enqueue the Workspace on the existing serialized
   watcher invalidation queue. If it did not change, do not enqueue.
4. Projection remains asynchronous/serialized repair. A reconcile/projection failure must not
   roll back the committed Task. A failed Task mutation must not enqueue.

The key does not include Task lifecycle state unless that state change selects a different
relevant Task or different stack hints. Dashboard Task actions share this gate with daemon
`task_start` / `task_checkpoint`; relocation continues to always invalidate because it rebuilds
index identity, not because of skill relevance.

## 2026-09-01 amendment: current-session skill delivery is next-discovery-boundary

Decision 6 is product intent, not a temporary gap. [ADR-0041](0041-task-skill-session-delivery.md)
makes Task-selected skills after `task_start` in an already-started session **next-session-only**
when the host does not hot-reload. Reconciliation still guarantees projected files for the
next host discovery boundary. Identifier lists such as `recommended_skills` are not instruction
delivery. MCP does not carry skill bodies.

## 2026-09-02 amendment: Decision 3 withdrawn

[ADR-0042](0042-project-stack-skill-selection.md) withdraws Decision 3. Task mutations no longer
compare a skill-relevance key and no longer enqueue watcher skill reconciliation. The 2026-09-01
relevance-key amendment is historical.

Decision 2 and Decision 4 remain: watcher-owned authoritative scans and foreground `harness scan`
still resolve and reconcile skills after project or index changes. Task `stack_hints` stay optional
durable metadata and are not a Skill selector.
