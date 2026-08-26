# ADR-0022: Make Linux Claude Code installation ownership-aware and daemon-coordinated

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Repository architecture baseline

## Context

Harness already had the pieces required for useful local operation on POSIX: canonical per-user daemon paths and lazy autostart, a durable SQLite store, deterministic Workspace scan, a five-tool MCP bridge, a Claude Code adapter that safely owns one user-scope MCP registration, and rollback-aware generated skill projection. What was missing was the user-facing lifecycle that composes those pieces without bypassing their ownership boundaries.

A shell-script installer that edits Claude configuration directly would duplicate proprietary configuration semantics and weaken the adapter's collision/ownership checks. A CLI-side skill resolver that opens SQLite directly would violate daemon ownership. Uninstall also cannot be treated as only `claude mcp remove`: generated project skills are Harness-owned integration artifacts, while Project Intelligence is durable user data and must survive ordinary uninstall.

## Decision

For the currently supported Linux/POSIX + Claude Code slice, add `harness install` and `harness uninstall [--purge]`.

`harness install` performs non-mutating SQLite/FTS5 and Claude-registration ownership preflight first. A foreign MCP server named `harness` fails before canonical daemon state is created. The command then prepares/reuses the canonical daemon/database and delegates user-scope MCP registration to `ClaudeCodeAdapter`, preserving its idempotence, stale-owned replacement, and collision behavior. The registration always targets the exact Python interpreter containing the installed Harness package.

Project skill integration remains daemon-coordinated. Protocol v1 gains bounded `workspace_skills_reconcile` and `skill_cleanup` methods. `harness scan` performs its authoritative index reconciliation first; only when the discovered Claude adapter reports the exact current Harness-owned MCP registration does the CLI request Workspace skill reconciliation. The daemon serializes this work with authoritative scans, resolves the canonical external skill registry against daemon-owned Workspace state, and applies the existing ownership/collision-safe projection plan.

`harness uninstall` preflights Claude registration ownership, asks the daemon to reconcile Harness-owned Claude skill projections to an empty set across safely resolvable registered Workspaces, delegates MCP removal to the adapter, then requests a clean daemon shutdown through a new bounded local `shutdown` IPC method. Missing or Git-incompatible Workspace paths are skipped rather than guessed. Even for a path reused by another repository, cleanup still removes only exact Harness-owned projection markers and leaves user-owned content untouched.

Ordinary uninstall preserves the canonical database. `--purge` removes the canonical SQLite database plus known SQLite sidecars and the canonical `~/.harness/skills` registry only after daemon shutdown while holding the same database singleton lock used by the daemon; the empty lock file remains as a synchronization sentinel. It preserves unknown files outside those explicit Harness data roots and removes the `~/.harness` parent only when it becomes empty. The proprietary Claude CLI must be present during uninstall so Harness can verify ownership instead of directly editing Claude configuration.

## Consequences

- A Linux user with Claude Code can now execute an idempotent product lifecycle rather than manually invoking adapter internals.
- The daemon remains the owner of SQLite-backed skill resolution and scan serialization.
- Normal uninstall removes host/project integration artifacts while preserving Project Intelligence.
- Purge deletes the explicit canonical database and skill-registry roots, but remains narrower than recursive deletion of every Harness-looking directory; unknown files outside those roots are preserved.
- This does not constitute proprietary Claude Code acceptance. Host discovery of the registration, real `CLAUDE_PROJECT_DIR` injection, tool visibility, restart continuity, and actual skill visibility remain acceptance-gated.
- Other host adapters remain later work and do not silently share Claude's installation contract.

## Verification

Automated proof covers:

- registration-state classification and foreign-registration preflight before daemon mutation;
- install idempotence and exact installed-Python registration;
- real daemon skill reconcile/cleanup through strict IPC;
- automatic relevant-skill projection after `harness scan` only for a current owned Claude registration;
- ownership-safe cleanup that leaves user content untouched when a registered path is reused;
- clean daemon shutdown and Project Intelligence preservation by default;
- `--purge` removal of the canonical database after shutdown;
- a complete fake-Claude install → scan → uninstall lifecycle;
- the install/uninstall lifecycle from an isolated installed wheel.
