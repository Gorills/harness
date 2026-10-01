# ADR-0074: Multi-project workbench and inline decisions

- **Status:** Accepted for implementation
- **Date:** 2026-10-01
- **Amends:** ADR-0020, ADR-0040, ADR-0070

## Context

The Project hub repeats large cards, pushes search and Task history below the fold, and sends
Project navigation through an Overview that repeats one current Task. Lists require navigation
before acceptance, feedback or state changes. This makes many registered Projects difficult to
scan. The operator requested a complete dashboard redesign around fast decisions.

## Decision

1. Home becomes a workbench: compact linked counts, Task search, review queue, Project directory
   and Task history. Reviews sort first, other live Tasks second and terminal Tasks last; each
   group keeps deterministic recency/ID ordering. The bounded 24-Task page is split between queue
   and history without duplicating forms. Queue counts distinguish displayed and total reviews;
   older pages remain reachable, and an empty later queue offers a return to its beginning.
2. `/projects/{id}/` becomes the Project Task archive across all registered folders. It supports
   the same bounded `q` and `page` query grammar as Workspace history. The Project SSE snapshot
   includes search/page and the corresponding authoritative Task records. Settings rejects these
   query parameters. Existing URLs, legacy capability bookmarks and mutation endpoints remain.
3. Shared navigation has Tasks, Notes/access and Settings. Task links lead to the aggregate list;
   folder links retain exact Workspace context. Notes retain validated source identities and
   the direct return to the source Task. Private frames receive no new data or scripts.
4. Task rows expose one-click Accept for `waiting(operator_review)`, a feedback disclosure and a
   status disclosure. Task detail exposes its status form without a disclosure. Every form posts
   explicit Task/Workspace identity and the displayed revision through existing domain services.
   No optimistic terminal state, automatic stale retry, new write authority or schema migration.
5. Client-side Project search, sidebar search and Project filters are optional enhancements over
   the complete server-rendered directory. The controls are hidden before JS binds them and on
   script-free vault pages. Filters survive an HTML replacement in memory, do not count as dirty
   drafts and add no browser persistence. Native Task search and all mutations work without JS.
6. Show distinct review, operator-input and external-wait labels. Show current-revision feedback
   as the next step after a return to work; show checkpoint next steps only for matching current
   state/reason, and suppress them for terminal Tasks. Read bounded summary previews without
   loading checkpoint changed-path inventories. Localize visible Task-row timestamps to the browser timezone with a
   UTC fallback. Waiting-reason controls appear when selecting waiting; the native form retains
   them when JS is unavailable. Dense wide lists recompose into labelled rows on narrow screens.

## Verification

Exercise Project archive membership across multiple folders and foreign Projects, search scope,
bounded pagination, page-aware SSE fingerprints, inline form identity/revision, successful
POST/303 preserving the originating Project URL, and stale POST rejection. Retain the existing
HTTP/origin/CAS/disclosure, history, draft recovery and real JavaScript regression suites.
Browser acceptance uses a disposable database with 12 Projects, wide and narrow viewports,
inline acceptance, feedback/state selection, filtering, settings and exact Task return from notes.
Screenshots are presentation evidence; they do not establish full accessibility conformance.
