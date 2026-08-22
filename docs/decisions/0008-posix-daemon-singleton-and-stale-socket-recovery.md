# ADR-0008: POSIX daemon singleton uses a process-lifetime lock and bounded stale-socket recovery

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

Harness is globally installed and the canonical runtime model requires one `harnessd` per OS user. The existing POSIX daemon already binds a private per-user Unix-domain socket, but startup treated every existing socket-path entry as an unrecoverable conflict.

That behavior is safe against accidental replacement, but it is not sufficient for later autostart:

- a daemon crash can leave a stale Unix socket behind;
- two clients may try to start the daemon at the same time;
- startup must not let both processes initialize/migrate the same canonical database before singleton ownership is established;
- stale recovery must never turn into unconditional unlink of an unknown filesystem entry.

A full OS service manager is not required to establish these runtime invariants and would be premature in this slice.

## Decision

On supported POSIX systems, `harnessd` serializes ownership of each selected Unix-socket endpoint with a sibling advisory lock file:

```text
harness.sock
harness.sock.lock
```

The lock file lives in the same private current-user-only runtime directory as the socket. Harness opens it as a current-user regular file, rejects symlink/non-regular or multiply-linked identities, secures it to mode `0600`, acquires a non-blocking exclusive `flock`, and holds that file descriptor for the entire daemon lifetime.

The lock is acquired before database initialization/migration and before any stale-socket cleanup. Therefore a competing current implementation cannot reach database mutation or socket replacement after another daemon owns the endpoint.

The lock file itself is not removed on normal shutdown. Kernel lock ownership, not file presence, represents the live singleton. Leaving the inode in place avoids unlink/recreate races and allows the next process to reuse the same protected lock path.

### Existing socket classification

Only the process that holds the singleton lock may classify an existing Unix socket at the selected endpoint.

Startup behavior is:

1. no existing socket path: bind normally;
2. existing non-socket or wrong-owner entry: fail closed without deleting it;
3. existing socket that accepts a local connection: treat it as live and fail the duplicate start;
4. existing same-user socket that refuses because no listener exists: treat it as crash-stale;
5. before deleting a stale socket, re-read its device/inode/type/owner and unlink only if the identity is unchanged;
6. an endpoint that cannot be safely classified fails closed rather than being removed.

A successful probe does not need to understand Harness IPC. Any process accepting connections at the protected endpoint is considered live enough that Harness must not replace it.

### Scope of singleton identity

The installed canonical socket gives the intended one-daemon-per-user behavior.

Explicit `--socket PATH` overrides continue to exist for tests, recovery, and isolated manual operation. Singleton ownership is therefore per selected socket endpoint; separate explicit socket paths may intentionally run separate isolated daemons.

### Autostart

This ADR establishes the lifecycle primitive required by autostart but does not start background processes from `harness`, install a launchd/systemd service, or implement reconnect policy. Those remain separate bounded tasks.

Windows remains outside this decision until the Windows local-user IPC transport is implemented and proven.

## Consequences

### Positive

- A crashed daemon no longer permanently blocks the canonical POSIX endpoint with its stale socket.
- Concurrent daemon starts are serialized before durable database initialization/migration.
- Duplicate starts do not disturb the live daemon or delete its socket.
- Unknown/non-socket endpoint entries remain fail-closed.
- Lock lifetime is crash-safe because the kernel releases `flock` ownership when the process exits.
- The design adds no service-manager framework and no new runtime dependency.

### Costs and limits

- `flock` is a POSIX-specific primitive and is intentionally not presented as Windows support.
- A sibling `.lock` file remains in the runtime directory after shutdown; this is expected and does not imply the daemon is running.
- A connectable legacy/foreign process occupying the selected socket prevents startup rather than being replaced.
- This task does not make `harness status` or `harness scan` autostart the daemon.

## Verification

Automated tests must prove:

- a stale owned Unix socket is replaced only after singleton lock acquisition and the new daemon serves normally;
- the process-lifetime lock file is a current-user regular file with mode `0600`;
- a second daemon using the same socket fails before initializing a second database and the first daemon remains responsive;
- a symlinked lock path is rejected without modifying its target or initializing the database;
- existing non-socket endpoint entries remain preserved and fail closed;
- clean shutdown still removes only the socket inode created by the running daemon while leaving the reusable lock file.
