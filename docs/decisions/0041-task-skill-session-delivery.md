# ADR-0041: Task-selected skills are next-discovery-boundary, not current-session delivery

- **Status:** Accepted
- **Date:** 2026-09-01
- **Deciders:** Repository architecture baseline
- **Choice:** Option A — next-discovery-boundary semantics
- **Builds on:** [ADR-0032](0032-continuous-project-skill-reconciliation.md),
  [ADR-0029](0029-quality-discipline-verification-and-response-economy.md)
- **Amends:** ADR-0032 Decision 6 is product intent for live sessions, not a temporary gap.

## Context

Task `stack_hints` and relevant-Task identity change which skills the resolver projects.
ADR-0032 already queues watcher reconciliation after a skill-relevance key change and states
that skill hot reload is host-owned: reconciliation guarantees files for the **next host
discovery boundary**.

That filesystem guarantee is not instruction delivery. A new `SKILL.md` on disk after
`task_start` does not prove the live model context loaded it. Host hot reload is not
Harness-owned and must not be treated as a correctness dependency.

Returning `{"recommended_skills":["x"]}` (or any other identifier list) is not delivery:
the identifier is not the skill body. Harness MCP is a five-tool surface
(`project_status`, `project_search`, `project_context`, `task_start`, `task_checkpoint`).
`project_context` expands code/doc/Knowledge/Task refs only. It has no skill-ref kind.

The original specification already says correctness does not depend on live detection of a
new skill in an existing session (`docs/specification.md` §82). R-03 makes that the
enforceable product contract for task-specific skills.

## Decision

Choose **Option A**: Task-selected skills affect native projection for the **next host
discovery boundary**. They do not create a Harness-owned current-session instruction channel.

Rejected for this contract:

- **Option B** (stable native catalog plus task selection) would keep a broader catalog
  visible at session start. It changes projection budget and polyglot visibility without
  solving hosts that never re-read skills mid-session. Not selected.
- **Option C** (MCP fallback skill-ref/context) would duplicate progressive disclosure
  through daemon-owned MCP. It needs a skill-ref schema, reference policy, and a predefined
  current-session expected result. Not implemented here; not the v1 contract.
- **Option D** (hybrid B+C) inherits both costs. Not selected.

### Synthetic acceptance gate

```text
session starts
skill X was not task-selected
task_start selects X
host never hot reloads
```

**Expected result: next-session-only.** Harness does not claim that guidance X is available
in the current session. Projection/enqueue (ADR-0032 / R-01) prepares files for the next
host discovery boundary (typically a new session, full client restart/reopen, or other
host-documented skill rediscovery). Optional host-owned live detection may still occur; it
is not a Harness guarantee and is not scored as current-session delivery.

Codex `scripts/accept_codex.py` native skill-read proves the **scan-projected** set already
on disk when that Codex process starts. It does not prove mid-session selection of a skill
that was absent at session start.

### Nine questions

1. **Must Task hints affect the current live session?**
   No. Hints update durable relevance and may enqueue native projection. They do not mutate
   the already-started session's model instruction context. Harness does not claim
   current-session model guidance from mid-session `task_start`. A host that independently
   hot-reloads may load new files; Harness does not own or require that.

2. **What is proof of delivery?**
   Instruction delivery is proved only when the host session actually loaded the skill body
   (or host-owned progressive disclosure of that body). Filesystem presence, watcher
   enqueue, materialized `SKILL.md`, and identifier lists (`recommended_skills`, skill ids
   in MCP JSON) are not delivery.

3. **What happens on a host without hot reload?**
   After `task_start` selects skill X that was not in the session's discovery set, X is not
   available in that session. The next discovery boundary sees the projected files. Restart
   remains the documented fallback.

4. **What happens on skill budget overflow?**
   Unchanged: the resolver projects at most the configured bounded subset. Skills that do
   not win the budget are not projected and are not delivered through MCP. Overflow is a
   next-boundary projection outcome, not a current-session MCP fallback.

5. **How are nested `references/` disclosed?**
   Host-native progressive disclosure after the host loads the projected `SKILL.md`.
   Harness MCP does not expand skill refs or embed reference bodies. Canonical nested
   content remains registry/projection policy (ADR-0029).

6. **How is duplicate or conflicting guidance prevented?**
   Unchanged projection plan: one generated copy per compatible active graph, collision
   detection, ownership-marked cleanup. MCP does not carry skill bodies, so it cannot add
   a second instruction channel that conflicts with native skills. A live session may keep
   stale previously projected skills when selection changes and the host does not reload.
   That is Option A, not a Harness bug.

7. **What remains host-native?**
   Skill discovery, progressive disclosure, optional hot reload, and applying `SKILL.md`
   in model context. Projection target paths (shared `.agents/skills` for Codex/Cursor).

8. **What is daemon-owned?**
   Canonical registry, relevance resolution, generating/removing projected files, and
   watcher enqueue after a skill-relevance key change. Not owned: live session instruction
   context, host skill catalogs, or MCP skill-body delivery.

9. **How does acceptance verify this without undocumented host assumptions?**
   Automated tests lock Option A against current code: MCP tools and structured responses
   have no skill-body or `recommended_skills` delivery fields; `project_context` rejects
   skill refs; the `task_start` tool description states the next discovery boundary and
   that mid-session `task_start` is not live skill injection; R-01 tests remain
   filesystem/watcher enqueue, not model context. The synthetic gate expected result is
   **next-session-only**. Optional host hot reload must not cause "relevant generated
   skill is visible" to be scored as Harness current-session delivery; score
   next-session-only / files at the next discovery boundary. A no-reload session must
   not be accepted as Harness current-session delivery. Codex skill-read is
   scan-projected, session-start native discovery.

## Consequences

- Greenfield and later Task hint changes still converge on disk without a manual scan.
- Operators and agents must not treat mid-session `task_start` as live skill injection.
  That caveat is on the `task_start` tool description (`stack_hints` drive the next host
  discovery boundary, not current-session MCP skill delivery). `_SERVER_INSTRUCTIONS`
  does not repeat it: the 1024-byte budget already holds R-02 status→task→search,
  Russian in the first 512 characters, and the code/doc native-read exemption.
- Closing current-session delivery would be a later ADR (Option C/D) with a new expected
  result and a skill-ref schema; this ADR forbids shipping that as a silent MCP add-on.

## Verification

Automated tests must prove:

- the five MCP tools and `task_start` / `project_status` payloads do not include skill
  bodies or `recommended_skills` as delivery;
- `project_context` kinds remain code/doc/knowledge/task and reject `skill:` refs;
- the `task_start` tool description states next host discovery boundary and forbids
  treating mid-session `task_start` as live skill injection;
- docs and Codex acceptance wording state next-session-only for the synthetic gate;
- projection-after-`task_start` tests still assert watcher enqueue / filesystem repair.
