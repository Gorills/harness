# ADR-0068: Apply dashboard action responses without competing refreshes

- **Status:** Accepted
- **Date:** 2026-09-20
- **Amends:** [ADR-0043](0043-dashboard-in-place-html-refresh.md),
  [ADR-0065](0065-dashboard-evidence-history-and-draft-recovery.md)

## Context

Dashboard actions still used native form navigation even though realtime updates already replaced
HTML in place. A successful action could trigger both its POST/303 navigation and an SSE-driven
GET of the same page. Operators had no immediate saving feedback, and competing reads repeated
page rendering and live Git work. Workspace and Project detail readers also repeated identity
inspection inside an existing request-local Workspace applicability proof.

## Decision

Enhance same-origin POST forms with a single fetch of the existing form action. Show saving state
immediately and prevent concurrent submissions. Keep the server's bounded form validation,
same-origin checks, revision CAS, domain mutation and 303 redirect unchanged. Apply the returned
authoritative HTML through the existing layout replacement and interaction-preservation path.
Without JavaScript, forms retain their existing POST/redirect navigation.

Coordinate mutation responses with realtime and manual refreshes: a pre-mutation GET must not
replace the mutation result, and hints received during submission must not launch a competing
page refresh. Reconnect using the returned page's snapshot so SSE continues to detect subsequent
changes. SSE remains a content-free freshness hint; the browser does not infer successful Task
state before receiving the server response.

Clear only successfully submitted editable values that the operator has not changed since
submission. Preserve other forms' drafts and edits made while the request was running. Failed
actions retain authoritative recovery controls and fresh revision tokens. An uncertain transport,
parse or apply failure retains input and requests fresh HTML once, without replaying the POST;
the operator is prompted to check whether it was saved. Deleted targets and redirects keep the browser
URL consistent with the rendered page.

For Workspace and Project detail, use the existing applicability proof around live Git status
instead of nesting another pair of runtime identity probes. Keep the normal-mode status read for
the existing dirty-path count and the final applicability validation, including all-mode status,
registry identity and consulted content checks. This is request-local reuse, with no persistent
cache, migration, alternate mutation authority or relaxation of branch visibility.

## Verification

- Execute the shipped JavaScript for successful mutations, immediate saving state, double-submit
  suppression, SSE/manual refresh races, late edits, conflicts and transport failures.
- Retain real HTTP tests for POST/303 behavior, Origin/Host checks, CAS and draft recovery.
- Count identity inspections on Workspace/Project detail and prove that movement during the read
  still fails closed. Performance measurements use synthetic isolated repositories; timing is
  reported separately from deterministic correctness assertions.
- Run dashboard regression tests and the repository quality gate; report browser acceptance
  separately from the Node DOM-double checks.
