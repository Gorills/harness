# ADR-0065: Make dashboard evidence, history and failed actions reviewable

- **Status:** Accepted
- **Date:** 2026-09-11
- **Amends:** [ADR-0019](0019-dashboard-human-review-loop.md),
  [ADR-0020](0020-dashboard-drilldown-realtime-design.md),
  [ADR-0040](0040-dashboard-root-url-and-project-index.md),
  [ADR-0043](0043-dashboard-in-place-html-refresh.md)

## Context

The utility audit reproduced missing checkpoint verification, counters based on one selected Task
per Workspace, silently truncated history, and empty error responses after operator actions.
The UI could offer acceptance while hiding a reported failure, show no review Tasks when several
existed, and lose a comment after a revision conflict. Explicit HTML refresh also discarded drafts.
These defects prevent reliable daily use before any broader visual redesign.

## Decision

Keep the existing daemon-owned state and server-rendered UI. No schema, host configuration, MCP
surface, or Task state-machine change is needed.

### Verification belongs to its checkpoint

The Task page shows the latest checkpoint's verification before the timeline: each check's name,
`passed` / `failed` / `not_run` status, evidence, and source. Agent-reported results are labeled as
such, not described as independently observed. The report revision and time are visible; if the
Task has advanced since that report, the UI says so without claiming that a comment necessarily
invalidated the code. A latest checkpoint without checks says that none were supplied. It does
not silently inherit an older checkpoint's passing result.

Historical checks stay attached to their checkpoint in the timeline, including on older pages.
Load verification for the visible checkpoint IDs and the latest checkpoint within the same read
transaction. Escape all supplied text, preserve evidence line breaks, and wrap long names/evidence
on compact screens. Human Accept remains an explicit decision under the existing state/CAS rules;
verification display is not a new automatic acceptance gate.

### Counts and history refer to all durable Tasks

Overview metrics aggregate all `working` and `waiting` Tasks. Review counts include all
`waiting(operator_review)` Tasks, independent of the single Task selected for Workspace attention.

Home and Workspace histories retain the live-first recency order, with 24 Tasks per page. The Task
timeline shows 60 events per page. `page` is a singular positive bounded integer; an otherwise valid
page beyond the end resolves to the last page. Real previous/next links work without JavaScript,
preserve the search query where applicable, and point at the relevant history section. Existing
search byte limits and rejection of duplicate or unknown query fields remain.

The latest Task summary and recorded branch remain the latest even when an older timeline page
is open. Timeline checkpoints are loaded for that page's events, rather than pairing old events
with the newest checkpoints. The page selection is part of the SSE view and fingerprint. Pages
reflect a fresh consistent read, not a permanently frozen history snapshot; concurrent updates can
change recency ordering between requests.
The limits bound the rendered records, not total SQL work: the existing live-first Task ordering
can sort candidates, and deep pages use OFFSET. This change makes history reachable; it does not
claim constant-cost pagination for arbitrarily large Task collections.

### Failed actions preserve editable input

Successful POSTs retain the normal domain mutation and redirect path. A valid same-origin failed
action receives a Russian recovery page instead of an empty 400/409 response. It explains whether
the request was invalid or the Task changed and retains the supported editable draft fields.
Where the exact target and action remain available, reconstruct controls from fresh authoritative
state and restore only editable values. Task recovery also shows the latest reported verification
before presenting acceptance controls. Hidden identities and revision tokens come from that state.
The operator reviews and submits again; no failure is automatically retried.

When a compatible form is no longer available, keep an escaped read-only copy of the draft and a
return link. Unknown fields, malformed envelopes and cross-origin requests never acquire an
editable mutation form merely because a request supplied plausible IDs. Exact Host/Origin checks,
body/field limits, ownership checks, CAS, terminal-state rules and hardened response headers remain.
Do not put drafts in query strings or browser persistent storage.

### Refresh preserves ongoing interaction

Dirty inputs, including checkbox/radio changes and server-restored drafts, prevent automatic SSE
apply. Explicit refresh may fetch fresh HTML but preserves editable values, selection, focus,
open disclosures and sidebar scroll for compatible controls. Match forms by action and stable
target identity; do not restore stale hidden revision tokens or copy a draft into another Task's
form. If an edited control disappeared or no longer matches, leave the old view and draft available
with an explanation instead of silently losing the input. Recheck edits made while fetch was in
flight before applying the result. POST remains the only mutation authority.

This supersedes ADR-0043's explicit-refresh draft-discard behavior. It adds no client-side durable
state and does not turn SSE into a Task-content channel.

## Verification

- Database-backed tests cover failed/not-run/passed reports, escaping, source/age labels, empty
  latest verification, old-page evidence, and isolation from another Task.
- Counts and paginated HTTP navigation cover multiple review Tasks in one Workspace, multiple
  Projects, more than one page of Tasks/events, query validation, and page-aware SSE snapshots.
- Same-origin HTTP tests cover revision conflicts, invalid editable values, fresh retry tokens,
  unavailable forms, retained drafts, and non-mutation on failure.
- Tests execute the shipped JavaScript with a small DOM double in Node, covering late edits,
  checkbox/radio state, forced refresh, matching targets and interaction state. They explicitly
  skip when Node is unavailable; that is not real-browser acceptance.
- Real-browser fixture checks and the repository quality gate are reported separately in the Task
  checkpoint. A local headless fixture does not establish proprietary Cursor/Codex model behavior.
