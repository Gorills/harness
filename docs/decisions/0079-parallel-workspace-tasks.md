# ADR-0079: Independent parallel Tasks in one Workspace

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** Explicit operator request

## Context

Two agents pursuing different outcomes in the same checkout could not create independent Tasks.
The original specification §49 and ADR-0011 used one working Task per Workspace as a complexity
bound. This also blocked parallel research or unrelated code work, although existing Task writes
already carry explicit identity and revision CAS. Workspace exclusivity is not a filesystem lock
and cannot establish edit authorship.

## Decision

Allow multiple independent working Tasks in one Workspace. Create, resume, feedback, reopen and
operator state changes preserve all other Tasks. Existing transactions, Task IDs, revision CAS,
baseline capture, lifecycle/event atomicity, Git applicability and operator-owned completion remain.
One outcome still uses one durable Task across diagnosis, implementation, checks and continuation.
Neither Task titles nor transport/client/session identities infer a write target or deduplicate work.

Schema v25 drops the old partial unique index and adds a nonunique active-Task lookup index.
No Task, baseline, checkpoint, event, Knowledge or Git evidence rows are rewritten or removed.

`project_status` accepts optional `task_id`. Explicit selection verifies Workspace ownership,
working/waiting state and current Git applicability; unavailable identities fail without disclosing
their title or checkpoint. Without an ID, the unique eligible working Task supplies continuity;
if none works, the unique eligible waiting Task does. More than one Task at that priority supplies
no selected Task, checkpoint, next step or feedback. Recency is not ownership.

Status adds up to five eligible `active_tasks` (working/waiting summaries only), the total eligible
`active_task_count`, and `active_tasks_truncated`. Apply Git filtering before list limits and counts.
Explicit selection works beyond the bounded list. Existing single-Task fields remain compatible
for an unambiguous selection. The model exposure budget remains 10 KiB.
The bridge accounts for both SDK text and structured content plus wire overhead; when necessary
it shortens the active list further, preserves selected continuity, and marks truncation.
Dashboard lists/counts
retain their operator archive semantics; ambiguous current focus asks the operator to choose from
the Task list, rather than selecting a Task by last update.

Mechanical changed paths remain the net shared-Workspace delta since each Task's own baseline.
They may include other agents or human edits; the dashboard labels this limit. They are not Task
authorship. Verification reflects the code state checked, not an isolated per-Task tree. Separate
worktrees remain the recommended way to isolate overlapping edits, branches and verification.

## Alternatives

- Mandatory worktrees preserve the old invariant but impose isolation even on parallel research.
- One Task per agent/session would couple durable work to unreliable transient host identity and
  prevent several agents collaborating on one outcome.
- Removing only the SQLite index leaves domain conflicts and arbitrary continuation selection.
- Automatic file ownership/leases or worktree orchestration are separate product outcomes and are
  not needed for independent Task histories.

## Compatibility and recovery

This amends the one-working-Task portions of ADR-0001, ADR-0011, ADR-0015, ADR-0031, ADR-0067 and
the specification audit; original specification text stays historical. IPC protocol version remains
1, but its private Task-status shape gains bounded fields and its request gains optional `task_id`.
The daemon and bridge upgrade together through the supported installed-runtime refresh. An old
runtime refuses a newer schema; rolling back the binary alone is unsupported. Restore a pre-upgrade
backup only if discarding later accepted writes is intentional; otherwise use forward repair.
Migration runs in the existing serialized transaction and rolls back index changes on failure.

After activation, affected hosts must restart and use fresh conversations to obtain the updated
tool schemas and bootstrap instructions; the same unfinished Harness Task IDs remain valid.

## Verification

Cover v24 migration/data preservation and failed-migration rollback; concurrent distinct starts;
independent baselines, revisions and history; same-Task stale-write rejection; resume/feedback/
reopen/operator-state transitions beside other Tasks; ambiguous and explicit status through IPC
and independent MCP processes; list truncation and exposure budgets; Git filtering before counts
and selection; foreign/terminal/unavailable identities; and dashboard focus/list semantics.
