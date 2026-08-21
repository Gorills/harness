# Harness

Harness is a local-first control plane for coding agents. It preserves project intelligence across agent sessions and hosts while keeping source editing, shell access, Git, and browsing in the native host.

> **Repository status:** implementation foundation. Packaging/tooling, SQLite persistence, Project/Workspace registry primitives, read-only runtime doctor checks, a read-only Workspace status CLI, bounded daemon/local-IPC status paths, deterministic file-index reconciliation primitives, and canonical POSIX per-user runtime paths exist; MCP, search, Tasks, host integration, and dashboard behavior are not implemented yet.

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

## Development state

The repository now has a Python 3.13 package, a locked development toolchain, and SQLite schema version 3. Database initialization creates ordered migration metadata, requires WAL mode, enables foreign-key enforcement for Harness connections, probes FTS5 as a runtime capability, and migrates the initial bootstrap schemas forward without discarding unrelated existing data.

Schema v3 contains the durable Project/Workspace registry plus the first rebuildable Structural Index table, `indexed_files`. New Projects are created with the required `normal` visibility default. Workspace registration is internal-only and requires an explicit Project identity plus an inspectable Git worktree; Harness stores the canonical worktree root and Git common directory reported by Git, treats repeat registration as idempotent, fails closed on incompatible root/Project identity, and rejects contradictory visibility policy for Workspaces sharing one Git common directory. It does not infer logical Project identity across independent clones or expose visibility-mode transitions.

The first deterministic index slice inventories regular files and symlinks for one registered Workspace, stores only relative path, entry kind, byte size, and SHA-256 mechanical identity, and reconciles additions/changes/deletions transactionally. Candidate enumeration uses Git tracked/untracked state with standard Git ignores, `.harnessignore`, common generated-directory exclusions, and default sensitive patterns such as `.env`, PEM, and key files. Symlink targets are hashed as link metadata rather than followed for source content, and symlinked parent paths that escape the Workspace fail closed. The index is derived/rebuildable data. This slice does not yet expose `harness scan`, run a filesystem watcher, parse symbols/languages, populate FTS, or implement search.

The installed `harness` CLI includes `harness doctor`. With no database argument it checks the current SQLite runtime and FTS5 availability using only an in-memory database, so it creates no durable Harness state. `harness doctor --database PATH` additionally inspects an explicitly selected, already initialized Harness database for supported schema state, WAL mode, foreign-key enforcement, and FTS5 availability. Database inspection does not create or migrate the selected path; a missing or invalid database is reported as a bounded failure. Even though the daemon now has a canonical database default, `doctor` intentionally requires `--database PATH` before inspecting durable state so the no-argument diagnostic stays runtime-only. The broader doctor contract (daemon, permissions, registrations, host adapters, projects, index state, skills, dashboard, and stale integrations) is not implemented yet.

The installed CLI also exposes `harness status [PATH] [--socket PATH]`. `PATH` defaults to the current directory and is treated as a filesystem location inside an already registered Workspace. The command resolves that location through the daemon's fail-closed Workspace resolver and prints only compact mechanical state: Project/Workspace identity, canonical root, visibility mode, Git branch/HEAD, dirty-path summary, indexed-file count, and schema version. It is read-only: it does not register a Project/Workspace, run a scan, or mutate the database. With no `--socket`, it connects to the canonical per-user daemon socket; the option remains available as an explicit override.

On supported POSIX systems the canonical daemon database is `$XDG_STATE_HOME/harness/harness.db` when `XDG_STATE_HOME` is an absolute path, otherwise `~/.local/state/harness/harness.db`. The canonical socket is `$XDG_RUNTIME_DIR/harness/harness.sock` when `XDG_RUNTIME_DIR` is absolute, otherwise `<system temporary directory>/harness-<effective uid>/harness.sock`. Empty/relative XDG values are ignored. Before the default database is used, the final Harness state directory is created/validated as current-user-only; the daemon independently enforces the existing current-user-only ownership/mode rules on its socket directory.

The installed `harnessd` console script exposes `harnessd serve [--database PATH] [--socket PATH]`. With no overrides it initializes/migrates the canonical per-user database and binds the canonical per-user Unix-domain socket. The two options remain available independently for development, tests, recovery, or isolated manual operation. On supported POSIX systems the socket directory must be owned by the current OS user with no group/other access; Harness creates a missing final directory with mode `0700`, sets the socket to `0600`, refuses to replace an existing socket-path entry, and removes only the socket inode it created on clean shutdown.

The global status result remains deliberately small: schema version plus Project and Workspace counts. `workspace_status` accepts at most four ordered absolute filesystem hints, resolves them against the durable Workspace registry with the existing fail-closed priority rules, verifies the registered Git identity, and returns only compact mechanical state: Project/Workspace identity, effective visibility mode, canonical Workspace root, live Git branch/HEAD, dirty-path count, and current indexed-file count. It does not expose source text, search results, Tasks, Knowledge, or MCP objects.

The current internal IPC protocol is independent of MCP wire objects, uses protocol version `1`, allows exactly one request/response per connection, enforces a 16 KiB message limit, uses bounded client/server timeouts, and rejects unknown fields/methods fail-closed. Adding `workspace_status` is an additive protocol-v1 method; the original `status` wire shape remains unchanged. This slice still does not implement daemon autostart/service management, expose MCP, or claim a verified Windows named-pipe/equivalent transport; Windows daemon IPC and canonical Windows transport paths therefore remain unsupported until that separate platform task is proven.

Development uses `uv 0.12.5`. Install/sync exactly from the committed lock and run the repository quality gate with:

```text
uv sync --locked --all-groups
uv run --frozen python scripts/quality.py
```

The quality gate checks lock freshness, formatting, lint, strict typing, pytest, and a wheel smoke test that installs the built artifact into an isolated environment, executes both shipping console scripts, verifies that installed CLI help exposes the implemented commands/runtime-path override contract, and runs the installed `harness doctor` command.

## License

No license has been selected yet. Do not assume redistribution or contribution terms until the repository owner chooses one.
