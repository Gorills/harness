# ADR-0034: Dashboard project removal and explicit Workspace relocation

- **Status:** Accepted
- **Date:** 2026-08-30
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0002](0002-host-integration-and-workspace-resolution.md),
  [ADR-0018](0018-daemon-workspace-watcher-reconciliation.md), and
  [ADR-0020](0020-dashboard-drilldown-realtime-design.md)

## Context

The dashboard retained every registered Project even after its checkout was no longer relevant or
had moved to another directory. Running `harness scan` at the new path cannot safely infer that an
independent Git common-directory path is the same logical Project. Creating a new Project in that
case loses dashboard continuity, while silently rewriting an old path could attach Tasks and
Knowledge to the wrong repository.

Project and Workspace have different lifetimes. A Project is the logical owner of durable Tasks and
Knowledge; a Workspace is one physical checkout and owns its absolute root, Git common directory,
live Git state, and derived index. The operator therefore needs separate explicit operations for
removing a logical Project and rebinding a moved physical Workspace.

## Decision

The Project detail page exposes a destructive deletion form behind a disclosure. It requires the
operator to type `УДАЛИТЬ`, and the server accepts it only when the posted `project_id` exactly
matches the capability-scoped Project page. Deletion runs in one `BEGIN IMMEDIATE` transaction and
removes the Project, its Workspaces, Tasks, checkpoints/events/baselines, Knowledge, anchors, and
derived search/index rows through the existing foreign keys and cleanup triggers. It does not
delete, rename, or otherwise modify repository files or project-local integration artifacts.

The Workspace detail page exposes an explicit relocation form accepting an absolute path. Harness
inspects that path as a live Git Workspace before beginning the registry write, canonicalizes the
new root and Git common directory, and rejects a root already registered to another Workspace or a
Git common directory associated with another Project. The posted `workspace_id` must exactly match
the Workspace page.

A successful relocation preserves `project_id`, `workspace_id`, Task history, Knowledge, and
Workspace-relative anchors. It atomically rewrites only the canonical physical Git identity and
deletes the Workspace's rebuildable index rows. This prevents results derived from the old folder
from being presented as current. The daemon watcher observes the changed durable Workspace identity
and rebuilds the index from the new filesystem through the existing authoritative scan path. The
dashboard also enqueues the Workspace on the watcher's existing invalidation channel after the
registry commit, so an unchanged Git HEAD/status token cannot delay the first rebuild.

Both actions use the dashboard's existing exact Host and same-origin mutation boundary. They are
progressively usable without JavaScript. No MCP tool or schema migration is added.

## Consequences

- Moving a checkout retains its logical history instead of creating a duplicate Project.
- Harness cannot prove that a moved standalone repository is the same logical codebase from Git
  paths alone; the explicit operator action is the authority for that association.
- Search/index counts may be temporarily empty after relocation until the watcher completes its
  next scan. Durable Tasks and Knowledge remain available.
- Project-local host settings are adapter-owned. The dashboard tells the operator to run
  `harness scan` in the new folder after relocation so absolute Codex configuration and other host
  integration state are reconciled through the established scan lifecycle.
- Project deletion is intentionally irreversible at the Harness database layer. Repository files
  and project-local generated integration files remain on disk, so deleting and later scanning the
  directory creates a new Project identity.
- A Project containing several Workspaces is deleted as one logical unit. Each moved Workspace is
  relocated independently.

## Verification

Automated tests must prove that relocation preserves Project/Workspace/Task identity, clears stale
derived index rows, rejects registered destinations without partial mutation, and renders the new
canonical path. Project deletion must require exact confirmation and page identity, cascade through
Task/Knowledge/index/search state, redirect away from the deleted page, and leave repository files
untouched. Existing dashboard Host/origin and hardened-response tests remain applicable.
