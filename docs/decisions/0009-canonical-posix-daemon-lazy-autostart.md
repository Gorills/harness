# ADR-0009: Canonical POSIX clients lazily autostart the per-user daemon

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

Harness is installed once per user and the architecture requires one long-lived `harnessd` owner for durable business state. ADR-0007 established canonical per-user database/socket paths and ADR-0008 established safe singleton ownership and crash-stale socket recovery.

Before this decision, `harness status` and `harness scan` still required the user to start `harnessd serve` manually. A clean installation with no runtime directory therefore failed before any useful command could reach the daemon, which is not install-ready behavior.

A full systemd/launchd service manager would add platform-specific installation and lifecycle surface that is not required to remove this immediate first-run burden.

## Decision

On supported POSIX systems, client commands that use the canonical socket lazily ensure the canonical daemon is reachable before issuing their Workspace request.

The bounded behavior is:

1. resolve the canonical runtime paths;
2. if the final runtime directory already exists, validate that it is a real current-user-only directory before trusting any socket inside it;
3. probe the canonical daemon with the bounded internal `status` request;
4. if the transport is unavailable, launch a detached child from the same installed Python package with `python -m harness.daemon_process`;
5. wait for at most three seconds for the private runtime directory and daemon status probe to become ready;
6. continue with the original Workspace request only after readiness is proven.

If the canonical runtime directory does not yet exist, that absence is treated as a first-run state: the client launches the daemon and waits for the daemon to create and secure the directory. An existing insecure/symlinked/untrusted runtime directory still fails closed and is never repaired or replaced by the client.

The child redirects stdin/stdout/stderr to the null device, closes inherited file descriptors, and starts a new POSIX session so it can outlive the short client process. Daemon singleton and database locks from ADR-0008 remain the authority under concurrent autostart attempts: multiple clients may race to launch, but only one daemon may own the canonical endpoint/database.

### Explicit socket overrides

`harness status --socket PATH` and `harness scan --socket PATH` do not autostart the canonical daemon. Explicit sockets remain caller-managed development/recovery/test endpoints and preserve their prior direct-IPC failure behavior.

### Failure classification

Only local IPC transport unavailability triggers autostart after a trusted runtime directory is present. Protocol or structured remote errors are not treated as evidence that another daemon should be launched.

Autostart readiness is bounded. A spawn failure or a daemon that does not become ready within the deadline is returned as a bounded CLI failure; the client does not loop indefinitely.

## Consequences

### Positive

- A clean POSIX install no longer requires a separate manual `harnessd serve` step before canonical `status`/`scan` usage.
- Existing runtime-directory security checks remain fail-closed.
- Concurrent startup relies on the already-proven daemon singleton/database ownership contract instead of adding a second coordination mechanism.
- No systemd/launchd installer, pidfile protocol, or service-manager abstraction is introduced.
- Explicit custom socket workflows remain side-effect free with respect to the canonical daemon.

### Costs and limits

- The first canonical client call may spend up to three seconds waiting for daemon readiness before failing.
- A healthy canonical client performs one small `status` probe before its requested Workspace operation.
- The daemon currently has no public stop command or OS service registration; it remains a lazily detached user process.
- Windows remains outside this decision until its local-user IPC/runtime path contract is implemented.

## Verification

Automated tests must prove:

- a reachable canonical daemon is reused without spawning another process;
- a missing canonical runtime directory triggers one detached package-module launch and then readiness validation;
- an unavailable transport in an existing secure runtime directory triggers one launch and retry;
- an existing insecure runtime directory fails closed without spawning;
- process creation failures are reported as bounded autostart errors;
- canonical `status` and `scan` paths invoke the autostart boundary;
- explicit `--socket` status/scan paths never invoke canonical autostart.
