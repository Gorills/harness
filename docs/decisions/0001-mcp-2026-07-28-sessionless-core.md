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
5. Task continuity is Workspace/Task domain state. `task_start` creates/resumes a Task, returns its stable Harness `task_id` plus current `revision`, and changes the Workspace's current Task transactionally.
6. Every Task has a monotonically increasing revision. Workspace-current Task state is a read/relevance convenience, not a write identity or concurrency token.
7. `task_checkpoint` and every future mutating Task operation must explicitly carry `task_id` plus `expected_revision`. The daemon verifies Task/Workspace ownership and transition validity, then mutates only if the stored revision matches; success increments and returns the revision.
8. Revision mismatch is a non-mutating conflict. A stale request for Task A must neither mutate Task B nor overwrite a newer checkpoint for Task A; callers refresh/reconcile rather than silently replay stale semantic content.
9. Starting a different Task while a Workspace already has a `working` Task is a conflict; it is never an implicit switch.
10. Dashboard Task mutations use the same revision precondition at the application boundary.
11. Client metadata is diagnostic only and must not drive security or correctness-critical behavior.
12. Deprecated MCP roots and server-initiated request mechanisms are not correctness dependencies.
13. Harness `Task` must not be implemented as, identified by, or mapped one-to-one onto the MCP `io.modelcontextprotocol/tasks` extension. The five v1 Harness tools use ordinary bounded request/response semantics.

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
- Task write calls carry stable `task_id` + `expected_revision`; this small explicit-input cost prevents both cross-Task retargeting and lost updates from concurrent writers of the same Task.

## Verification

Core automated tests must demonstrate Task continuity across multiple independent MCP subprocess instances. They must prove that a delayed checkpoint for Task A cannot mutate Task B, that two writers of Task A using the same `expected_revision` cannot both commit, that a revision conflict performs no partial state/event/knowledge write, and that starting a different Task while one is `working` fails transactionally. MCP contract/wire tests must assert that `task_checkpoint` requires Harness `task_id` + `expected_revision`, returns the new revision on success, and that v1 does not advertise or use the MCP Tasks extension to implement Harness Task lifecycle. Dashboard tests must exercise the same conflict rule for Accept/feedback/cancel. Real-host tests must demonstrate the same Harness Task can be resumed after host/bridge restart.

## References

- https://modelcontextprotocol.io/specification/2026-07-28/
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md
