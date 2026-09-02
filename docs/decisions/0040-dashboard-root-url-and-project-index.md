# ADR-0040: Serve one dashboard at the loopback root and index Projects, not copies

- **Status:** Accepted
- **Date:** 2026-09-01
- **Amended:** 2026-09-02
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0019](0019-dashboard-human-review-loop.md),
  [ADR-0020](0020-dashboard-drilldown-realtime-design.md),
  [ADR-0025](0025-dashboard-eager-start-and-russian-ui.md),
  [ADR-0027](0027-dashboard-fixed-loopback-port.md),
  [ADR-0031](0031-task-operator-tracking-reopen-and-search.md)

## Context

ADR-0027 made the dashboard port stable (`127.0.0.1:17373`, isolated `17374`) but kept the
operator URL behind an unguessable path token. `http://127.0.0.1:17373/` therefore 404s.
Operators bookmark the advertised port, not a capability path. The token is already an
OS-user-local secret shared with Codex's loopback bearer (ADR-0037); it is not a second
authorization layer against the same OS user.

The home UI also presents each Project as nested "рабочие копии" and relabels a checkout whose
folder name matches the Project as "Основная копия". That is not a product concept. There is one
daemon-owned dashboard and a list of Projects. A Workspace remains a physical checkout in the
registry; it is not a second dashboard or a "copy" of the Project.

GET already requires the exact loopback `Host`. Mutations already require that Host plus
same-origin `Origin` or `Sec-Fetch-Site: same-origin`. Those checks, not the path token, are the
browser isolation contract.

## Decision

1. The operator dashboard URL is the loopback root: `http://127.0.0.1:17373/` for the canonical
   per-user daemon and `http://127.0.0.1:17374/` for an isolated checkout. Explicit `--socket`
   overrides keep an ephemeral port with path `/`. `harness dashboard` and the runtime
   `dashboard.url` file publish that root URL.
2. HTML, CSS, JavaScript, SSE, and mutation POSTs are served from that root
   (`/projects/{id}/`, `/workspaces/{id}/`, `/tasks/{id}/`, `/assets/`, `/events`). They are no
   longer capability-path-scoped. Loopback bind, exact `Host`, same-origin mutation rules, and
   revision-CAS actions remain.
3. Persist `dashboard.token` next to the selected database as today. It is the Codex Streamable
   HTTP bearer, not a dashboard path secret. Dashboard startup still ensures the file exists so
   Codex and the dashboard share one capability.
4. A request whose first path segment equals the stored token is rewritten to the equivalent root
   path so older capability bookmarks keep working. Any other first segment is not a dashboard
   page and stays 404.
5. Sidebar navigation is one link per Project and opens that Project's Workspace folder
   (`/workspaces/{workspace_id}/`), not `/projects/{project_id}/`. Breadcrumbs use the same
   Workspace URL. The home page is daemon-wide Task search plus a bounded recent-Task list that
   pins non-terminal Tasks (`working` and `waiting`, including operator review) ahead of
   completed or cancelled Tasks, then recency within that grouping. Do not list Task titles in
   the sidebar. Do not label a Workspace as a copy, a primary copy, or a second dashboard.
   Workspace detail remains for bounded search, current Task, recent Tasks, visibility, explicit
   relocation, and the Project deletion disclosure; operator copy calls it a folder and shows
   the absolute path. `/projects/{id}/` remains the mutation target and folder inventory.
   Sidebar Project links stay on `/workspaces/{workspace_id}/`.

## Consequences

- The bookmarkable URL matches the advertised loopback port.
- Removing the path token does not weaken Host/origin mutation checks. A foreign website still
  cannot mutate the dashboard, and a DNS-rebound hostname still fails the exact `Host` check.
- Codex MCP authentication is unchanged.
- Registry Workspace identity is unchanged. Extra Git worktrees stay extra folders of one Project.

## Verification

Automated tests must prove:

- the published URL path is `/` on the selected loopback port, and `GET /` renders daemon-wide
  Task search and a bounded Task list that pins live Tasks ahead of recency;
- sidebar and breadcrumb Project links use `/workspaces/{workspace_id}/` and do not list Task
  titles;
- `dashboard.url` / IPC `dashboard_url` match that root URL and the URL file is still removed on
  shutdown;
- a stored-token prefix still reaches the same pages;
- an unknown path, a wrong `Host`, and a non-token first segment remain 404;
- mutation POSTs still require exact loopback Host and same-origin proof;
- overview HTML does not call a Workspace a copy or primary copy;
- Workspace detail renders the Project deletion disclosure posting to `/projects/{id}/`;
- isolated canonical sockets still select `17374`.
