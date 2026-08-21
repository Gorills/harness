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
5. Task continuity is Workspace/Task domain state. `task_start` creates/resumes a Task, returns its stable Harness `task_id`, and changes the Workspace's current Task transactionally.
6. Workspace-current Task state is a read/relevance convenience, not a write identity. `task_checkpoint` and any future mutating Task operation must explicitly carry the intended Harness `task_id`.
7. The daemon verifies the supplied Task belongs to the resolved Workspace and that the transition is valid. A stale request for Task A must never mutate Task B merely because B became current before execution.
8. Starting a different Task while a Workspace already has a `working` Task is a conflict; it is never an implicit switch.
9. Client metadata is diagnostic only and must not drive security or correctness-critical behavior.
10. Deprecated MCP roots and server-initiated request mechanisms are not correctness dependencies.
11. Harness `Task` must not be implemented as, identified by, or mapped one-to-one onto the MCP `io.modelcontextprotocol/tasks` extension. The five v1 Harness tools use ordinary bounded request/response semantics.

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
- Task write calls carry a stable Harness `task_id`; this is a small explicit-input cost that prevents stale clients from mutating a newer Task.

## Verification

Core automated tests must demonstrate Task continuity across multiple independent MCP subprocess instances. They must also prove that a delayed `task_checkpoint(task_id=A)` cannot mutate Task B after the Workspace current Task changes, and that starting a different Task while one is `working` fails transactionally. MCP contract/wire tests must assert that `task_checkpoint` requires a Harness `task_id` and that v1 does not advertise or use the MCP Tasks extension to implement Harness Task lifecycle. Real-host tests must demonstrate the same Harness Task can be resumed after host/bridge restart.

## References

- https://modelcontextprotocol.io/specification/2026-07-28/
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md
