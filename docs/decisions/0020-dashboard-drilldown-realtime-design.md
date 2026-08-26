# ADR-0020: Make dashboard drill-down and realtime refresh a progressive-enhancement layer

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Repository architecture baseline

## Context

The daemon-owned dashboard already exposes a capability-scoped Projects overview and revision-CAS human review actions over the same durable state used by MCP. The next dashboard milestone requires Project/Workspace/Task drill-down, bounded search, and realtime UI updates without introducing a second source of truth, weakening browser isolation, or disclosing raw source text.

The interface also needs a stable visual language rather than accumulating ad-hoc styles as pages are added. Harness is a dense local developer tool, not a marketing surface, so the design should optimize information hierarchy, state visibility, keyboard/accessibility behavior, and consistency before decorative motion.

## Decision

Add capability-scoped Project, Workspace, and Task detail routes under the existing random dashboard path. Workspace detail reuses the existing deterministic indexed-path search domain primitive with a fixed bounded result limit; it exposes only path metadata and match reason, never source text or stored content hashes. Task detail renders bounded recent timeline events, checkpoint summaries/next steps, changed-path metadata, and operator feedback from the existing durable Task history. Human actions continue to call the exact same revision-CAS domain workflow.

Serve dashboard CSS and JavaScript as capability-scoped local assets. The Content Security Policy permits only same-origin style, script, and EventSource connections; inline script/style is not required. The UI uses progressive enhancement: navigation, search, and human-review forms work without JavaScript. JavaScript is limited to realtime freshness behavior.

Realtime uses Server-Sent Events only as a dashboard refresh hint. Every rendered page embeds a SHA-256 fingerprint of its bounded authoritative view model in the capability-scoped EventSource URL. On connection, the server recomputes that view once to close the race between HTML rendering and EventSource setup, then keeps one read-only SQLite connection open and watches `PRAGMA data_version` instead of repeatedly rebuilding the view or running live Git subprocesses. A changed data version emits only a `refresh` marker; the stream never carries Task text, source content, model reasoning, or mutation payloads. The browser reloads after a refresh when no user input is at risk; when feedback/search input is non-empty it shows an explicit update affordance instead of discarding the draft. SSE sessions are bounded in duration, capped per dashboard server, and reconnect through normal EventSource behavior.

The visual system is a dense editorial developer-tool direction: warm neutral surfaces, one restrained coral accent, serif display typography paired with system sans/monospace data, a named 4/8px spacing scale, shallow elevation, textual state pills, visible keyboard focus, responsive layouts, dark-mode tokens, and motion only for hover/live-state feedback. Reduced-motion preferences disable non-essential transitions and animation. Color is never the only carrier of Task state.

## Consequences

- Dashboard navigation and realtime remain local presentation concerns; SQLite/domain state is still authoritative.
- The existing stdlib HTTP server remains sufficient; no new runtime web-framework dependency is required.
- Search behavior cannot drift from CLI/MCP mechanical path search because the dashboard calls the same domain primitive.
- A malicious site still needs the unguessable capability path, and mutation POSTs additionally retain exact Host/Origin plus revision-CAS validation.
- SSE steady-state polling is an O(1) SQLite `data_version` read on one persistent read connection; it does not create a Git subprocess storm. Durable Task/operator/index commits trigger refresh hints. A Git-only live-status change with no Harness database commit is picked up by the next normal page render rather than being promised as instantaneous SSE state.
- The dashboard now has a deliberate reusable design vocabulary instead of per-page one-off CSS.

## Verification

Automated coverage must prove:

- unscoped Project/Workspace/Task/assets/events routes remain inaccessible;
- Project/Workspace/Task navigation renders durable state and escapes all persisted text;
- Workspace search returns only bounded indexed-path metadata;
- external Task/index changes produce an SSE refresh hint without streaming the changed content;
- CSP permits only same-origin assets/EventSource and no inline script/style;
- existing same-origin CAS actions, stale-revision conflicts, response hardening, daemon lifecycle, and shutdown behavior remain unchanged;
- repo-wide formatting, lint, typing, tests, and wheel smoke remain green.
