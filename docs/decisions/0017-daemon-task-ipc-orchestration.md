# ADR-0017: Expose Task orchestration through bounded daemon IPC before MCP

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

ADR-0015 established transport-independent `task_start`, `task_resume`, and `task_checkpoint` domain operations, and ADR-0016 added atomic Task-provenance Knowledge plus mechanical staleness. The audited delivery sequence now reaches the official MCP v2 bridge, but the architecture also requires that the bridge remain thin and never access SQLite or duplicate Task business rules.

The existing internal protocol v1 already exposes daemon-owned Workspace status, deterministic scan, and bounded indexed-path search. It does not yet expose Task mutations, so an MCP bridge implemented now would either need direct database access or an ad-hoc second mutation path. Both violate daemon ownership and the explicit Task identity/revision invariants.

## Decision

Extend internal daemon IPC protocol v1 additively with two methods that delegate to the existing domain workflow:

- `task_start` supports exactly two modes:
  - create: ordered Workspace hints plus `title`;
  - resume: ordered Workspace hints plus explicit `task_id` and optional `expected_revision`.
- `task_checkpoint` requires ordered Workspace hints, explicit `task_id`, positive `expected_revision`, Task state, summary, optional next step/wait reason, and a bounded sequence of Knowledge drafts.

The daemon resolves the registered Workspace from ordered hints, verifies that its current Git identity still matches registry identity, and then calls ADR-0015/0016 domain operations. It does not reimplement lifecycle transitions, revision CAS, baseline capture, changed-file calculation, Knowledge validation, anchor capture, or transaction boundaries.

Resume semantics remain domain-owned: an already-working Task is idempotent/read-like and does not require or consume a current revision; a waiting-to-working transition requires `expected_revision`. `task_checkpoint` always targets one explicit Task and revision. Workspace-current mutable state is never used as a write target.

IPC success payloads are intentionally smaller than the domain records. `task_start` returns schema version, Workspace identity, Task identity, state/wait reason, and revision. `task_checkpoint` returns those Task fields plus checkpoint identity and bounded Knowledge IDs. It does not expose Knowledge title/body/anchors, baseline fingerprints, Git dirty fingerprints, changed-path maps, events, source bytes, or internal ranking/index metadata.

The existing 16 KiB IPC hard message limit remains an additional transport bound. Protocol v1 stays version 1 because these are additive methods with strict method-specific schemas; existing method wire shapes are unchanged.

Stable daemon errors distinguish Workspace resolution, Task not-found/conflict/revision/workspace/transition/validation failures, mechanical evidence failure, database failure, and oversized responses. Mechanical/corruption failures use bounded generic messages rather than serializing sensitive filesystem or Knowledge details.

## Consequences

### Positive

- The future official MCP bridge can remain a stateless adapter over daemon IPC.
- Task identity, revision CAS, one-working-Task enforcement, checkpoint atomicity, and Knowledge provenance have one authoritative implementation.
- Internal Task responses are bounded and negative-disclosure friendly before model-facing budgets are introduced.
- Bridge restart/reconnect cannot implicitly mutate or retarget Task state.

### Costs and limits

- Internal protocol v1 gains two mutation methods and additional validation code.
- The 16 KiB IPC frame limit can be stricter than the domain's aggregate Knowledge persistence limits; callers must stay within both contracts.
- This slice does not add MCP SDK dependencies, MCP tools, CLI Task commands, `project_status` enrichment, `project_context`, verification payloads, AgentSession activity, or host Workspace hint adapters.

## Verification

Automated tests must prove:

- real daemon round-trip create/checkpoint persists through the existing domain transactions;
- waiting resume without revision fails, CAS resume succeeds, and already-working resume remains idempotent;
- stale checkpoint revision performs no Task/checkpoint/event/Knowledge mutation;
- wrong-Workspace Task targeting fails without mutation;
- malformed/extra wire fields fail closed and the daemon remains usable;
- exact success payloads expose only the documented bounded fields and do not disclose Knowledge bodies/anchors, changed paths, baseline data, or raw source;
- existing status/search/scan IPC contracts continue to pass unchanged.
