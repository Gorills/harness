# Harness

Harness is a local-first control plane for coding agents. It preserves project intelligence across agent sessions and hosts while keeping source editing, shell access, Git, and browsing in the native host.

> **Repository status:** implementation foundation. Packaging/tooling, SQLite persistence, Project/Workspace registry primitives, read-only runtime doctor checks, Workspace status and deterministic scan CLIs, bounded daemon/local-IPC status and scan paths, POSIX daemon singleton/stale-socket recovery, on-demand canonical POSIX daemon autostart for status/scan, deterministic file-index reconciliation, and canonical POSIX per-user runtime paths exist; MCP, search, Tasks, watcher, host integration, and dashboard behavior are not implemented yet.

## Product principles

- One global Harness installation, isolated project context.
- Native-agent-first integration through MCP.
- Progressive disclosure instead of bulk context dumps.
- Filesystem and Git remain sources of truth for code and repository state.
- Semantic knowledge is captured only after real task investigation and always has provenance.
- A small, stable model-facing surface: status, search, context, task start/resume, checkpoint.
- One modular-monolith core; host-specific behavior stays in adapters.
- Two explicit publication modes: Normal for ordinary native-host SCM behavior, Hidden for local-only agent integration with human-owned Git/SCM publication.

## Start here

1. Read [`docs/specification.md`](docs/specification.md) for the original approved product specification.
2. Read [`docs/audits/2026-08-21-spec-audit.md`](docs/audits/2026-08-21-spec-audit.md) for the independent audit against current official MCP and host documentation.
3. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for the corrected implementation baseline.
4. Read [`AGENTS.md`](AGENTS.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md) before making changes.
5. Check [`docs/host-compatibility.md`](docs/host-compatibility.md) before touching host adapters or skill projection.

## Architecture decisions

- [ADR-0001: MCP 2026-07-28 and a sessionless core](docs/decisions/0001-mcp-2026-07-28-sessionless-core.md)
- [ADR-0002: Host integration and workspace resolution boundaries](docs/decisions/0002-host-integration-and-workspace-resolution.md)
- [ADR-0003: Normal and Hidden project visibility modes](docs/decisions/0003-normal-and-hidden-visibility-modes.md)
- [ADR-0004: Hidden mode transitions require capability revocation](docs/decisions/0004-hidden-mode-transition-barrier.md)
- [ADR-0005: Hidden transition proof must survive daemon recovery](docs/decisions/0005-hidden-transition-recovery-safety.md)
- [ADR-0006: Hidden mode transitions use crash-safe commit ordering](docs/decisions/0006-hidden-transition-crash-atomicity.md)
- [ADR-0007: Canonical POSIX runtime paths use XDG-compatible per-user defaults](docs/decisions/0007-canonical-posix-runtime-paths.md)
- [ADR-0008: POSIX daemon singleton and stale-socket recovery](docs/decisions/0008-posix-daemon-singleton-and-stale-socket-recovery.md)
- [ADR-0009: POSIX canonical clients autostart the daemon on demand](docs/decisions/0009-posix-on-demand-canonical-daemon-autostart.md)

## Development state

The repository now has a Python 3.13 package, a locked development toolchain, and SQLite schema version 3. Database initialization creates ordered migration metadata, requires WAL mode, enables foreign-key enforcement for Harness connections, probes FTS5 as a runtime capability, and migrates the initial bootstrap schemas forward without discarding unrelated existing data.

Schema v3 contains the durable Project/Workspace registry plus the first rebuildable Structural Index table, `indexed_files`. New Projects are created with the required `normal` visibility default. The low-level explicit registration primitive remains internal and requires an explicit Project identity plus an inspectable Git worktree; Harness stores the canonical worktree root and Git common directory reported by Git, treats repeat registration as idempotent, fails closed on incompatible root/Project identity, and rejects contradictory visibility policy for Workspaces sharing one Git common directory. Public scan registration reuses an already registered root, attaches a new linked worktree to the one unambiguous existing Project sharing its Git common directory, and creates a new Project for a Git checkout whose common directory is not registered. It does not infer logical Project identity across independent clones or expose visibility-mode transitions.

The first deterministic index slice inventories regular files and symlinks for one registered Workspace, stores only relative path, entry kind, byte size, and SHA-256 mechanical identity, and reconciles additions/changes/deletions transactionally. Candidate enumeration uses Git tracked/untracked state with standard Git ignores, `.harnessignore`, common generated-directory exclusions, and default sensitive patterns such as `.env`, PEM, and key files. Symlink targets are hashed as link metadata rather than followed for source content, and symlinked parent paths that escape the Workspace fail closed. The index is derived/rebuildable data. Daemon-backed scans carry a finite execution deadline; deadline expiry rolls back an in-progress index reconciliation. This slice still does not run a filesystem watcher, parse symbols/languages, populate FTS, or implement search.

The installed `harness` CLI includes `harness doctor`. With no database argument it checks the current SQLite runtime and FTS5 availability using only an in-memory database, so it creates no durable Harness state. `harness doctor --database PATH` additionally inspects an explicitly selected, already initialized Harness database for supported schema state, WAL mode, foreign-key enforcement, and FTS5 availability. Database inspection does not create or migrate the selected path; a missing or invalid database is reported as a bounded failure. Even though the daemon now has a canonical database default, `doctor` intentionally requires `--database PATH` before inspecting durable state so the no-argument diagnostic stays runtime-only. The broader doctor contract (daemon, permissions, registrations, host adapters, projects, index state, skills, dashboard, and stale integrations) is not implemented yet.

The installed CLI exposes `harness scan [PATH] [--socket PATH]`. `PATH` defaults to the current directory and may be any directory inside a Git worktree. The CLI canonicalizes it locally, then the daemon resolves the Git worktree root, registers or reuses durable Project/Workspace identity as described above, and performs deterministic local non-LLM index reconciliation. The result reports Project/Workspace identity, whether either identity was newly created, effective visibility mode, canonical Workspace root, total indexed files, and added/updated/removed counts. Registry identity is durable state while the Structural Index is rebuildable derived state: if registration succeeds but a later reconciliation fails, the registered Workspace remains and a retry reuses it rather than inventing a second identity. With no `--socket`, the CLI creates/validates the private canonical runtime directory and first tries the canonical per-user daemon normally. If the transport proves that no endpoint accepted the connection (`ENOENT` or `ECONNREFUSED`), the CLI starts the canonical daemon once, waits for a bounded status readiness probe, and retries the scan once. Passing `--socket` is an explicit development/recovery override and disables autostart.

The installed CLI also exposes `harness status [PATH] [--socket PATH]`. `PATH` defaults to the current directory and is treated as a filesystem location inside an already registered Workspace. The command resolves that location through the daemon's fail-closed Workspace resolver and prints only compact mechanical state: Project/Workspace identity, canonical root, visibility mode, Git branch/HEAD, dirty-path summary, indexed-file count, and schema version. The Workspace-status domain operation is read-only: it does not register a Project/Workspace, run a scan, or mutate business state. With no `--socket`, the same canonical on-demand daemon behavior used by `scan` applies; therefore a first invocation may create daemon lifecycle artifacts and initialize/migrate the canonical database before the read-only Workspace-status request runs. Passing `--socket` disables autostart and connects only to the explicitly selected endpoint.

On supported POSIX systems the canonical daemon database is `$XDG_STATE_HOME/harness/harness.db` when `XDG_STATE_HOME` is an absolute path, otherwise `~/.local/state/harness/harness.db`. The canonical socket is `$XDG_RUNTIME_DIR/harness/harness.sock` when `XDG_RUNTIME_DIR` is absolute, otherwise `<system temporary directory>/harness-<effective uid>/harness.sock`. Empty/relative XDG values are ignored. Before the default database is used, the final Harness state directory is created/validated as a real (non-symlink) current-user-only directory. Canonical clients create/validate the final socket directory with the same real current-user-only contract before IPC/autostart; the daemon re-validates it before bind, so pre-created symlink entries in a shared temporary namespace fail closed.

The installed `harnessd` console script exposes `harnessd serve [--database PATH] [--socket PATH]`. With no overrides it initializes/migrates the canonical per-user database and binds the canonical per-user Unix-domain socket. The daemon serves the implemented global status, Workspace status, and deterministic Workspace scan requests; only the scan path mutates durable registry/index state. The two path options remain available independently for development, tests, recovery, or isolated manual operation; concurrent isolated daemon instances require distinct selected Harness databases. On supported POSIX systems the socket directory must be a real directory owned by the current OS user with no group/other access. Harness holds process-lifetime `flock` ownership for both the selected socket endpoint (`<socket>.lock`) and the resolved selected database (`<database>.lock`), with both lock files secured to `0600`. The endpoint lock is acquired before stale-socket classification; the database lock is acquired before SQLite initialization/migration. A competing daemon therefore cannot reuse the endpoint or become a second writer for the same Harness database through another socket or an equivalent database path alias. If the endpoint lock is free after a crash, Harness removes an existing same-user Unix socket only when it cannot accept a connection and its device/inode identity is unchanged at unlink time; connectable sockets and non-socket/unsafe entries fail closed. Harness creates a missing final runtime directory with mode `0700`, sets the served socket to `0600`, removes only the socket inode it created on clean shutdown, and intentionally leaves the reusable lock files in place.

Canonical POSIX client autostart launches the installed daemon with the same Python interpreter using safe-path mode (`python -P -m harness.daemon_process serve`), detached from the invoking terminal with closed standard streams. The `-P` flag prevents a repository-local `harness` package from shadowing the installed daemon module through the caller's current working directory. Readiness is proven by the existing bounded daemon status IPC request rather than process existence or socket-file presence. Concurrent first-use clients may each attempt a launch; the endpoint/database singleton locks make that race converge on one daemon. Timeouts, permission errors, connection resets, protocol failures, and daemon-returned errors do not trigger speculative autostart.

The global status result remains deliberately small: schema version plus Project and Workspace counts. `workspace_status` accepts at most four ordered absolute filesystem hints, resolves them against the durable Workspace registry with the existing fail-closed priority rules, verifies the registered Git identity, and returns only compact mechanical state: Project/Workspace identity, effective visibility mode, canonical Workspace root, live Git branch/HEAD, dirty-path count, and current indexed-file count. It does not expose source text, search results, Tasks, Knowledge, or MCP objects.

The current internal IPC protocol is independent of MCP wire objects, uses protocol version `1`, allows exactly one request/response per connection, enforces a 16 KiB message limit, uses bounded request/response timeouts, and rejects unknown fields/methods fail-closed. `workspace_status` and `scan_workspace` are additive protocol-v1 methods; the original global `status` wire shape remains unchanged. The scan client uses a longer bounded receive timeout than read-only status because the daemon performs deterministic filesystem reconciliation, while the daemon applies its own finite scan execution deadline and returns a structured `scan_timeout` error when that deadline is exceeded. POSIX canonical `harness status` and `harness scan` now have bounded on-demand daemon autostart, but this slice still does not implement `harness install`/`uninstall`, install/manage an OS service, autostart an MCP bridge, expose MCP, or claim a verified Windows named-pipe/equivalent transport; Windows daemon IPC and canonical Windows transport paths therefore remain unsupported until that separate platform task is proven.

Development uses `uv 0.12.5`. Install/sync exactly from the committed lock and run the repository quality gate with:

```text
uv sync --locked --all-groups
uv run --frozen python scripts/quality.py
```

The quality gate checks lock freshness, formatting, lint, strict typing, pytest, and a wheel smoke test that installs the built artifact into an isolated environment, executes both shipping console scripts plus the safe-path daemon module entrypoint used by autostart, verifies that installed CLI help exposes the implemented commands/runtime-path override contract, and runs the installed `harness doctor` command.

## License

No license has been selected yet. Do not assume redistribution or contribution terms until the repository owner chooses one.
