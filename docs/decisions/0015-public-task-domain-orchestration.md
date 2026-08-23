# ADR-0015: Add public daemon-domain Task start, resume, and checkpoint orchestration

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

ADRs 0011–0014 established durable Task revision CAS, mandatory mechanical Task-start baselines, mechanical changed-file calculation, and atomic checkpoint/event persistence. The corrected architecture requires the next layer to expose one transport-independent domain workflow before daemon IPC, CLI, or MCP. Existing-Task mutations must target an explicit Task and revision, while resume of an already-working Task remains idempotent/read-like. ADR-0014 also intentionally deferred Task-created/resumed events until this orchestration gave them precise semantics.

## Decision

Add public daemon-domain operations `task_start`, `task_resume`, and `task_checkpoint`. They are Python domain APIs owned by the daemon layer; they are not yet IPC, CLI, or MCP contracts.

`task_start(connection, workspace_id, title)` creates a new Task only when the Workspace has no distinct working Task. One `BEGIN IMMEDIATE` transaction captures the mandatory ADR-0012 baseline, persists the Task at revision 1, appends exactly one immutable `created` event, and commits. Any baseline, Task, or event persistence failure rolls the whole operation back.

`task_resume(connection, workspace_id, task_id, expected_revision=...)` always resolves the explicit Task and verifies Workspace ownership. If the Task is already `working`, resume is idempotent/read-like: it returns current durable state without incrementing revision or appending an event, and no revision token is required. A `waiting → working` resume requires a positive `expected_revision`; under `BEGIN IMMEDIATE` it rechecks ownership/state, performs the existing CAS transition, clears the wait reason, increments revision exactly once, and appends one immutable `resumed` event for the new revision before commit. `completed` and `cancelled` remain terminal. A waiting Task cannot resume while another Task is working in the same Workspace.

`task_checkpoint(connection, workspace_id, task_id, expected_revision=..., ...)` is the public domain wrapper around ADR-0014 checkpoint persistence. Task/Workspace ownership is checked inside the same checkpoint `BEGIN IMMEDIATE` transaction before revision validation and mechanical changed-file work, so the public precondition has no check/use race. The checkpoint primitive otherwise keeps its existing semantics and atomicity.

Schema version 7 expands `task_events` to `created`, `resumed`, and `checkpoint`. Database checks require `created` at revision 1 with no checkpoint, `resumed` after revision 1 with no checkpoint, and `checkpoint` after revision 1 with matching checkpoint identity. Partial unique indexes allow only one `created` event per Task and one `resumed` event per Task revision. Existing schema-v6 checkpoint event IDs are preserved during migration. The migration deliberately does not fabricate `created` or `resumed` events for historical Tasks because Harness cannot reconstruct lifecycle facts that were never persisted.

No operation infers a mutating target from Workspace-current state. No raw source is persisted or exposed by this layer.

## Consequences

### Positive

- The intended Task workflow now exists once in transport-independent daemon-domain code.
- Task creation, baseline capture, and creation history are crash-consistent.
- Mutating resume uses the same stable identity and revision CAS contract as other Task writes.
- Idempotent already-working resume remains cheap and does not create synthetic history.
- Public checkpoint ownership is verified within the atomic write boundary.
- Later daemon IPC, CLI, and MCP layers can stay thin rather than reimplementing Task semantics.

### Costs and limits

- Schema version increases from 6 to 7.
- Historical pre-v7 Tasks can have checkpoint history without a `created`/`resumed` event; this is an explicit truthful-history limitation, not corruption.
- Operator feedback, acceptance, cancellation, verification, Knowledge, and bridge/session activity remain outside this slice.
- No daemon IPC method, CLI Task command, MCP tool payload, or model-visible budget is defined here.

## Verification

Automated tests must prove:

- schema v6 migrates to v7 preserving checkpoint events and their IDs without fabricating lifecycle history;
- lifecycle-event database constraints reject invalid linkage and duplicate created/resumed events;
- Task start persists Task + baseline + created event atomically and rolls back all three if event persistence fails;
- already-working resume is non-mutating even when a stale revision token is supplied;
- waiting resume requires revision CAS, increments once, appends one resumed event, and rolls back the state transition if event persistence fails;
- terminal Tasks and waiting Tasks blocked by another working Task cannot resume;
- public resume/checkpoint reject explicit Tasks owned by another Workspace;
- checkpoint Workspace ownership is checked inside its write transaction;
- stale checkpoint revision remains non-mutating and never retargets to another Task.
