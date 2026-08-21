# ADR-0001: Target MCP 2026-07-28 with a sessionless core

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline

## Context

The original specification models an Agent Session as a concrete MCP client session and states that after `task_start` the MCP session is bound to a Task so subsequent calls normally do not repeat `task_id`.

The current MCP 2026-07-28 specification removed the initialize/initialized handshake, protocol-level sessions, and `Mcp-Session-Id` on the modern path. Request protocol version, client capabilities, and optional/self-reported client information are carried per request. The official Python SDK v2 is the current stable line and supports this modern path plus older protocol revisions.

A Harness domain invariant tied to an MCP session would therefore depend on legacy protocol behavior and could fail across modern hosts, reconnects, or SDK evolution.

The same protocol revision also defines the `io.modelcontextprotocol/tasks` extension for long-running individual MCP requests. Its `Task` vocabulary is not equivalent to the Harness domain `Task`, which represents durable project work across calls, hosts, and human feedback. The shared name creates a correctness risk if the two lifecycles are accidentally coupled.

## Decision

1. Production uses the official MCP Python SDK v2; Harness does not implement its own protocol stack.
2. Core domain/application behavior is sessionless with respect to MCP.
3. Harness mints its own bridge/activity identifiers for observability and history.
4. `AgentSession` is an observed Harness record, not protocol identity.
5. Task continuity is Workspace/Task domain state. Creating through `task_start` has no prior Task revision: it transactionally enforces the one-working-Task invariant and returns the new Harness `task_id` plus initial `revision`.
6. Every existing Task has a monotonically increasing revision. Workspace-current Task state is a read/relevance convenience, not a write identity or concurrency token.
7. `task_start(task_id=...)` is idempotent when the referenced Task is already the Workspace's `working` Task and returns its current revision without mutating Task state. If resume would change existing Task state (for example `waiting → working`), it requires `expected_revision` and uses compare-and-set. `completed`/`cancelled` Tasks are not reopened by `task_start` in v1.
8. `task_checkpoint` and every other mutating operation on an existing Task must explicitly carry `task_id` plus `expected_revision`. The daemon verifies Task/Workspace ownership and transition validity, then mutates only if the stored revision matches; success increments and returns the revision.
9. Revision mismatch or a required-but-missing revision is non-mutating. A stale request for Task A must neither mutate Task B nor overwrite a newer checkpoint for Task A; callers refresh/reconcile rather than silently replay stale semantic content.
10. Starting a different Task while a Workspace already has a `working` Task is a conflict; it is never an implicit switch.
11. Dashboard Task mutations use the same revision precondition at the application boundary.
12. Client metadata is diagnostic only and must not drive security or correctness-critical behavior.
13. Deprecated MCP roots and server-initiated request mechanisms are not correctness dependencies.
14. Harness `Task` must not be implemented as, identified by, or mapped one-to-one onto the MCP `io.modelcontextprotocol/tasks` extension. The five v1 Harness tools use ordinary bounded request/response semantics.

## Consequences

### Positive

- Correct across modern sessionless MCP.
- Host restart/reconnect does not lose Task continuity.
- Domain state is testable without proprietary host lifecycle semantics.
- Legacy clients remain supportable through the official SDK rather than core branching.
- Harness task continuity cannot be accidentally replaced by a protocol-level long-running-request primitive with different semantics.

### Costs

- Workspace resolution becomes an explicit integration problem.
- Activity/session terminology must be precise in schema/UI/docs.
- Any feature that needs multi-call conversational server state must model that state explicitly in Harness rather than hiding it in transport state.
- Mutations of an existing Task carry stable `task_id` + `expected_revision`; this small explicit-input cost prevents cross-Task retargeting, same-Task lost updates, and resume/checkpoint races. New Task creation is serialized by the Workspace working-Task invariant instead of a nonexistent prior revision.

## Verification

Core automated tests must demonstrate Task continuity across multiple independent MCP subprocess instances. They must prove that a delayed checkpoint for Task A cannot mutate Task B, that two writers of Task A using the same `expected_revision` cannot both commit, that a revision conflict performs no partial state/event/knowledge write, that two concurrent new-Task starts cannot both establish distinct `working` Tasks, and that a state-changing `task_start` resume cannot bypass CAS or race Dashboard feedback. MCP contract/wire tests must assert that `task_checkpoint` requires Harness `task_id` + `expected_revision`, that state-changing resume requires `expected_revision`, that idempotent resume of an already-working Task does not mutate revision, and that v1 does not advertise or use the MCP Tasks extension to implement Harness Task lifecycle. Dashboard tests must exercise the same conflict rule for Accept/feedback/cancel. Real-host tests must demonstrate the same Harness Task can be resumed after host/bridge restart.

## References

- https://modelcontextprotocol.io/specification/2026-07-28/
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md
