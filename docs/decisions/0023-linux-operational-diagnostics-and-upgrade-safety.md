# ADR-0023: Linux operational diagnostics and upgrade-safe runtime identity

- **Status:** Accepted
- **Date:** 2026-08-26
- **Amended:** 2026-08-28
- **Deciders:** Repository architecture baseline

## Context

The first Linux/Claude Code installation slice made install/uninstall mechanically safe, but two release-level gaps remained. First, `harness doctor` still defaulted to an SQLite-only probe and could not explain stale daemon, host registration, Project/index, generated-skill, or dashboard state. Second, reinstalling Harness could leave an already-running daemon executing code loaded from an older installation. Comparing only package version or Python path is insufficient because an in-place reinstall can replace package bytes without changing either value.

## Decision

On the supported Linux/POSIX profile, bare `harness doctor` is the read-only operational diagnostic. It inspects the canonical paths and permissions, SQLite/FTS5, Claude adapter and Harness-owned registration, daemon runtime identity, canonical database, registered Project/Workspace Git identity, deterministic index freshness, canonical skill registry and expected generated projections, dashboard subsystem state, and stale integrations. Warnings describe absent/lazy/repairable state and do not make the command fail; integrity, ownership, compatibility, or runtime mismatches are failures. Workspace live checks are bounded by both per-Workspace and aggregate deadlines and never reconcile state. Those bounds distinguish three operator-visible outcomes: **unavailable** means non-timeout Git/identity inspection failed for a named Workspace; **timed out** or **failed** means Workspace-identity, index, or generated-skill inspection hit the deadline or (for index and generated skills) raised a non-timeout inspection error for a named Workspace; **doctor budget** means remaining named Workspaces were not inspected because the count limit or aggregate deadline was reached. Timeouts and budget truncation remain warnings. The per-Workspace deadline is aligned with the daemon scan bound (30s) so a ~10k-file live inventory can complete; the aggregate deadline (90s) covers a typical multi-Workspace install (at least 11 live Workspaces, including one large index) while remaining finite.

`harness doctor --runtime-only` preserves the original in-memory SQLite/FTS5-only diagnostic. `harness doctor --database PATH` remains a read-only selected-database recovery/development check. Quiescent WAL databases are inspected through an immutable SQLite snapshot so doctor does not create `-wal`/`-shm` artifacts merely by reading them; when a real WAL sidecar already exists, the read-only connection follows live WAL frames instead. Operational Project/index/skill checks share one SQLite read transaction so the report is a point-in-time durable-state snapshot. No doctor mode starts the daemon, starts the dashboard, registers a host, scans a Workspace, reconciles skills, or mutates durable state. After ADR-0025 the dashboard listener starts with the daemon; a live daemon with `dashboard_running=false` is a warning, not a lazily inactive success.

The daemon exposes additive protocol-v1 `runtime_diagnostics` containing schema version, package version, absolute Python executable, a SHA-256 fingerprint of the installed Harness Python source tree frozen when the daemon process starts, Project/Workspace counts, and dashboard-running state. The fingerprint is computed without following symlinks, is size-bounded, and fails if package entries change while being read.

`harness install` reuses a daemon only when schema, package version, Python executable, and frozen code fingerprint exactly match the current installed runtime. Otherwise it requests clean shutdown, waits for the existing singleton boundary to release, starts the current runtime, and verifies the new daemon identity before changing the Claude registration. Cursor project preflight lists registered Workspace roots from the existing canonical database before that restart, so the listing is read-only and accepts a migratable older schema (`<= SCHEMA_VERSION`, Workspace identity columns stable since schema v2). A newer schema remains refused. After the current daemon is verified, install enumerates again under the current schema. The immediately preceding protocol-v1 daemon predates `runtime_diagnostics`; a structured `invalid_request` for that additive method is treated as the one supported compatibility transition. Harness validates its legacy `status`, refuses a newer schema, uses the already-supported `shutdown`, and then verifies the restarted current runtime. No other diagnostics error authorizes replacement. `harness uninstall` likewise upgrades/restarts a stale or legacy owned daemon before project-skill cleanup so destructive integration cleanup does not execute under old loaded code.

The installed CLI also exposes read-only `harness skills list` for the canonical external skill registry.

## Consequences

- Reinstall from another virtual environment and in-place package replacement are both detected without trusting mutable distribution metadata in an already-running daemon.
- A healthy `doctor` is meaningful for local operation rather than merely proving that SQLite imports.
- Clean-machine absence remains warning-only; unsafe filesystem objects, foreign registration ownership, stale daemon identity, unsupported schema, or invalid canonical data are failures.
- Doctor can be more expensive than the old runtime-only probe because it may hash live Workspace inventories, but it is explicitly bounded and read-only.
- Real proprietary Claude Code discovery/tool visibility is still acceptance-gated; operational diagnostics cannot substitute for vendor-host acceptance.

## Verification

Automated tests and the installed-wheel smoke must prove:

- default doctor is read-only on a clean machine and reports warning-only absent state;
- unsafe canonical database/skill-registry objects fail closed without following or mutating them;
- stale SQLite sidecars are reported without deletion;
- initialized database inspection is read-only and does not create quiescent WAL sidecars while still observing live WAL frames when present;
- live Project/index/skill/dashboard state is reported from authoritative sources without reconciliation, and doctor names Workspaces whose identity is unavailable or timed out, whose index/skill inspection timed out or failed, or who were skipped by the count/time budget;
- doctor live-Workspace deadlines remain finite and tests can still expire them;
- daemon diagnostics preserve exact schema/version/interpreter/code fingerprint contracts;
- the previously released protocol-v1 daemon without `runtime_diagnostics` is upgraded only through validated legacy `status` plus clean `shutdown`;
- install/uninstall Workspace-root listing before daemon restart accepts a migratable older schema and still refuses a newer schema;
- reinstall through a second Python 3.13 environment replaces the stale daemon and Claude registration;
- a stale code fingerprint is replaced even when version and interpreter path are unchanged;
- installed-wheel lifecycle reaches a full doctor with zero failures after install and scan.
