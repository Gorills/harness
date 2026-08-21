# Harness

Harness is a local-first control plane for coding agents. It preserves project intelligence across agent sessions and hosts while keeping source editing, shell access, Git, and browsing in the native host.

> **Repository status:** architecture baseline. Product implementation has not started yet.

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

## Development state

This baseline intentionally contains no production package scaffold yet. The next bounded implementation task should establish the Python package, reproducible dependency lock, formatting/lint/type/test configuration, and CI together so the repository never claims reproducibility before it is actually verifiable.

## License

No license has been selected yet. Do not assume redistribution or contribution terms until the repository owner chooses one.
