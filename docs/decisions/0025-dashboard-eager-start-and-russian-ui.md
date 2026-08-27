# ADR-0025: Start the dashboard with the daemon and keep the UI operational in Russian

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Repository architecture baseline
- **Amends:** [ADR-0020](0020-dashboard-drilldown-realtime-design.md), [ADR-0023](0023-linux-operational-diagnostics-and-upgrade-safety.md)

## Context

The dashboard HTTP listener was a second lazy gate on top of daemon autostart (ADR-0009). An operator had to run `harness dashboard` after the daemon was already alive, then copy a capability URL that existed only in daemon memory. The rendered copy still explained what Harness is (control plane, loopback, durable state, no model transcript) instead of showing current work.

The capability path, loopback bind, same-origin mutation rules, and revision-CAS actions remain the security and domain contract. This decision only changes when the listener starts, how the current URL is published to the same OS user, and what the HTML says.

## Decision

Start the daemon-owned loopback dashboard as soon as `harnessd` is serving, before it accepts IPC clients. `get_url` remains idempotent: a later `dashboard_url` request reuses the same listener. If dashboard startup fails, the daemon keeps serving IPC; doctor then warns that the daemon is up but the dashboard listener is not. Doctor still does not start the dashboard.

Publish the current capability URL to `dashboard.url` in the same private runtime directory as the Unix socket, mode `0600`, as a regular current-user file. The file is rewritten for the live URL and removed on clean dashboard shutdown. It is an OS-user-local handle, not a second authorization layer: the same user can already obtain the URL over IPC. The random path token stays in daemon memory for the process lifetime; the file only mirrors the live URL. `harness dashboard` prints that URL and no longer starts a separate listener.

Render the dashboard in Russian. Copy is limited to work process: projects, workspaces, current Task, next step, review actions, search, and timeline. Do not describe the product, the loopback trust model, or why Harness exists. Domain identifiers (`task_id`, revisions, paths) stay literal. Task state, wait reason, visibility, and search match kind are translated only at the HTML boundary.

Persisted Task `title`, checkpoint `summary`/`next_step`, and Knowledge title/body are operator-facing text. MCP server instructions and the `task_start`/`task_checkpoint` tool descriptions tell the model to write those fields in Russian. Tool names, JSON field names, enums, CLI, doctor, and IPC stay English. Harness stores the supplied UTF-8 as-is and does not language-detect or reject English Task text.

## Consequences

- Opening Cursor/Claude, `harness scan`, `harness install`, or any other canonical client that autostarts `harnessd` also brings the dashboard listener up. A reboot still depends on that existing daemon autostart; this decision does not add systemd/launchd.
- `harness dashboard` is discovery, not a start command. The runtime `dashboard.url` file is the stable local handle for the current process.
- A healthy daemon with a down dashboard listener is a warning, not the previous "lazily inactive" success.
- English CLI/doctor/IPC schemas and MCP payload field names are unchanged. Model-facing instructions now require Russian operator-facing Task/Knowledge text; whether a given host actually follows those instructions remains a host-acceptance concern.

## Verification

Automated tests must prove:

- a reachable daemon reports `dashboard_running=true` without a prior `dashboard_url` request;
- the runtime `dashboard.url` file is mode `0600`, matches the IPC URL, and is removed on shutdown;
- repeated `dashboard_url` requests reuse one URL;
- dashboard HTML is `lang="ru"`, uses operational Russian labels, and still escapes persisted Task text;
- MCP `server/discover` instructions and `task_start`/`task_checkpoint` descriptions require Russian operator-facing Task/Knowledge text and stay inside the existing instruction/catalog budgets;
- dashboard startup failure remains bounded so the daemon keeps serving.
