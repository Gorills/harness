# ADR-0073: Native Windows development runtime

- **Status:** Proposed
- **Date:** 2026-09-27
- **Deciders:** Windows development task

## Context

The original product requirements and architecture allow Windows named-pipe IPC, but the
implemented runtime, private file checks, vault subprocess and checkout launcher were POSIX-only.
Windows developers need a local checkout that can start without a Linux subsystem or a global
Harness installation. This ADR extends ADR-0007 for the Windows development path; it does not
change POSIX defaults.

## Decision

- `scripts/dev.cmd` invokes `scripts/dev.ps1`, pins uv 0.12.5 and Python 3.13 under the ignored
  checkout overlay, and sets isolated state, runtime, skills and temporary paths under `.harness`.
  The wrapper does not install Harness globally.
- Windows defaults use `%LOCALAPPDATA%` for durable state when no XDG override is supplied, and a
  current-user temporary directory for runtime data. The checkout wrapper overrides both paths.
  SQLite, dashboard and skills live under directories validated with Windows owner and DACL checks.
- Daemon IPC uses a Windows named pipe derived from the canonical runtime path. A random secret in
  a private runtime file authenticates both sides using the Python multiprocessing HMAC exchange.
  The existing bounded, versioned request/response schema remains unchanged. A separate Windows
  file lock preserves the singleton daemon/database rule.
- The vault subprocess uses Windows file ACL checks, a nonblocking file lock and a parent-liveness
  reader thread. The manual master-password and no-password modes are supported. Device unlock
  through Linux Secret Service remains unavailable on Windows until a native keychain backend is
  designed and verified. The POSIX address-space limit is not asserted for Windows.
- `scripts/dev.cmd connect-codex` writes an ignored project-local `.codex/config.toml` with an
  authenticated loopback MCP endpoint and explicit Workspace root. It requires the isolated daemon
  to be running, refuses tracked or unknown existing config, and never changes user-global Codex
  settings. A full Codex restart and new conversation are required after connecting.

## Consequences and verification

The Windows checkout has a native CLI, daemon, dashboard, stdio MCP and project-local Codex
connection path. Windows filesystem privacy is based on owner and DACL rather than POSIX mode
bits. Linux mode-bit fixtures are not evidence for Windows ACL behavior; Windows-specific tests
exercise the private path check, named-pipe authentication, singleton lock and daemon status.

The local connection is tested with a synthetic Git Workspace. Real Codex project discovery after
restart remains a real-host acceptance step. Global installation, Cursor host activation, recovery
and Windows device-keychain support remain separate tasks; this ADR does not claim those paths.
