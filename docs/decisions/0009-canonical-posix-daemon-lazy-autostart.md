# ADR-0009: Canonical POSIX clients lazily autostart the per-user daemon

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

Harness is installed once per user and the architecture requires one long-lived `harnessd` owner for durable business state. ADR-0007 established canonical per-user database/socket paths and ADR-0008 established safe singleton ownership and crash-stale socket recovery.

Before this decision, `harness status` and `harness scan` still required the user to start `harnessd serve` manually. A clean installation with no runtime directory therefore failed before any useful command could reach the daemon, which is not install-ready behavior.

A full systemd/launchd service manager would add platform-specific installation and lifecycle surface that is not required to remove this immediate first-run burden.

## Decision

On supported POSIX systems, client commands that use the canonical socket lazily ensure the canonical daemon endpoint is available before issuing their Workspace request.

The bounded behavior is:

1. resolve the canonical runtime paths;
2. if the final runtime directory already exists, validate that it is a real current-user-only directory before trusting any socket inside it;
3. probe the canonical daemon with the bounded internal `status` request;
4. launch a detached child from the same installed Python package with `python -m harness.daemon_process` only when the trusted endpoint is confirmed absent by `ENOENT` or `ECONNREFUSED`;
5. wait for at most three seconds while the runtime directory/endpoint remains confirmed absent;
6. continue with the original Workspace request when the status probe succeeds or when a probe timeout shows that an endpoint is already occupied/busy enough that starting another daemon would be unsafe.

If the canonical runtime directory does not yet exist, that absence is treated as a first-run state: the client launches the daemon and waits for the daemon to create and secure the directory. An existing insecure/symlinked/untrusted runtime directory still fails closed and is never repaired or replaced by the client.

The child redirects stdin/stdout/stderr to the null device, closes inherited file descriptors, and starts a new POSIX session so it can outlive the short client process. Daemon singleton and database locks from ADR-0008 remain the authority under concurrent autostart attempts: multiple clients may race to launch, but only one daemon may own the canonical endpoint/database.

### Explicit socket overrides

`harness status --socket PATH` and `harness scan --socket PATH` do not autostart the canonical daemon. Explicit sockets remain caller-managed development/recovery/test endpoints and preserve their prior direct-IPC failure behavior.

### Failure classification

Autostart requires positive evidence that the canonical endpoint is absent. For the current POSIX Unix-socket transport, only `ENOENT` and `ECONNREFUSED` from the bounded IPC probe are treated as absence after the runtime directory has passed its trust check.

A probe timeout is not evidence of absence. The daemon currently serves clients sequentially and a deterministic scan may occupy it for substantially longer than the short autostart probe; a timeout therefore means the endpoint may be live/busy and must not trigger a duplicate process. The original Workspace request then uses its command-specific IPC timeout. Other unclassified transport failures, protocol errors, and structured remote errors fail closed rather than starting another daemon.

Autostart readiness is bounded. A spawn failure or an endpoint that remains positively absent through the deadline is returned as a bounded CLI failure; the client does not loop indefinitely.

## Consequences

### Positive

- A clean POSIX install no longer requires a separate manual `harnessd serve` step before canonical `status`/`scan` usage.
- Existing runtime-directory security checks remain fail-closed.
- A live daemon that is busy with a long serial request is never mistaken for an absent daemon merely because the short probe timed out.
- Concurrent startup relies on the already-proven daemon singleton/database ownership contract instead of adding a second coordination mechanism.
- No systemd/launchd installer, pidfile protocol, or service-manager abstraction is introduced.
- Explicit custom socket workflows remain side-effect free with respect to the canonical daemon.

### Costs and limits

- The first canonical client call may spend up to three seconds waiting for a positively absent endpoint to appear before failing.
- A healthy canonical client performs one small `status` probe before its requested Workspace operation.
- A busy daemon may cause the requested command itself to hit its existing command-specific IPC timeout; autostart does not add a second daemon to work around daemon serialization.
- The daemon currently has no public stop command or OS service registration; it remains a lazily detached user process.
- Windows remains outside this decision until its local-user IPC/runtime path contract is implemented.

## Verification

Automated tests must prove:

- a reachable canonical daemon is reused without spawning another process;
- a short probe timeout is treated as a busy/live-enough endpoint and never triggers another process;
- a missing canonical runtime directory triggers one detached package-module launch and then readiness validation;
- confirmed endpoint absence (`ENOENT`/`ECONNREFUSED`) in an existing secure runtime directory triggers one launch and retry;
- a concurrent-start readiness probe that times out does not trigger or wait for another daemon;
- unclassified transport failures fail closed without spawning;
- an existing insecure runtime directory fails closed without spawning;
- process creation failures are reported as bounded autostart errors;
- canonical `status` and `scan` paths invoke the autostart boundary;
- explicit `--socket` status/scan paths never invoke canonical autostart.
