# ADR-0001: Target MCP 2026-07-28 with a sessionless core

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline

## Context

The original specification models an Agent Session as a concrete MCP client session and states that after `task_start` the MCP session is bound to a Task so subsequent calls normally do not repeat `task_id`.

The current MCP 2026-07-28 specification removed the initialize/initialized handshake, protocol-level sessions, and `Mcp-Session-Id` on the modern path. Request protocol version, client capabilities, and optional/self-reported client information are carried per request. The official Python SDK v2 is the current stable line and supports this modern path plus older protocol revisions.

A Harness domain invariant tied to an MCP session would therefore depend on legacy protocol behavior and could fail across modern hosts, reconnects, or SDK evolution.

## Decision

1. Production uses the official MCP Python SDK v2; Harness does not implement its own protocol stack.
2. Core domain/application behavior is sessionless with respect to MCP.
3. Harness mints its own bridge/activity identifiers for observability and history.
4. `AgentSession` is an observed Harness record, not protocol identity.
5. Task continuity is Workspace/Task domain state. `task_start` changes the Workspace's current Task transactionally.
6. Subsequent operations resolve the current Task from Workspace state when unambiguous; ambiguity fails safely or requires explicit identity.
7. Client metadata is diagnostic only and must not drive security or correctness-critical behavior.
8. Deprecated MCP roots and server-initiated request mechanisms are not correctness dependencies.

## Consequences

### Positive

- Correct across modern sessionless MCP.
- Host restart/reconnect does not lose Task continuity.
- Domain state is testable without proprietary host lifecycle semantics.
- Legacy clients remain supportable through the official SDK rather than core branching.

### Costs

- Workspace resolution becomes an explicit integration problem.
- Activity/session terminology must be precise in schema/UI/docs.
- Any feature that needs multi-call conversational server state must model that state explicitly in Harness rather than hiding it in transport state.

## Verification

Core automated tests must demonstrate Task continuity across multiple independent MCP subprocess instances. Real-host tests must demonstrate the same Task can be resumed after host/bridge restart.

## References

- https://modelcontextprotocol.io/specification/2026-07-28/
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md
