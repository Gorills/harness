# ADR-0009: POSIX canonical clients autostart the daemon on demand

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

Harness is intended to be installed once per user and used without requiring the user to remember a separate foreground `harnessd serve` command. The canonical runtime paths from ADR-0007 and the endpoint/database singleton and stale-socket recovery from ADR-0008 provide the safety primitives needed for concurrent startup, but the implemented `harness status` and `harness scan` clients still failed whenever no daemon was already listening.

The audit intentionally left the cross-platform packaging/autostart mechanism open. A launchd/systemd service or platform installer is not required to remove the immediate POSIX user friction, and choosing one prematurely would expand this bounded slice into OS service management.

Autostart must remain fail-closed. A client must not spawn a daemon merely because an existing endpoint timed out, rejected the protocol, reset a connection, or otherwise behaved ambiguously: those cases may represent a live Harness process, a foreign process, a permission problem, or a transport fault that autostart must not overwrite or obscure.

## Decision

On supported POSIX systems, `harness status` and `harness scan` use on-demand autostart only when they are using the canonical per-user socket.

The client sequence is:

1. resolve and validate the requested Workspace filesystem location locally;
2. resolve the canonical runtime paths;
3. create or validate the canonical runtime directory as a real current-user-only directory with no group/other access;
4. attempt the requested IPC operation normally;
5. if and only if the underlying transport failure is `ENOENT` or `ECONNREFUSED`, start the canonical daemon once;
6. wait for a bounded daemon `status` readiness probe;
7. retry the original operation exactly once.

`ENOENT` means no endpoint path was available to accept the connection. `ECONNREFUSED` also covers the crash-stale Unix-socket case that ADR-0008 can recover safely. Other transport failures, protocol failures, daemon-returned errors, and timeouts do not trigger autostart.

### Process launch

The client starts the daemon with the same Python interpreter that is running the installed `harness` command:

```text
<python> -P -m harness.daemon_process serve
```

The child inherits the user's environment so canonical XDG/home/runtime path selection remains identical to the caller. Python safe-path mode (`-P`) prevents the caller's current Workspace from being prepended to the child import path and therefore prevents a repository-local `harness` package from shadowing the installed daemon module merely because the user ran the command inside that repository.

The process is detached from the invoking terminal session, receives `/dev/null`-style standard streams through `subprocess.DEVNULL`, switches its working directory to the POSIX root (`/`) so the global daemon does not retain the launching Workspace or its mount as process cwd, and continues running after the short-lived CLI command exits. Readiness is proven through the existing bounded IPC `status` contract rather than through process existence or socket-file existence.

Concurrent clients may both attempt to spawn a daemon. That race is intentional and safe: ADR-0008 endpoint and database locks ensure that only one process can own the canonical endpoint/store, while every client independently waits for the one healthy daemon to answer the status probe.

### Explicit overrides

Passing `--socket PATH` disables autostart. Explicit sockets are development/recovery/manual-operation contracts and do not identify which database a client intends a newly spawned daemon to own. Harness therefore never guesses a database or launches a background process for an explicit socket.

Manual `harnessd serve [--database PATH] [--socket PATH]` remains available and unchanged.

### Read-only status semantics

The Workspace-status domain operation remains read-only: it does not register a Project/Workspace, scan files, or mutate business state. However, a first canonical `harness status` invocation may now start the daemon, and daemon startup may create the canonical runtime/state directories, lock files, and initialize or migrate the canonical database as part of daemon lifecycle bootstrap. That lifecycle effect is distinct from the `workspace_status` domain operation and is documented rather than hidden.

### Scope boundary

This decision does not implement:

- `harness install` / `harness uninstall`;
- launchd, systemd, login items, or another OS service manager;
- daemon idle shutdown or persistent service registration;
- autostart for explicit socket overrides;
- MCP-bridge autostart yet;
- Windows named-pipe transport or Windows service/autostart behavior.

Those remain separate bounded tasks.

## Consequences

### Positive

- Canonical `harness scan` and `harness status` no longer require a separate manual daemon command on supported POSIX systems.
- Crash-stale canonical sockets can recover through the same user operation that needed the daemon.
- Concurrent first-use clients converge on one daemon through the already-proven singleton locks.
- Ambiguous/live/foreign endpoint failures remain fail-closed instead of causing speculative background process launches.
- The implementation does not depend on PATH lookup for a `harnessd` executable and avoids current-directory module shadowing.
- The global daemon does not retain whichever project directory happened to launch it.
- No service-manager dependency or platform-specific configuration artifact is introduced.

### Costs and limits

- The detached daemon has no idle-shutdown policy in this slice and normally lives until explicitly terminated or the user session/system ends.
- Startup failure is reported as a bounded readiness/autostart failure; this slice does not introduce daemon log-file management.
- A canonical read-only command can cause daemon lifecycle bootstrap/migration side effects when no daemon is running.
- The transport classification currently relies on the concrete `OSError` chained beneath `IpcTransportError`; changing IPC exception chaining requires preserving this autostart contract or introducing an explicit equivalent signal.
- Windows remains unsupported for this behavior.

## Verification

Automated tests must prove:

- canonical clients create/validate the private runtime directory before IPC;
- canonical status and scan autostart once on `ENOENT`/`ECONNREFUSED` and retry the original request once;
- explicit `--socket` never autostarts;
- permission-denied, timeout/reset, protocol, or daemon-returned failures do not trigger speculative startup;
- the autostart child uses the same Python interpreter, safe-path mode, the daemon module entrypoint, detached session behavior, closed standard streams, and a working directory independent of the launching Workspace;
- readiness is based on a bounded successful daemon status request rather than socket-file existence;
- a real missing Unix-socket request preserves an underlying error classification that permits autostart;
- canonical runtime-directory symlink/permission safety remains fail-closed;
- the installed wheel contains a runnable daemon module entrypoint and existing console scripts remain healthy.
