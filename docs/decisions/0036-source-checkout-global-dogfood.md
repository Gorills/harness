# ADR-0036: Source-checkout global dogfood is an explicit index-only route

- **Status:** Accepted
- **Date:** 2026-08-31
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md), [ADR-0030](0030-codex-project-scoped-workspace-mcp.md)

## Context

The Harness source checkout normally launches checkout code and private XDG state through
`scripts/dev`. That isolation is required for deterministic development and prevents an
in-progress schema, daemon, or adapter from mutating the user's canonical Harness state.

Isolation also leaves a dogfood gap. An agent working in this repository cannot exercise the
tool-installed executable, canonical daemon, durable Task/Knowledge history, and real MCP route
against the same dirty worktree. Synthetic machine acceptance uses temporary state and another
Workspace, while live installation deliberately preserves the checkout overlay. Running checkout
code directly against the canonical socket would close the gap by mixing runtime identities and is
not acceptable.

## Decision

The source checkout exposes one opt-in router, `scripts/dogfood`.

1. The default route remains `scripts/dev harness ...` with checkout-local state. A mode marker is
   absent by default.
2. `scripts/dogfood enable-global` resolves Harness only through the uv tool binary directory,
   rejects an executable inside the checkout, removes checkout virtual-environment and XDG
   overlays, and runs the tool-installed CLI.
3. Before recording the opt-in, the installed CLI must successfully run
   `harness scan --global-dogfood <checkout>`. Failure leaves the prior route unchanged. The marker
   is then written atomically as an exact versioned value under ignored `.harness/`; symlinked,
   non-regular, or unknown marker state fails closed.
4. `--global-dogfood` is valid only for a detected Harness source overlay, outside
   `HARNESS_DEV_ROOT`, and from an interpreter outside the checkout. It registers and indexes the
   Normal Workspace in the selected daemon, then returns before host configuration or skill
   projection. Hidden activation is refused because this route does not reconcile host policy.
5. In global mode the router sets the canonical checkout root explicitly and launches the
   tool-installed MCP/CLI without `HARNESS_HOST_PROFILE`. Search, context, Task, Knowledge,
   status, dashboard, and rescans therefore use canonical state, while production adapter
   lifecycle remains outside the checkout route.
6. The tracked Codex, Cursor, and Claude MCP configurations launch the router. They continue to
   represent one source-checkout server per host. Codex keeps only `harness-dev`; a second alias is
   forbidden because duplicate catalogs make tool selection and diagnostics ambiguous.
7. `scripts/dogfood disable-global` removes only the exact local mode marker. It does not delete
   canonical Project, Workspace, index, Task, or Knowledge state. Host restart/reload is required
   after either transition.
8. `install` and `uninstall` are never forwarded through the router. Package replacement and live
   host activation retain their separate explicit-authorization targets. Global dogfood also
   refuses `visibility` changes; disable the route before an operator changes source-checkout
   visibility.
9. Production install/uninstall reconciliation and the canonical daemon watcher preserve a
   detected source-checkout overlay without projecting canonical skills into it. The isolated
   daemon identified by `HARNESS_DEV_ROOT` keeps the existing development-skill workflow.

## Consequences

- Development tests keep a reproducible private daemon and database by default.
- A developer can use the installed Harness as the persistent control plane for work on Harness
  itself without running checkout code against the canonical daemon.
- Global dogfood deliberately does not test current uninstalled code. Exact-current-wheel machine
  acceptance and isolated tests remain separate evidence layers.
- Global and isolated stores may both contain the same Git Workspace identity. Their process,
  database, socket, and runtime identities remain separate.
- Disabling the route is reversible and non-destructive, but the host must restart to replace an
  already running MCP subprocess.

## Verification

Automated tests must prove:

- absent marker routes to `scripts/dev`;
- enable scans successfully before writing the marker, and a failed scan leaves it absent;
- global execution removes checkout XDG, virtualenv, skill-registry, and source-import overlays;
- the resolved global executable is outside the checkout;
- invalid and symlinked markers fail closed;
- `--global-dogfood` refuses checkout interpreters and non-overlay repositories;
- Hidden source-checkout registration and routed visibility changes are refused;
- a successful dogfood scan registers/indexes but never reconciles host adapters or skills;
- tracked host configs expose one router-backed source-checkout server and production lifecycle
  preserves the overlay;
- global doctor recognizes the tracked Codex and Cursor source-checkout overlays as intentional
  and does not require generated skill projections that the index-only route deliberately skips;
- later production host lifecycle and watcher passes do not project skills into the checkout,
  while the isolated development daemon can still reconcile its development skill profiles.
