# ADR-0027: Bind the dashboard to a fixed loopback port and persist the capability token

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0025](0025-dashboard-eager-start-and-russian-ui.md)
- **Amended by:** [ADR-0040](0040-dashboard-root-url-and-project-index.md)

## Context

ADR-0025 starts the dashboard with `harnessd` and publishes the live URL to a runtime `dashboard.url` file. The HTTP listener still bound `127.0.0.1` on an ephemeral port, and the capability path token lived only in daemon memory. Every daemon start produced a new URL, so an operator could not bookmark the dashboard.

The capability path, loopback bind, unscoped-root 404, same-origin mutation rules, and revision-CAS actions remain the security contract. This decision only makes the operator URL stable across daemon restarts.

## Decision

The canonical per-user daemon binds the dashboard to `127.0.0.1:17373`. An isolated checkout (`HARNESS_DEV_ROOT`) binds `127.0.0.1:17374` so it can coexist with a separately installed user-global daemon. Explicit `--socket` overrides, tests, and other non-canonical sockets keep an ephemeral port.

Those ports are User Ports with no IANA TCP assignment as of 2026-08-17, sit below the Linux ephemeral range (`ip_local_port_range`, commonly `32768-60999`), are absent from `/etc/services`, and are not conventional local-dev listeners (`3000`, `5173`, `8000`, `8080`, `8888`, `11434`, …). They are loopback-only; Harness is not reserving a LAN service.

Persist the capability path token next to the selected database as `dashboard.token`, mode `0600`. The first successful start creates it; later starts reuse it. It is an OS-user-local secret, not a second authorization layer: the same user can already obtain the URL over IPC or `dashboard.url`. Unscoped `/` remains 404. `harness dashboard` still prints the full capability URL. The runtime `dashboard.url` file remains the current-process handle and is still removed on clean dashboard shutdown.

If the preferred port cannot be bound, dashboard startup fails bounded and the daemon keeps serving IPC. Doctor warns and names the expected loopback port. This decision does not add systemd/launchd.

## Consequences

- The bookmarkable URL is `http://127.0.0.1:17373/<token>/` for the user-global daemon and `http://127.0.0.1:17374/<token>/` for an isolated checkout. The token is stable for that database until the token file is removed.
- Isolated development and a user-global install can both keep a dashboard listener.
- Tests and recovery daemons with explicit sockets do not contend for the well-known ports.
- A foreign process occupying `17373`/`17374` on loopback makes the dashboard unavailable until that port is free; IPC continues.

## Verification

Automated tests must prove:

- canonical sockets select `17373`, isolated canonical sockets select `17374`, and override sockets select ephemeral `0`;
- a preferred-port listener plus durable token reuse the same full URL across manager/daemon restarts;
- a busy preferred port raises a bounded dashboard error without leaving a running listener;
- the daemon passes the selected listen port into the dashboard manager;
- unscoped `/` remains 404 and `dashboard.url` is still removed on shutdown.
