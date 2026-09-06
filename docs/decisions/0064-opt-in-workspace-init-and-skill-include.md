# ADR-0064: Opt-in Workspace init, optional Git, and included skill surfaces

- **Status:** Accepted
- **Date:** 2026-09-06
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0002](0002-host-integration-and-workspace-resolution.md),
  [ADR-0032](0032-continuous-project-skill-reconciliation.md),
  [ADR-0034](0034-dashboard-project-removal-and-workspace-relocation.md),
  [ADR-0042](0042-project-stack-skill-selection.md),
  [ADR-0045](0045-project-skill-scope-policy.md),
  [ADR-0063](0063-registered-workspace-baseline-and-godot-evidence.md)
- **Amends:** ADR-0045 (Adds `Included` alongside Auto/Excluded), ADR-0063 Decision 4
  (non-Git directories are registerable via `harness init`), and the `harness scan`
  registration contract in ARCHITECTURE §11/§16. Scan of already-registered Workspaces
  and watcher skill refresh after index changes remain.
- **Does not rewrite:** [docs/specification.md](../specification.md). The original scan-as-register
  workflow is preserved there; this ADR is the implementation amendment.

## Context

Operators often start a local trial folder with no Git. Git may appear later or never.
`harness scan` previously registered any inspectable Git worktree it was pointed at, so adding
a project was coupled to Git discovery. Empty Git worktrees also could not show specialized
Skills (Godot, backend, …) until files existed; Dashboard policy was only Auto/Excluded.

The operator-selected UX is: bind one folder with `harness init`, keep the quality baseline on,
choose specialized surfaces in the Dashboard, and let the watcher refresh Skills when the stack
of an already-registered Workspace changes. Continuous discovery that registers new projects is
not wanted.

## Decision

1. `harness init [PATH]` is the only ordinary registration command. It binds the canonical
   directory (Git toplevel when PATH is inside a Git worktree, otherwise that directory).
   Init is idempotent for an already-registered root. A new Git worktree whose common directory
   already belongs to one Project attaches to that Project; independent clones still create a
   new Project. Nested or overlapping registered roots fail closed.
2. `harness scan [PATH]` reconciles index, host MCP, and Skills for an already-registered
   Workspace. It does not create a Project. Unregistered PATH fails with an actionable
   `harness init` instruction. `scan --global-dogfood` remains the explicit ADR-0036 exception
   that may register the Harness source checkout.
3. A Workspace without Git is first-class for Normal visibility, index, search, Tasks, and
   Skills. Persistence keeps `git_common_dir` NOT NULL and stores the canonical root as a
   sentinel (`git_common_dir == workspace_root`). Hidden remains Git-only and fails closed
   for filesystem Workspaces. `project_status` Git fields stay the existing nullable
   branch/HEAD shape (same as an unborn repository); Harness does not invent a fake branch.
4. When Git appears later in the same bound root, Harness attaches the real Git common
   directory in place and keeps `workspace_id` / `project_id`. Losing Git after attachment
   fails closed. Relocating a Workspace may target a Git or filesystem directory.
5. Every registered Workspace, Git or filesystem, including an empty index, is
   `software-project` evidence for the six core built-ins (extends ADR-0063 beyond Git).
6. Dashboard skill scope gains `Included` for managed surfaces. `Included` projects that
   surface even without indexed evidence. `Auto` stays stack-driven. `Excluded` still
   suppresses the surface. `software-project` remains non-disableable. A facet cannot be
   both included and excluded. Watcher/scan reconciliation after index changes continues
   to add specialized Skills when evidence appears on an Auto surface.
7. Filesystem indexing walks the bound tree with the same default directory/file excludes
   as Git `ls-files` extras, plus `.harnessignore` as bounded glob lines. It does not follow
   directory symlinks. Watcher Git control sampling is skipped until Git is attached.

## Consequences

- Binding a project is an explicit operator action in the folder they care about.
- Trial folders without Git get search, Tasks, and the quality baseline immediately.
- Specialized Skills can be forced from the Dashboard before files exist, and still appear
  automatically on Auto when the watcher sees new stack evidence.
- Host adapters write project MCP in a filesystem Workspace without requiring Git; `info/exclude`
  is applied on the next reconcile after Git attaches.
- Scan is no longer a discovery registrar. Existing registered Workspaces keep converging.
- Hidden, `info/exclude`, and shared worktree visibility stay Git invariants.

## Verification

Automated tests must prove:

- `init` registers a directory without Git, projects the six cores, and is idempotent;
- `scan` of an unregistered path does not create a Project;
- adding `project.godot` (or Dashboard Include of `godot-project`) changes the resolved pack
  after scan/reconcile;
- `git init` in a filesystem Workspace attaches Git identity without minting a new
  `workspace_id`;
- Hidden is refused for a filesystem Workspace;
- overlapping init roots fail closed;
- schema v21 persists included facets and cannot store include+exclude for one facet.
