# ADR-0076: Dashboard for daily work across many Projects

Date: 2026-10-01

Status: Accepted implementation direction requested by the operator

## Context

The operator requested a fresh dashboard, discarding the previous visual design and page
composition. Daily work requires finding a Project and acting on a ready result without passing
through an Overview or scrolling past a mixed archive. A long review queue must not consume the
bounded active-task list or make its pagination depend on unrelated archived Tasks.

## Decision

Replace the dashboard CSS and major page compositions with a light work surface and persistent
dark navigation. The home page pairs a searchable Project directory with an independently
scrollable inbox. Global Tasks and review have their own routes through a validated `scope` query
field; the Project and Workspace routes open active Tasks by default.

- Home defaults to `scope=projects`; its bounded inbox selects review Tasks only.
- Task views accept `active`, `review`, `all`, and `archive`. Active includes only working;
  review means waiting for operator review; archive means completed or cancelled. Tasks waiting
  for operator input or an external dependency remain available under all and search.
- Sidebar primary links and Project search stay fixed; only the quick Project list scrolls,
  without a visible scrollbar. Keyboard focus and wheel scrolling still reach every Project.
- Membership, selected count, 24-row pagination, and SSE fingerprints use the same scope.
  Total Task counts remain available for filter counts and do not control the selected page.
- Search spans active Tasks and archive within the existing global or Project boundary. It
  renders at most 24 actionable Task rows, with an explicit refinement hint at the limit.
  Search replaces the ordinary list, keeping each mutation form identity unique.
- Ready rows expose acceptance and feedback. A disclosure showing the current status opens
  the existing explicit status form. Project directory rows link directly to the folder;
  infrequent settings and notes use an inline disclosure that cannot be clipped by list scrolling.
- Task detail separates report/checks/history from the operator action column. On narrow screens,
  primary navigation stays visible, inbox precedes Projects, and actions precede history.
- Existing POST schemas, same-origin validation, domain services, revision CAS, failed-form
  recovery, and private-vault isolation remain authoritative. This changes no database schema.
- JavaScript enhances optional Project filters and freshness. In-place refresh retains list
  scroll, filter state, compatible drafts and focus, using fresh server revision fields.

This supersedes the visual composition and mixed-list defaults in ADR-0074. Its direct Project
Task navigation and domain-backed inline decisions remain.

## Verification

HTTP/SSE tests cover selected-scope snapshots, strict query validation, multiple review pages,
active Tasks after a full review page, archive membership, and actionable search with no duplicated
forms. Existing action, ownership, evidence, same-origin and draft-recovery tests remain in place.
Browser verification uses a disposable database with multiple Projects; real registered Tasks are
not accepted or edited for testing. Wide and narrow screenshots accompany the delivery.
