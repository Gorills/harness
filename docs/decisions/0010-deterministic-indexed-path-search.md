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

This slice is intentionally a domain primitive only. It does not add a public CLI command, daemon IPC method, or MCP `project_search` surface yet. Those exposure layers must be added only after the retrieval contract is independently reviewed and verified.

## Consequences

### Positive

- Search becomes independently testable without coupling the first retrieval contract to MCP or a new wire shape.
- Legacy repositories gain deterministic identifier/path narrowing from data Harness already indexes.
- The result is naturally bounded and has a small negative-disclosure surface.
- No schema migration, content ingestion, parser dependency, embedding provider, or cloud service is introduced.

### Costs and limits

- This is not the full v1 search implementation and does not claim lexical source/content search.
- FTS5, symbols/imports/exports, docs, Git metadata, structural proximity, Tasks, Knowledge, Working Sets, and rank fusion remain later bounded search channels.
- Identifier token splitting is deliberately conservative and ASCII-oriented in this slice; exact and substring path matching still work for other Unicode path text.
- The implementation currently scans the selected Workspace's indexed rows in process. A later FTS/schema slice may replace this internal mechanism without changing the bounded domain result contract.

## Verification

Automated tests must prove:

- exact path and exact filename precedence;
- camel/snake/natural identifier-token matching;
- deterministic substring fallback and ordering;
- hard query and result-count bounds;
- Workspace scoping through the existing registry/index contract;
- result negative disclosure: no source content or internal content hash fields.
