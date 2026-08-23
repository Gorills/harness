# ADR-0014: Persist Task checkpoints and checkpoint events atomically

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

ADR-0011 established durable Task identity and revision compare-and-set, ADR-0012 added the mandatory mechanical Task baseline, and ADR-0013 added read-only mechanical changed-file calculation. The next dependency is durable checkpoint history. The specification requires a meaningful checkpoint to retain Task state, timestamp, changed files, Git state, and semantic progress, while the audited architecture requires every existing-Task mutation to use explicit `task_id` plus `expected_revision` and to fail without partial state/event writes on conflict.

This slice must establish those persistence and atomicity semantics without prematurely exposing public `task_checkpoint`, daemon IPC, CLI, MCP, Knowledge, verification, or bridge/session attachment.

## Decision

Add schema version 6 with three checkpoint-history structures:

- `task_checkpoints` stores one immutable checkpoint per post-mutation Task revision;
- `task_checkpoint_changed_paths` stores the deterministic mechanical changed-path set for that checkpoint;
- `task_events` stores an ordered immutable `checkpoint` timeline event tied by database foreign keys to the same Task, checkpoint, and Task revision.

Add internal `checkpoint_task(connection, task_id, expected_revision=..., ...)`. It is a daemon-domain mutation foundation, not yet a public transport contract.

A checkpoint may be created only while the referenced Task is currently `working`. Its target state may be `working`, `waiting`, or `completed`. `working → working` is intentionally valid so meaningful intermediate progress can increment revision without inventing a new lifecycle state. A `waiting` checkpoint requires both one of the existing Task wait reasons and a non-empty `next_step`. Cancellation remains a separate operator mutation rather than a checkpoint state.

Checkpoint text is deliberately bounded before persistence: summary is required and limited to 4096 UTF-8 bytes; optional `next_step` is limited to 2048 UTF-8 bytes. These are internal persistence bounds, not the future MCP serialized-payload limits.

The operation starts `BEGIN IMMEDIATE`, loads the explicit Task, verifies `expected_revision`, and rejects any non-`working` current state before doing mechanical work. It then invokes ADR-0013 changed-file calculation on the same connection. The mechanical result now also exposes the already-sampled current branch and dirty-path count in addition to baseline/current HEAD and changed paths; no extra source read is introduced for checkpoint persistence.

Only after mechanical calculation succeeds does the transaction:

1. update the Task state/wait reason and increment revision exactly once using a revision-qualified update;
2. insert one checkpoint row for that new revision;
3. insert every mechanically calculated changed path in sorted deterministic form;
4. insert one immutable `checkpoint` event whose database foreign key binds checkpoint identity, Task identity, and the same Task revision;
5. commit.

Any stale revision, invalid state, mechanical changed-file failure, SQLite constraint failure, or other exception rolls the transaction back. No checkpoint or event may survive without the corresponding Task revision mutation, and no Task revision mutation may survive without its checkpoint/event rows.

Checkpoint changed paths are cumulative net mechanical Task-time Workspace state relative to the Task-start baseline, not a delta only since the previous checkpoint. This preserves the semantics established in ADR-0013 and keeps checkpoint history self-contained.

The first event schema intentionally persists only checkpoint events. Task-created/resumed/operator-feedback/accept/cancel/session-activity events will be added with the orchestration that gives those event payloads precise semantics rather than predeclaring unused variants.

## Consequences

### Positive

- Same-state meaningful `working` checkpoints now have durable history and consume exactly one Task revision.
- Stale or parallel checkpoint writers cannot both commit against the same revision.
- Task state, semantic checkpoint data, mechanical changed paths, Git snapshot metadata, and timeline event are crash-consistent in one SQLite transaction.
- Historical checkpoint changed paths remain available even after the live Workspace changes again.
- No raw source content is persisted by the checkpoint layer.
- Public daemon/MCP checkpoint orchestration can later build on one tested persistence primitive rather than duplicating CAS and transaction logic.

### Costs and limits

- Schema version increases from 5 to 6.
- `BEGIN IMMEDIATE` is held while bounded mechanical changed-file calculation runs, matching the existing correctness-first Task-baseline creation approach and serializing competing Task writers.
- Checkpoints are accepted only from current `working` state; a waiting Task must first be resumed by the later explicit resume operation.
- Verification, Knowledge, bridge/session activity, operator feedback, acceptance, and cancellation events are not persisted by this slice.
- There is no public `task_checkpoint` API, daemon IPC, CLI, or MCP contract yet.
- The changed-path set is not model-facing here and therefore is not truncated; future IPC/MCP surfaces must impose their own item and byte budgets without corrupting durable mechanical truth.

## Verification

Automated tests must prove:

- schema v5 migrates to v6 without losing existing Tasks or baselines;
- database constraints reject a waiting checkpoint without a wait reason and reject an event whose revision does not match its checkpoint;
- `working → working` persists summary, next step, changed paths, Git metadata, one event, and exactly one revision increment;
- waiting checkpoints require wait reason plus next step;
- completed checkpoints retain committed changed paths even when the worktree is clean;
- a Task that is no longer `working` cannot receive another checkpoint without resume;
- stale revision conflicts leave Task/checkpoint/event state unchanged;
- mechanical changed-file failure leaves Task/checkpoint/event state unchanged;
- two writers using the same expected revision cannot both commit;
- checkpoint text bounds are enforced by UTF-8 bytes;
- deleting a Task cascades checkpoint, changed-path, and checkpoint-event history.
