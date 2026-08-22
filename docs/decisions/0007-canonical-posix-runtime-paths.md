# ADR-0007: Canonical POSIX runtime paths use XDG-compatible per-user defaults

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline

## Context

Harness is installed once per user and `harnessd` is one daemon per OS user, but the implementation foundation initially required callers to pass explicit database and Unix-socket paths every time. That was useful while storage/IPC contracts were still being proven, but it is not an acceptable steady-state user contract and prevents thin clients from finding the per-user daemon without extra configuration.

The path decision must preserve these invariants:

- durable state and runtime IPC are per-user, not repository-local;
- the daemon remains the owner of durable business state;
- POSIX local IPC stays on a Unix-domain socket with the existing current-user-only directory checks;
- explicit path overrides remain available for tests, development, recovery, and isolated/manual operation;
- Windows transport/path conventions are not guessed before the named-pipe/equivalent transport is implemented and proven.

## Decision

On supported POSIX systems Harness uses one deterministic default database path and one deterministic default Unix-socket path.

### Durable database

If `XDG_STATE_HOME` is set to an absolute path:

```text
$XDG_STATE_HOME/harness/harness.db
```

Otherwise:

```text
~/.local/state/harness/harness.db
```

Relative or empty `XDG_STATE_HOME` values are ignored rather than treated as repository-relative paths.

Before `harnessd` uses this default database location, the final Harness state directory is created/validated as a real directory (not a symlink), owned by the effective OS user, with no group/other permissions. Explicit `--database PATH` overrides keep their existing caller-selected filesystem semantics.

### Runtime socket

If `XDG_RUNTIME_DIR` is set to an absolute path:

```text
$XDG_RUNTIME_DIR/harness/harness.sock
```

Otherwise Harness uses the process temporary directory with the effective numeric UID embedded in the private Harness directory:

```text
<TEMP>/harness-<uid>/harness.sock
```

The final Harness socket directory must be a real directory (not a symlink), owned by the effective OS user, with no group/other permissions. The daemon creates/validates that directory before bind. A client using the canonical socket validates the same directory before connecting, so a pre-created symlink or another user's directory in a shared temporary namespace is not trusted as the per-user daemon endpoint. Relative or empty `XDG_RUNTIME_DIR` values are ignored.

### CLI behavior

`harness status [PATH]` uses the canonical socket automatically. `--socket PATH` remains an explicit override.

`harnessd serve` uses both canonical defaults automatically. `--database PATH` and `--socket PATH` remain optional overrides and may be supplied independently.

`harness doctor` keeps its current contract: with no `--database`, it checks only the SQLite runtime/FTS5 and does not create or inspect durable state implicitly.

Path selection itself performs no daemon autostart, registration, scan, MCP setup, or host configuration.

## Consequences

### Positive

- Installed human/bridge clients have one deterministic place to find the per-user daemon.
- Manual daemon startup no longer requires copying database/socket arguments into every command.
- Durable database state does not live in a transient runtime directory.
- Socket fallback remains per-user and short enough for common Unix-domain socket path limits.
- Canonical clients fail closed on spoofable final runtime-directory entries rather than connecting through a symlink or another user's directory.
- Explicit overrides preserve isolated test/recovery workflows.

### Costs and limits

- This decision is POSIX-only until the Windows local-user IPC transport is implemented.
- Daemon singleton ownership and crash-stale socket recovery are defined separately by ADR-0008; this path decision still performs no autostart or OS service management.
- This does not create a public Project/Workspace registration workflow or scan command.
- The XDG-compatible fallback on macOS is a Harness convention rather than a claim that macOS natively defines XDG variables.

## Verification

Automated tests must prove:

- absolute XDG state/runtime bases produce the exact documented paths;
- relative XDG values are ignored and deterministic fallbacks are used;
- the canonical state directory is created private to the effective user and insecure existing state directories fail closed;
- state/runtime final directories that are symlinks are rejected even when their targets are private current-user directories;
- `harness status` uses the canonical socket when no override is supplied and validates its canonical runtime directory before IPC;
- `harnessd serve` uses the canonical database/socket defaults and prepares the default state directory;
- the daemon rejects a symlinked socket parent before bind;
- explicit CLI path overrides still bypass default selection as intended;
- installed-wheel help exposes the optional override contract.
