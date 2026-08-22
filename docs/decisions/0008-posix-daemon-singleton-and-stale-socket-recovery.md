# ADR-0008: POSIX daemon singleton uses process-lifetime endpoint and database locks

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

Harness is globally installed and the canonical runtime model requires one `harnessd` owner for durable Harness state. The existing POSIX daemon already binds a private per-user Unix-domain socket, but startup treated every existing socket-path entry as an unrecoverable conflict.

That behavior is safe against accidental replacement, but it is not sufficient for later autostart:

- a daemon crash can leave a stale Unix socket behind;
- two clients may try to start the daemon at the same time;
- startup must not let both processes initialize/migrate or write the same Harness database before singleton ownership is established;
- explicit socket overrides must not create a second writer for the same selected database;
- stale recovery must never turn into unconditional unlink of an unknown filesystem entry.

A full OS service manager is not required to establish these runtime invariants and would be premature in this slice.

## Decision

On supported POSIX systems, `harnessd` holds two advisory process-lifetime locks when serving:

```text
<socket>.lock
<resolved-database>.lock
```

The endpoint lock is a sibling of the selected Unix socket and serializes ownership and stale-socket classification for that endpoint. The database lock is a sibling of the resolved selected database path and serializes daemon ownership of the durable Harness store even when callers use different socket paths or equivalent database path aliases.

Harness opens each lock as a current-user regular file, rejects symlink/non-regular or multiply-linked identities, secures it to mode `0600`, acquires a non-blocking exclusive `flock`, and holds that file descriptor for the entire daemon lifetime.

The endpoint lock is acquired before stale-socket cleanup. The database lock is acquired before database initialization/migration and before the daemon opens its long-lived SQLite connection. Therefore a competing current implementation cannot mutate the same durable store through another socket endpoint.

Lock files are not removed on normal shutdown. Kernel lock ownership, not file presence, represents the live singleton. Leaving the inodes in place avoids unlink/recreate races and allows later processes to reuse the same protected lock paths.

For the canonical database, its parent directory is already subject to the canonical private state-directory contract from ADR-0007. Explicit `--database PATH` overrides retain their existing caller-selected filesystem semantics; Harness resolves the selected database path for lock identity and prepares the corresponding lock parent before database initialization.

### Existing socket classification

Only the process that holds the endpoint lock may classify an existing Unix socket at the selected endpoint.

Startup behavior is:

1. no existing socket path: continue startup;
2. existing non-socket or wrong-owner entry: fail closed without deleting it;
3. existing socket that accepts a local connection: treat it as live and fail the duplicate start;
4. existing same-user socket that refuses because no listener exists: treat it as crash-stale;
5. before deleting a stale socket, re-read its device/inode/type/owner and unlink only if the identity is unchanged;
6. an endpoint that cannot be safely classified fails closed rather than being removed;
7. before SQLite initialization, acquire the resolved database ownership lock; if another daemon owns that store, fail without binding a second serving socket.

A successful endpoint probe does not need to understand Harness IPC. Any process accepting connections at the protected endpoint is considered live enough that Harness must not replace it.

### Scope of singleton identity

The installed canonical database/socket pair gives the intended one-daemon-per-user behavior.

Explicit `--database PATH` and `--socket PATH` overrides continue to exist for tests, recovery, and isolated manual operation and may still be supplied independently. Endpoint ownership is per selected socket, while durable-state ownership is per resolved selected database. Separate daemon processes may therefore use separate explicit sockets only when they also use distinct Harness databases; changing only the socket does not create a second writer for the same store.

### Autostart

This ADR establishes the lifecycle primitive required by autostart but does not start background processes from `harness`, install a launchd/systemd service, or implement reconnect policy. Those remain separate bounded tasks.

Windows remains outside this decision until the Windows local-user IPC transport is implemented and proven.

## Consequences

### Positive

- A crashed daemon no longer permanently blocks the canonical POSIX endpoint with its stale socket.
- Concurrent starts on one endpoint are serialized before socket replacement.
- Concurrent starts targeting one Harness database are serialized before database initialization/migration even when socket paths differ.
- Duplicate starts do not disturb the live daemon or delete its socket.
- Unknown/non-socket endpoint entries remain fail-closed.
- Lock lifetime is crash-safe because the kernel releases `flock` ownership when the process exits.
- The design adds no service-manager framework and no new runtime dependency.

### Costs and limits

- `flock` is a POSIX-specific primitive and is intentionally not presented as Windows support.
- Sibling `.lock` files remain beside the selected socket and database after shutdown; this is expected and does not imply the daemon is running.
- A connectable legacy/foreign process occupying the selected socket prevents startup rather than being replaced.
- Explicit database paths keep caller-selected filesystem semantics; the stronger private-directory guarantee applies to the canonical state directory.
- This task does not make `harness status` or `harness scan` autostart the daemon.

## Verification

Automated tests must prove:

- a stale owned Unix socket is replaced only after endpoint-lock acquisition and the new daemon serves normally;
- endpoint and database lock files are current-user regular files with mode `0600`;
- a second daemon using the same socket fails before initializing a second database and the first daemon remains responsive;
- a second daemon using a different socket cannot acquire the same resolved database, including through an equivalent path alias, and the first daemon remains responsive;
- a symlinked lock path is rejected without modifying its target or initializing the database;
- existing non-socket endpoint entries remain preserved and fail closed;
- clean shutdown still removes only the socket inode created by the running daemon while leaving the reusable lock files.