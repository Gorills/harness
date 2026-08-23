# ADR-0011: Establish Task persistence and revision CAS before task_start orchestration

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

The audited delivery sequence places Task lifecycle after the first search contract. The architecture requires Task identity to survive host and bridge restarts, enforces at most one distinct `working` Task per Workspace, and requires every mutation of an existing Task to carry stable `task_id` plus `expected_revision`.

The original specification also requires `task_start` to capture a mechanical Git/index baseline automatically. That baseline is needed later to distinguish changes made during the Task from dirty state that already existed when work began. Exposing `task_start` before that baseline contract exists would create an incomplete user-visible lifecycle and would make later changed-file semantics ambiguous.

A smaller prerequisite can be independently verified first: durable Task state and optimistic-concurrency rules in the daemon-owned SQLite domain. This foundation must not pretend to be the `task_start` or `task_checkpoint` orchestration layer.

## Decision

Add schema version 4 with a durable `tasks` table and a domain module for Task persistence/state transitions.

The persisted Task foundation contains:

- Harness-minted Task ID;
- owning `workspace_id`;
- bounded title, normalized by the domain before persistence;
- state: `working`, `waiting`, `completed`, or `cancelled`;
- waiting reason only when state is `waiting`: `operator_review`, `operator_input`, or `external`;
- positive monotonically increasing `revision`;
- creation and update timestamps.

The database enforces the structural invariants that must survive any caller bug:

1. Task rows reference an existing Workspace and cascade with Workspace deletion.
2. At most one row with state `working` may exist for a Workspace, using a partial unique index.
3. Revision is positive.
4. Titles are non-empty and bounded to 256 UTF-8 bytes.
5. Waiting state requires a non-null allowed waiting reason; every other state requires `wait_reason IS NULL`.

The domain API provides:

- creation of one internal working Task record with initial revision `1`;
- lookup by stable Task ID;
- lookup of the current working Task for one Workspace;
- explicit existing-Task state transitions using `task_id` plus `expected_revision`.

Existing-Task transitions run in `BEGIN IMMEDIATE` transactions. A revision mismatch is non-mutating. Every successful transition increments revision exactly once. `completed` and `cancelled` are terminal in v1. A transition from `waiting` back to `working` fails if another Task is already working in the Workspace.

This slice intentionally does **not** expose `task_start`, `task_checkpoint`, daemon IPC, CLI, MCP, Task events, summaries, verification, Knowledge, Git baseline capture, changed-file calculation, or index-freshness capture. Those remain separate bounded slices. In particular, no caller should treat the internal creation primitive as the final `task_start` contract because it does not yet capture the required baseline.

## Consequences

### Positive

- Stable Harness Task identity and revision CAS become testable independently from transport and Git-baseline orchestration.
- The one-working-Task rule has a database backstop in addition to domain checks.
- Parallel stale writers cannot silently overwrite each other.
- Waiting-reason validity is enforced both in Python and SQLite.
- Later daemon/MCP/dashboard mutations can share one concurrency rule instead of growing interface-specific bypasses.

### Costs and limits

- Schema version increases from 3 to 4.
- This is foundation state, not a usable public Task workflow yet.
- No Task event history exists in this slice; events arrive with the checkpoint/start orchestration that gives them meaningful payload semantics.
- No Git/index baseline is stored yet, so the internal create primitive must not be surfaced as `task_start`.
- Same-state checkpoints are not represented by this state-transition primitive; checkpoint persistence will define its own revision-incrementing mutation semantics.

## Verification

Automated tests must prove:

- forward migration from schema v3 preserves existing indexed data and creates Task storage;
- new and older bootstrap paths converge to schema v4;
- bounded UTF-8 title validation;
- one working Task per Workspace at both domain and SQLite levels;
- waiting reason validity at both domain and SQLite levels;
- valid `working ↔ waiting` and terminal transitions;
- terminal Tasks cannot be reopened by this v1 primitive;
- stale revision mismatch leaves state unchanged;
- two parallel writers using the same expected revision cannot both commit;
- a waiting Task cannot resume to `working` while another Task is already working in the same Workspace.
