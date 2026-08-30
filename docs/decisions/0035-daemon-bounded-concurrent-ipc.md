# ADR-0035: Daemon IPC uses bounded concurrent client workers

- **Status:** Accepted
- **Date:** 2026-08-31
- **Deciders:** Repository architecture baseline

## Context

Harness has one daemon per selected durable store, but the POSIX IPC accept loop originally handled each accepted client synchronously on the accept thread and reused one long-lived SQLite connection. That preserved simple ownership, yet it also coupled unrelated callers: a slow `scan_workspace`, Git probe, skill reconciliation, Task operation, or even a client that delayed request handling prevented the daemon from accepting a second request until the first request completed or timed out.

That behavior is incompatible with the host-neutral control-plane role. Cursor, Codex, Claude, the CLI, install/doctor flows, and future hosts may legitimately share one daemon. A bounded read such as `status` must not wait behind an unrelated long-running scan merely because both use the same local IPC endpoint.

SQLite is already required to run in WAL mode. Sharing one `sqlite3.Connection` across worker threads would weaken Python's connection-thread safety and would still create an unnecessary serialization point, so concurrency must use independent connections while retaining daemon-level ownership and existing domain serialization.

## Decision

The POSIX daemon accept loop dispatches accepted clients to a fixed pool of **8** IPC client workers.

The concurrency boundary is deliberately bounded:

- the daemon acquires one worker slot before `accept()`;
- at most 8 accepted clients are active at once;
- the Unix socket listen backlog is also bounded to the worker count;
- when all slots are occupied, the accept loop polls rather than building an unbounded in-process work queue;
- the accept loop continues to observe the external stop event and Workspace watcher health while waiting for capacity.

Each accepted client worker opens its **own** initialized Harness SQLite connection, serves exactly one request/response exchange, closes that connection, closes the client socket, and releases its worker slot. SQLite WAL therefore provides concurrent-reader behavior and SQLite's normal single-writer coordination without sharing a connection between threads.

The process-level ownership contract does not change. ADR-0008's database lock is still held for the full daemon lifetime, so another Harness daemon cannot concurrently own the same selected durable store. “One daemon owns write coordination” means one daemon process owns the store and domain transition boundary; it does not require one process-wide SQLite connection.

Existing higher-level serialization remains authoritative:

- `scan_workspace`, visibility transitions, and generated-skill reconciliation continue to share the existing daemon `scan_lock` where their external/index side effects require serialization;
- Task mutations continue to use explicit Task identity/revision concurrency contracts;
- IPC access to the shared lazy `DashboardServerManager` is serialized so concurrent clients cannot race dashboard start/restart state;
- SQLite transactions remain the durable atomicity boundary;
- the Workspace watcher retains its own connection and uses the same scan lock.

A client disconnect or ordinary socket `OSError` remains isolated to that client. An unexpected exception escaping a client handler is **not** silently swallowed by the worker pool: the worker reports it to the accept loop, which fails the daemon with `DaemonError`, preserving the previous fail-fast behavior of the synchronous implementation.

Shutdown stops acceptance first, signals the watcher, waits for accepted client workers to finish their bounded operations, then joins the watcher and closes the dashboard before releasing database/endpoint locks and unlinking only the owned socket inode. A worker handling the IPC `shutdown` method may set the shared stop event; the accept loop observes it on its bounded poll cycle.

This ADR supersedes only ADR-0008's implementation detail that the daemon opens one long-lived SQLite connection before serving. ADR-0008's endpoint/database singleton locks, stale-socket classification, and process-lifetime lock ownership remain unchanged.

## Consequences

### Positive

- A slow scan or other long IPC operation no longer head-of-line blocks unrelated status/read requests.
- Multiple supported hosts can use the same daemon concurrently without sharing a Python SQLite connection across threads.
- Resource use stays bounded rather than converting head-of-line blocking into an unbounded executor queue.
- Existing scan/visibility/skill serialization and Task optimistic-concurrency contracts remain intact.
- Unexpected worker failures still stop the daemon instead of becoming hidden partial failures.

### Costs and limits

- Each IPC exchange pays the small cost of opening and validating one SQLite connection.
- SQLite still permits only one writer at a time; concurrent write-heavy requests may wait on SQLite or existing domain locks.
- Eight simultaneously blocked clients can still exhaust the bounded worker pool until one completes or times out. This is an intentional local resource bound, not a promise of unlimited parallelism.
- This decision does not implement cancellation of an already-running domain operation when its client disconnects.
- Windows remains outside this POSIX transport decision.

## Verification

Automated tests must prove:

- a deliberately blocked client handler does not prevent an independent `status` request from completing;
- a deliberately blocked `scan_workspace` does not prevent `status` from reading the same WAL database through a separate connection;
- the same regression tests fail against the former synchronous accept-loop implementation;
- an unexpected exception escaping a client handler surfaces as daemon failure rather than being swallowed by the worker pool;
- existing IPC protocol, scan, watcher, installation/shutdown, singleton, and installed-wheel smoke tests remain green;
- strict mypy and Ruff continue to pass without relaxing the repository quality contract.
