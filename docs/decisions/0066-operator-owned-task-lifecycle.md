# ADR-0066: Proportionate tracking and operator-owned Task lifecycle

- **Status:** Accepted
- **Date:** 2026-09-14
- **Deciders:** Operator-requested workflow simplification

## Context

Mandatory Task creation for every investigation or small edit creates unnecessary durable work.
Agent completion also conflates finishing implementation with the operator accepting its result.
The dashboard cannot express all operator decisions conveniently, and a single delivery marker
cannot represent deployment to both test and production.

## Decision

`project_status` remains the first repository action. Start or resume a Task for substantial
changes, multi-step work, needed durable continuity, or an explicit request. Quick questions,
reads, and small local edits can proceed without a Task when tracking adds no useful continuity.
Start tracking if the scope grows. There is no new required justification field or approval step.
This supersedes ADR-0038's unconditional Task-before-diagnosis requirement. Search still follows
the exact/durable versus natural-language retrieval rules and does not itself require a Task.

One tracked outcome reuses its explicit Task identity through investigation, implementation,
checks, clarifications, subagents, and host restarts. An existing mutation requires the expected
revision; no title-based target inference is introduced. Agent checkpoints can write `working`
or `waiting` only. Ready work, including an audit, waits with `operator_review`. Operator
acceptance alone marks it completed. Existing completed checkpoint history remains readable.

The dashboard offers all four lifecycle states and their required waiting reasons. Explicit
operator transitions preserve identity, revision-CAS, and the one-working-Task-per-Workspace
invariant. Completion appends an acceptance event; other direct state changes record their
state and wait-reason snapshot. The existing reopen operation remains compatible. Agents do
not gain the operator's completion, cancellation, reopening, or deletion authority.

The operator can permanently delete an unnecessary Task after explicit confirmation. One
transaction checks Task identity and revision, deletes Knowledge whose `source_task_id` is
that Task, and deletes the Task and dependent checkpoints, events, and search projections.
Failure rolls the entire operation back. Repository files are never deleted. Stale forms and
agent writers cannot retarget or resurrect the deleted Task; deleted context refs fail closed.

Test and production deployment are independent boolean markers, including both true and both
false. Schema v22 backfills the booleans from the previous single marker. Current records and
new event snapshots carry both flags. Historical nullable markers remain readable; compatibility
presentation can represent both deployments rather than silently choosing one. Checkbox forms
must treat an unchecked value as false and preserve the two flags independently.

## Compatibility and activation

The migration is transactional; existing accepted/cancelled Tasks and history are not rewritten
as new agent work. Old agent clients attempting terminal checkpoints receive a bounded rejection
and must submit `waiting(operator_review)` instead. Reading old results remains supported.

Harness-owned previous Codex instruction bodies remain recognized for reconciliation; unknown
user-owned instructions are not overwritten. The generated bootstrap remains below 1 KiB.
This change updates source policy, not the active host configuration. Actual reconciliation
requires a host restart and fresh conversation, continuing the same unfinished Task.
The adapter continues using the existing `developer_instructions` field documented in the
[official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Verification

Cover agent terminal-write rejection and historical reads, operator state transitions and
one-working conflicts, stale revisions, deletion rollback/cascades/context removal, migration
of existing delivery markers, both deployment flags through HTTP forms, and failed-form recovery.
Bootstrap tests must prove the proportional tracking policy, operator-only completion, size
limits, and upgrades from recognized prior instruction bodies. Real subprocess MCP checks
verify the writer contract; real-host behavior remains separate acceptance evidence.
