# ADR-0019: Make dashboard human review a revision-CAS Task workflow

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** Repository architecture baseline

## Context

The first dashboard slice established a daemon-owned loopback Projects overview over the same durable Harness database as MCP. The next audited roadmap item is the human review loop: a Task checkpointed as `waiting(operator_review)` must be accepted or receive operator feedback without inventing a new Task, and dashboard cancellation must not bypass the existing explicit-identity/revision concurrency contract.

Feedback also has to survive bridge/host restart. The specification requires `project_status` to expose pending operator feedback, so storing feedback only in browser state or rendering it only in the dashboard would break continuity for a fresh agent session.

## Decision

Schema version 10 expands immutable `task_events` with three operator event types: `accepted`, `operator_feedback`, and `cancelled`. Operator feedback is bounded to 1024 UTF-8 bytes and is stored directly on its event; non-feedback events cannot carry feedback text. Existing event IDs are preserved during migration and no operator history is fabricated for older Tasks.

Add transport-independent domain operations `task_accept`, `task_feedback`, and `task_cancel`. Every operation requires explicit Workspace identity, Task identity, and positive `expected_revision`, rechecks them inside one `BEGIN IMMEDIATE` transaction, performs the existing Task revision-CAS transition, appends exactly one immutable operator event, and commits both state and history together. Event failure rolls the transition back.

`task_accept` is valid only for `waiting(operator_review)` and transitions that Task to `completed`. `task_feedback` is valid only for `waiting(operator_review)`, persists normalized bounded feedback, and transitions the **same Task** to `working`; the one-working-Task invariant remains authoritative, so feedback cannot resume a waiting Task when another distinct Task is working in the Workspace. `task_cancel` transitions an explicit `working` or `waiting` Task to `cancelled`; terminal Tasks stay terminal.

The daemon's bounded Workspace Task status includes `pending_operator_feedback` only when an `operator_feedback` event is attached to the Task's **current working revision**. The MCP `project_status` response exposes that field. Once the agent checkpoints and the Task revision advances, the old feedback remains immutable history but is no longer pending.

Dashboard actions call those domain operations directly through daemon-owned local state; they do not edit Task tables ad hoc and do not add a second mutation workflow. The Projects page renders actions with the Task revision observed in the same dashboard read. Mutation POSTs are accepted only on the private capability path, require the exact loopback `Host`, exact same-origin `Origin`, a bounded `application/x-www-form-urlencoded` body, singular known fields, and the rendered revision token. Cross-site, malformed, stale, wrong-Workspace, and ineligible-state requests are non-mutating. All success/error HTTP responses keep the dashboard's hardened no-store/CSP/nosniff/no-referrer policy and omit `Server`/`Date` headers.

No new MCP tool is added. Accept/feedback/cancel are human-facing operations; agent continuity is provided by the existing `project_status` and Task tools.

## Consequences

### Positive

- Human feedback continues the same durable Task across agent/bridge restart.
- Dashboard writes obey the same explicit identity and revision-CAS invariants as other Task writes.
- State transition and operator history are crash-consistent and rollback together.
- Pending feedback is visible to the next agent session without exposing full Task history.
- Browser-originated mutation has a concrete CSRF/rebinding boundary in addition to the random capability path.

### Costs and limits

- Schema version increases from 9 to 10 and `workspace_task_status` gains one additive bounded field.
- Feedback is intentionally bounded rather than serving as an unbounded conversation transcript.
- Projects overview remains the only dashboard page; Project/Task detail timelines, dashboard search, verification UI, and SSE are later slices.
- A local process running as the same OS user can still obtain the capability URL through protected IPC; browser same-origin checks are protection against web-origin attacks, not a second OS-user authentication layer.

## Verification

Automated tests must prove:

- schema v9 migrates to v10 preserving existing event IDs without fabricating operator history;
- database constraints reject malformed operator event payload/linkage and duplicate operator actions for one Task revision;
- Accept/feedback/cancel require explicit ownership and revision CAS and keep terminal-state rules;
- feedback resumes the same Task, obeys the one-working-Task invariant, and event persistence failure rolls back the state transition;
- dashboard actions reject missing/foreign Origin, wrong Host, malformed/oversized bodies, stale revisions, and keep hardened response headers;
- a real human review sequence can checkpoint to operator review, receive dashboard feedback, surface that feedback through a fresh MCP bridge `project_status`, clear pending feedback after a checkpoint, return to review, and Accept the same Task to completion.
