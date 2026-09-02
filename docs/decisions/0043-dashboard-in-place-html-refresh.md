# ADR-0043: Dashboard realtime replaces HTML in place, not via full navigation

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0020](0020-dashboard-drilldown-realtime-design.md)

## Context

ADR-0020 made Server-Sent Events a freshness hint and had the browser reload the current page when
no operator input was at risk. That is enough for a read-mostly review surface. It is not enough
once the dashboard is a place where operators keep working: a full navigation drops scroll, focus,
open disclosures, and any in-progress interaction that is not yet a dirty form field. The dashboard
will keep gaining operator workflows, so page-level reload cannot remain the realtime apply path.

SSE must stay a hint. Streaming Task text or source through the event channel would create a second
payload path beside the HTML renderer and weaken the existing disclosure boundary.

## Decision

Keep the existing SSE contract: `refresh` / `changed` only, plus the page fingerprint in the
EventSource URL. When a hint arrives and no editable field is dirty, the dashboard JavaScript
re-fetches the current same-origin URL (`pathname` + query) as HTML, parses it with `DOMParser`,
and replaces the rendered `.app-layout` in place. It updates `document.title` and `data-events-url`,
then reconnects EventSource only when the snapshot URL changed. The explicit «Обновить» control
uses the same apply path. Automatic apply still yields to dirty textarea, non-hidden input, and
select values. JavaScript must not assign `.innerHTML` and must not call `location.reload`.

Navigation, search, and mutation forms remain server-rendered progressive enhancement. Fetch is
presentation-only; POSTs stay the mutation authority.

## Consequences

- Realtime updates no longer destroy the browsing context of the current dashboard page.
- A later dashboard feature that lives inside `.app-layout` is picked up by the next HTML fetch
  without a client-side view model.
- EventSource must adopt the new snapshot URL after a successful apply so a later reconnect does
  not immediately re-emit `refresh` against a stale fingerprint.
- Dirty operator drafts still require an explicit refresh, which replaces the layout and discards
  those drafts, matching the previous explicit-reload affordance.

## Verification

Automated tests must prove:

- dashboard JavaScript contains `DOMParser` / `replaceWith` and does not contain `location.reload`
  or `.innerHTML`;
- unsaved input still gates automatic apply;
- SSE still emits only the `refresh` / `changed` hint after an external Task change;
- CSP still allows same-origin `connect-src` for both EventSource and the HTML fetch.
