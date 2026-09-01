# ADR-0031: Add operator Task tracking, explicit reopen, and searchable history

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0011](0011-task-persistence-and-revision-cas.md), [ADR-0019](0019-dashboard-human-review-loop.md), [ADR-0020](0020-dashboard-drilldown-realtime-design.md), [ADR-0021](0021-project-intelligence-retrieval.md)
- **Amended by:** [ADR-0040](0040-dashboard-root-url-and-project-index.md)

## Context

The existing Task lifecycle answers whether agent work is active, waiting, completed, or cancelled. It does not answer the operator's separate delivery questions: which Jira work item owns the work, whether the result is being deployed to test or production, and what human notes explain its current position. Reusing `waiting` reasons for those facts would mix agent coordination with delivery tracking and recreate the workflow-state explosion rejected by the original specification.

Completed and cancelled Tasks were also terminal under the original v1 transition primitive. Human review feedback already resumes the same waiting Task, but an operator could not explicitly reopen a terminal Task when later work proved necessary. Dashboard search covered indexed paths while durable Task search was available only through Project Intelligence and did not index Git branches, Jira links, or general operator comments.

## Decision

Keep `working`, `waiting`, `completed`, and `cancelled` as the only lifecycle states. Add two nullable operator-owned Task fields that do not affect lifecycle rules:

- one bounded HTTP(S) Jira URL without embedded credentials;
- one bounded delivery marker: `deploy_test` or `deploy_prod`.

Add immutable bounded `operator_comment`, `jira_link_updated`, and `operator_status_updated` Task events. A generic operator comment is history only: unlike `operator_feedback`, it does not change lifecycle state and is not exposed as pending agent feedback. Setting or clearing Jira/status and adding a comment are explicit Task mutations. They require Workspace identity, Task identity, and `expected_revision`, increment the Task revision exactly once, and persist state plus event in one transaction.

Add an explicit `task_reopen` operator operation. It is valid only for `completed` or `cancelled`, requires revision CAS, transitions the same Task to `working`, and appends one `reopened` event atomically. It remains subject to the one-working-Task-per-Workspace invariant. Ordinary `task_resume` and the model-facing `task_start(task_id=...)` continue to reject terminal Tasks, so reopen is never inferred from agent activity.

Expose these operations only through the human dashboard in this slice; do not add MCP tools or daemon IPC methods. Task detail shows the Jira link, operator status, comments, and controls. Project/Workspace overview cards show the current operator marker and a direct Jira link. All POSTs retain the dashboard's existing capability path, same-origin/Host admission, bounded form parsing, and rendered revision token.

Extend the rebuildable Task FTS index with Jira URL, operator status (including Russian display terms), operator comments, baseline branch, and checkpoint branch. Workspace dashboard search combines its existing bounded indexed-path results with Project-scoped Task hits and links those hits to Task detail. The home dashboard searches Task-history FTS across every registered Project on the daemon. Authoritative Task/event/checkpoint/baseline rows remain the source of truth; FTS remains derived candidate data.

## Consequences

- An operator can understand delivery position without changing the agent lifecycle state.
- Notes, Jira changes, delivery markers, and reopen actions remain durable and ordered in the Task timeline.
- A stale dashboard page or agent checkpoint conflicts after any operator mutation instead of overwriting newer human state.
- Reopen preserves Task identity and history while remaining explicit and conflict-safe.
- v1 has one Jira link and two fixed delivery markers; custom status definition and multiple issue trackers remain future work.
- Generic comments are not agent instructions. Work-requesting feedback should still use review feedback or reopen plus the normal Task workflow.

## Verification

Automated tests must prove:

- schema v12 migrates to v13 without losing event IDs or existing FTS history;
- malformed event payloads and unsupported status values fail closed;
- comment/Jira/status/reopen mutations require explicit ownership and revision CAS and roll back atomically on failure;
- reopen keeps the same Task ID, rejects non-terminal Tasks, and cannot violate the one-working-Task invariant;
- Task search finds title/checkpoint history plus Git branch, Jira key/URL, operator comments, and Russian delivery-marker text;
- dashboard Task pages escape persisted text, show Jira/status/history, and submit bounded same-origin CAS actions;
- workspace search returns bounded linked Task hits without raw source or unrelated Project data;
- home dashboard Task search finds Tasks across registered Projects without raw source.
