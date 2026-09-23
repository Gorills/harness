# ADR-0072: Concrete skill previews and bounded source evidence

- **Status:** Accepted
- **Date:** 2026-09-23
- **Builds on:** [ADR-0032](0032-continuous-project-skill-reconciliation.md),
  [ADR-0036](0036-source-checkout-global-dogfood.md),
  [ADR-0042](0042-project-stack-skill-selection.md),
  [ADR-0064](0064-opt-in-workspace-init-and-skill-include.md)
- **Does not rewrite:** the original specification or host skill-discovery contracts.

## Context

The delivered-skills audit found two gaps. Dependency-only detection missed Python stdlib
services and embedded web interfaces. Dashboard facet switches did not show the actual skills
or distinguish saved policy from successful filesystem delivery. A source checkout intentionally
excluded from global reconciliation could therefore appear successfully configured even though
its local skill files were stale.

## Decision

1. Keep the durable Auto/Included/Excluded facet policy and the same resolver. The settings page
   reads the actual registry, resolves each Workspace, and previews exact added, removed and
   retained skill IDs before an explicit Apply. Show short purposes, original trigger descriptions,
   detected facets, selection reasons and the shared baseline. Facets may overlap; removing one
   facet does not necessarily remove a skill selected for another. Changes cover all Project
   Workspaces and future matching registry additions, not a permanently frozen per-skill list.
2. Separate the configured selection from filesystem delivery. Inspect owned projections and
   report current files, pending refresh/removal, collisions, missing host integration, source
   overlay exclusion or inspection errors per Workspace. Saving policy is not proof of delivery.
   The current mode can be applied again after repairing a collision. A current file snapshot
   never proves that an already-running host conversation loaded those instructions.
3. Use the same active runtime host-profile selector for preview and mutation, including the
   isolated development profile defaults. Keep reconciliation in the existing daemon/runtime
   boundary. Policy remains saved when a later per-Workspace projection fails; present the
   resulting diagnostic rather than a success claim.
4. Read the catalog once and detect the stack once per Workspace for all candidate modes. The
   settings snapshot uses a shared three-second cooperative budget for stack and projection
   reads across Workspaces. Registry loading is not deadline-aware, and the source-overlay Git
   probe has its own five-second timeout; there is no strict total wall-clock guarantee. Refuse
   existing Cursor configuration files larger than 1 MiB before the overlay probe. It performs no
   scan or mutation. Only settings requests
   and their `project_settings` SSE fingerprints load this snapshot; ordinary overview reads do
   not. Filesystem repair can change the settings fingerprint without a database write.
5. Supplement manifests with deterministic, bounded Python source evidence from the index.
   Consider production `.py` files only, excluding tests, documentation, examples, scripts,
   fixtures and benchmarks. Read at most 128 files, 256 KiB per file and 2 MiB total, under the
   caller deadline. Require indexed size and content hash to match current bytes; skip stale,
   growing, unreadable or invalid source. No general code search or Task-hint selection is added.
   Recognize concrete SQLite connection calls, HTTP server construction/subclasses, logging
   configuration/handler subclasses and substantial module-level embedded HTML or paired CSS/JS.
   An import alone is insufficient. Resolve aliases conservatively with lexical shadowing and
   preserve independent web evidence in mobile/web monorepos.
6. Keep the compact 16-skill catalog. Add progressive Dart and Flutter references to the existing
   language/mobile skills. Reliability requirements for background work depend on lost/duplicated
   delivery consequences; a local one-shot script does not require queues or an outbox.
7. Global reconciliation and cleanup both preserve an ADR-0036 source-checkout overlay. Refresh
   that checkout through its isolated `scripts/dev` route. Do not bypass ownership protections
   or silently enable/disable global dogfood to update skills.

## Consequences and verification

The page reports facts available from the registry, resolver and filesystem. It cannot guarantee
model selection or improved output quality. Bounded inference deliberately misses dynamic Python
patterns (including control-flow-dependent imports), files beyond its limits and non-indexed sources; an operator can still Include a facet.

Tests cover exact facet deltas, overlapping selections, escaped custom text, real HTTP Apply,
collision diagnostics, source overlay/no-host distinction, isolated profile defaults, read-only
freshness, source limits/currentness, alias shadowing, monorepo locality and overlay-safe cleanup.
The audit records rendered browser evidence and small forward tasks separately from host
acceptance. Causal usefulness requires a comparative task evaluation, not merely passing delivery
or content tests.
