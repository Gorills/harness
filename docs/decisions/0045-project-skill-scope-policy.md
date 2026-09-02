# ADR-0045: Project skill scope is explicit operator policy

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0044](0044-project-skill-projection-has-no-count-cap.md)

## Context

Detected repository stack and operator development scope are different facts. A Project may contain
both backend and frontend code while the Harness user is responsible only for the backend. Projecting
frontend-specific Skills into that Project adds irrelevant native-host choices even though stack
detection is mechanically correct.

Inferring team ownership from files, Tasks, branches, or recent edits would add unreliable automation.
Persisting exclusions only by current Skill ID would also age badly: a newly added frontend Skill
would bypass an older Project preference until the user configured it again.

## Decision

1. Harness stores a durable Project-level exclusion policy for stable development surfaces represented
   by existing stack facets: backend, web frontend, mobile, database, Godot, containers,
   observability, CI/release, and deployment operations.
2. Dashboard presents each managed surface with two states: `Auto` and `Excluded`. `Auto` keeps normal
   stack-driven behavior. `Excluded` prevents Skills for that detected surface from being projected.
3. `software-project` is not user-disableable. The shared quality baseline remains available even when
   one specialized development surface is excluded.
4. Skill resolution applies Project surface exclusions after Workspace stack detection. A matching
   managed surface cannot re-enter only because the same Skill also matches a dependency, manifest,
   or language. A deliberate explicit Skill include remains a narrower override.
5. Built-in or future pack Skills that belong to a managed surface MUST declare the corresponding
   facet. Therefore an existing Project exclusion automatically applies when the pack is updated or a
   new matching Skill is added.
6. A Dashboard policy change is persisted once for the Project, then current host Skill projections are
   reconciled for all registered Workspaces of that Project without rescanning source. Failed direct
   reconciliation uses the existing Workspace invalidation path as retry; no new watcher or workflow
   subsystem is introduced.
7. Task metadata and Task lifecycle do not select or mutate Project skill scope. Harness does not infer
   developer ownership from current Tasks.

## Consequences

- A multi-stack repository can remain fully indexed while its native Skill surface reflects the part
  the operator actually develops.
- `Frontend -> Excluded` remains effective when a future frontend Skill is added to the canonical pack,
  provided that Skill uses the established `web-frontend` facet.
- Restoring a surface to `Auto` immediately returns to detected-stack behavior; no per-Skill reset is
  required.
- Shared core Skills may still contain progressive cross-stack references, but specialized excluded
  surface Skills are not projected. Dynamic rewriting of core Skill files per Project is intentionally
  out of scope.
- No role model, team ownership database, task-time router, or second Skill composition DSL is added.

## Verification

- Schema tests cover the bounded managed-facet set, migration to the new schema, and Project cascade
  deletion.
- Resolver tests prove an excluded surface cannot re-enter through another match dimension.
- Reconciliation tests prove exclusion removes existing projected Skills, survives addition of a new
  matching Skill, and restoring `Auto` projects them again.
- Dashboard tests prove the human control is rendered, persists Project policy, avoids unnecessary
  source scans, reconciles all Project Workspaces when host profiles are active, and retries only
  failed reconciliations.
