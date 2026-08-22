# ADR-0010: Start search with deterministic bounded indexed-path retrieval

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

The audited implementation sequence places search immediately after deterministic Structural Index reconciliation. The current index stores Workspace-scoped path, entry kind, size, and content hash, but does not yet store lexical source text, symbols, docs, Tasks, or Knowledge.

The full v1 search architecture combines exact matching, normalized identifiers, FTS5, structure, documentation, Task history, and fresh semantic Knowledge. Implementing all of those channels in one change would cross several later bounded milestones and would require new ingestion/storage contracts before they are independently proven.

A useful first search slice can already narrow a repository using only the current disposable Structural Index. It must remain local, deterministic, Workspace-scoped, bounded, and must not expose source text or internal hashes.

## Decision

Add a domain-level indexed-path search contract over the current `indexed_files` rows.

The contract:

1. requires an existing Harness Workspace;
2. accepts one non-empty query bounded to 256 UTF-8 bytes;
3. accepts a result limit from 1 through 50, defaulting to 10;
4. searches only the selected Workspace's current Structural Index;
5. ranks matches deterministically in this order:
   - exact relative path;
   - exact filename;
   - all normalized identifier tokens present in the path;
   - case-insensitive path substring fallback;
6. splits common camelCase, PascalCase, snake_case, kebab-case, dotted, and path-separated ASCII identifiers into case-folded tokens;
7. returns only relative path, entry kind, size, and the mechanical match reason;
8. never returns raw source, content hashes, registry internals, Task state, Knowledge, or unrelated Workspace data.

The domain primitive is exposed through the daemon only after its contract is independently verified. Internal protocol v1 gains an additive `workspace_search` method carrying ordered Workspace hints, the bounded query, and the bounded result limit. The daemon resolves the Workspace using the existing fail-closed resolver, verifies the registered Git identity around the read, executes the domain search against one consistent index snapshot, and returns only Project/Workspace identity plus the mechanical hits.

The installed CLI exposes that daemon path as:

```text
harness search QUERY [PATH] [--limit N] [--socket PATH]
```

`PATH` defaults to the current directory and is a location inside an already registered Workspace. With no explicit socket override, the command uses the same canonical POSIX lazy-autostart behavior as `status` and `scan`. Search is read-only: it does not register, rescan, read source content, or mutate durable state. Result paths are escaped before terminal rendering so control characters in repository filenames cannot inject extra output lines.

This still does not add MCP `project_search`. The eventual model-facing contract has a richer bounded result schema (`ref`, title/location/summary/freshness and code-specific metadata) and belongs to the separately audited MCP bridge slice after Task and Knowledge domain state exists.

## Consequences

### Positive

- Search is independently testable without coupling the first retrieval contract to MCP.
- Legacy repositories gain deterministic identifier/path narrowing from data Harness already indexes.
- The result is naturally bounded and has a small negative-disclosure surface.
- CLI users can exercise real daemon-owned retrieval on an installed Harness without direct database access.
- No schema migration, content ingestion, parser dependency, embedding provider, or cloud service is introduced.

### Costs and limits

- This is not the full v1 search implementation and does not claim lexical source/content search.
- Search reflects the current persisted Structural Index snapshot; without the future watcher, callers use `harness scan` to reconcile filesystem changes before relying on fresh results.
- FTS5, symbols/imports/exports, docs, Git metadata, structural proximity, Tasks, Knowledge, Working Sets, and rank fusion remain later bounded search channels.
- Identifier token splitting is deliberately conservative and ASCII-oriented in this slice; exact and substring path matching still work for other Unicode path text.
- The implementation currently scans the selected Workspace's indexed rows in process. A later FTS/schema slice may replace this internal mechanism without changing the bounded domain result contract.
- Internal IPC remains capped at 16 KiB. A requested result set that cannot fit returns a bounded `response_too_large` error instead of silently emitting a partial wire payload.
- Invalid or corrupted persisted Structural Index rows return bounded `index_error` responses; the daemon remains available for subsequent requests rather than allowing the index-domain exception to escape its client handler.

## Verification

Automated tests must prove:

- exact path and exact filename precedence;
- camel/snake/natural identifier-token matching;
- deterministic substring fallback and ordering;
- hard query and result-count bounds;
- Workspace scoping through the existing registry/index contract;
- exact IPC request/response shape and malformed-request recovery;
- serialized IPC response-size enforcement;
- corrupted-index error containment and daemon recovery;
- CLI Workspace-hint, limit, canonical-autostart, and explicit-socket behavior;
- terminal path escaping for control characters;
- result negative disclosure: no source content or internal content hash fields.
