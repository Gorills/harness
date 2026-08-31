# ADR-0037: Serve Codex MCP through an authenticated daemon-owned loopback endpoint

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amends:** [ADR-0030](0030-codex-project-scoped-workspace-mcp.md), [ADR-0036](0036-source-checkout-global-dogfood.md)

## Context

The Codex adapter originally launched a project-scoped stdio bridge which then connected to the
canonical `harnessd` Unix socket. Real Codex Desktop and CLI acceptance showed that this bridge
can initialize and list tools while its later Unix-socket connection is denied by the Codex Linux
sandbox. `required = true`, prompt instructions, restarts, and local stdio placement did not repair
that boundary because initialization succeeded before daemon connectivity was exercised.

The previous acceptance runner reproduced the configured stdio command outside the proprietary
host sandbox. It proved the wire contract, but not the path that failed in Codex. Codex officially
supports Streamable HTTP MCP, static HTTP headers, and required-server startup failure. A real
`codex exec` spike on 2026-08-31 initialized a loopback Streamable HTTP server and completed a
model-selected tool call while command sandbox networking remained restricted.

## Decision

1. `harnessd` owns an MCP Streamable HTTP listener in addition to its private Unix IPC and
   dashboard listener. The canonical endpoint is `http://127.0.0.1:17375/mcp`; isolated
   development uses `127.0.0.1:17376`; explicit test/recovery sockets use an ephemeral port.
2. Codex project config uses `url`, `required = true`, and two exact static headers:
   `Authorization: Bearer <capability>` and `X-Harness-Workspace-Root: <canonical-root>`. It no
   longer launches `python -m harness.mcp_process`, forwards host environment variables, or
   reaches the daemon Unix socket from a Codex sandbox.
3. The bearer value reuses the daemon's persistent mode-`0600` loopback capability. Missing,
   duplicate, or incorrect authorization is rejected before MCP dispatch. The endpoint binds only
   IPv4 loopback and retains the SDK's DNS-rebinding Host checks.
4. Workspace identity remains explicit and project-scoped. HTTP initialization validates the
   absolute header root against daemon-owned Workspace state before returning success. Tool calls
   resolve the root from the current request header; MCP session IDs and `clientInfo` are never
   domain identity.
5. Failure to bind the MCP endpoint fails daemon startup. An unreachable daemon, invalid
   capability, absent root, or unregistered Workspace therefore prevents required Codex MCP
   initialization instead of producing a tool catalog that fails later.
6. The thin MCP tool implementation remains shared by stdio hosts and HTTP Codex. Both routes call
   daemon IPC and keep model-visible budgets; business state remains owned by `harnessd`.
7. A real Codex project config contains a private capability and an absolute machine path, so the
   Harness source checkout no longer tracks `.codex/config.toml`. Harness generates that ignored,
   mode-`0600` file through the same adapter used for other Workspaces. The repository tracks only
   a non-functional `.codex/config.toml.example` without credentials.
8. The old tracked `harness-dev` Codex stdio overlay is migration input only. Cursor and Claude
   source overlays remain unchanged. Global dogfood selects which daemon state is registered;
   Codex connects through the generated HTTP endpoint.

## Consequences

- Codex MCP availability no longer depends on sandbox access to arbitrary Unix sockets or broad
  command-network permissions.
- `required = true` now covers a daemon and Workspace health check during initialization.
- Token rotation makes owned Codex configs stale; `harness install --host codex` or `harness scan`
  reconciles them.
- The loopback bearer has the same OS-user trust intent as the dashboard capability. It must never
  be committed, logged, placed in model-visible responses, or included in acceptance evidence.
- Stdio remains supported for hosts whose accepted integration contract uses it, but it is no
  longer the Codex production transport.

## Verification

- unit tests prove exact HTTP TOML, ownership-aware stdio-to-HTTP migration, token refresh,
  absolute per-Workspace headers, cleanup, and Codex CLI discovery;
- daemon integration uses the official MCP SDK over real Streamable HTTP, rejects missing bearer
  and Workspace identity, and completes `project_status` against registered state;
- synthetic machine acceptance connects to the generated URL rather than recreating stdio;
- real Codex acceptance requires a completed model-selected HTTP MCP call and rejects any project
  action before `project_status`;
- disconnected-daemon and invalid-capability checks fail MCP initialization.
