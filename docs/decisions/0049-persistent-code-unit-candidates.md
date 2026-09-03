# ADR-0049: Persist bounded code-unit definitions as a rebuildable search candidate index

- **Status:** Accepted
- **Date:** 2026-09-03
- **Deciders:** Repository architecture baseline

## Context

ADR-0047 and ADR-0048 established precise current-source symbol navigation. Python uses stdlib AST;
JavaScript/JSX, TypeScript/TSX, Go, Rust, and Java use the locked offline `ast-grep-py` provider.
That layer deliberately parses plausible files at query time so exact identifier searches can classify
current definitions, calls, imports, and inheritance without a second freshness state.

Natural-language code search has a different problem. A query such as `rotate refresh token` should
prefer the file that *defines* `rotateRefreshToken` over an equally lexical comment or incidental
mention, even when the user did not supply an exact identifier seed. Re-parsing every supported file
for every natural query would discard the bounded Structural Index work already performed by scans.
The next Search-v2 structural slice therefore needs persistent parser-derived candidates, but adding a
whole call graph or claiming type-aware dispatch at the same time would be too large a semantic jump.

## Decision

### 1. Schema v18 adds a rebuildable definition-only code-unit index

Each Structural Index reconciliation derives `indexed_code_unit_files` and `indexed_code_units` for
regular source paths supported by the existing precise providers. A manifest records the authoritative
`content_sha256`, parser language, and one bounded status. Successful manifests contain named
definitions with only:

- local name and qualified name;
- definition kind;
- 1-based line and column;
- Workspace-relative path and deterministic position.

No source body, signature body, docstring, call edge, inferred type, receiver type, or reachability
claim is persisted. The tables are derived state with foreign-key cascades from `indexed_files` and
may be rebuilt from the Workspace at any time.

### 2. Reconciliation shares the existing Structural Index transaction and freshness boundary

Code units are reconciled inside the same transaction as `indexed_files`, code/docs lexical search,
Knowledge staleness, and index provenance. The parser receives the same stable UTF-8 source reread and
must match the current indexed SHA. Incremental scans replace units only for changed selected paths;
deletions cascade their manifests, units, and search rows.

There is no independent code-unit revision or parser freshness token. Search-v2 currentness already
forces the Structural Index to match the live worktree before retrieval. A code-unit manifest is valid
only for its stored content SHA and provider language.

### 3. Parser failures are persisted as bounded negative manifests

The existing 1 MiB precise-parser bound remains unchanged. Unsupported languages receive no manifest.
Supported files persist one of:

- `ok`;
- `parse_error`;
- `too_large`;
- `non_text`;
- `unit_limit`.

A non-`ok` manifest contains no units. Because the status is keyed to the same content SHA, an
unchanged malformed or oversized file is not reparsed on every full scan. Changing the file SHA makes
the manifest stale and causes normal re-analysis. No regex fallback is introduced.

A single file may persist at most 4096 definitions. Local names are capped at 512 UTF-8 bytes and
qualified names at 1024 UTF-8 bytes. Exceeding any unit bound fails closed to `unit_limit` rather than
persisting a partial structural picture.

### 4. Natural code search may use definitions as a candidate/ranking channel

`indexed_code_unit_search` is a contentless FTS5 projection over definition name, qualified name,
normalized identifier tokens, and symbol kind. It cannot return source text. For `scope=code`,
`project_search` may fuse matching definition rows into the existing per-file candidates while
retaining the normal `code:<path>` ref and live-source evidence validation.

Candidate fusion fails closed per row. If a persisted code-unit manifest SHA no longer matches the
current `indexed_files` SHA, that structural candidate is ignored rather than failing the whole query.
If live-source validation reports `changed_since_index`, the source-derived structural summary is
removed together with evidence, leaving only safe locator metadata. Invalid persisted row types and
other impossible index state remain hard retrieval errors.

A matching code-unit definition can outrank an otherwise same-tier lexical mention. Exact path and
filename tiers remain stronger. The result explains the structural reason and may expose a bounded
summary such as `method Client.fetch`; it does not expose internal BM25 scores or parser state.

Docs, Knowledge, and Task channels do not use this index. Exact current-source coverage remains the
exhaustive truth for explicit literals/identifiers, and query-time `symbol_navigation` remains the
precise current-source relation layer for calls/imports/inheritance.

### 5. Graph edges and type-aware resolution remain later slices

This ADR does not persist callers, imports, inheritance edges, SCIP data, type inference, override
selection, or dynamic dispatch. Definition persistence proves the lifecycle, freshness, boundedness,
and ranking contract first. A later Search-v2 slice may add explicit graph edges and then separately
add type-aware evidence where a language-specific provider can prove it.

Semantic embeddings/reranking remain an additional candidate channel and must not replace exact or
structural evidence.

## Consequences

- Natural code queries can prefer files that define matching compound symbols rather than merely
  mentioning the same words.
- Full and incremental scans maintain parser-derived candidates without a second freshness protocol.
- Malformed, non-text, oversized, or definition-explosive source fails closed and is not repeatedly
  reparsed until its SHA changes.
- Stale code-unit candidates cannot fail an otherwise valid search or leak a stale symbol summary when
  live-source validation proves that the file changed.
- SQLite still does not become a source-code store; only bounded names, kinds, and locations persist.
- The schema advances from v17 to v18 and older databases migrate in place.
- Structural search gains persistent definitions, while call graphs and type-aware navigation remain
  explicit future work rather than implied capability.

## Verification

Acceptance coverage must prove:

- Python and at least one ast-grep language extract all named definitions without reference relations;
- schema-v17 databases migrate to v18 without losing durable data;
- scans persist definition manifests/units and a contentless searchable projection without source
  bodies;
- a natural compound-symbol query can rank the actual definition ahead of a lexical mention and
  explain the structural match;
- incremental source changes replace old units and FTS rows;
- parse failures remove stale units, persist a negative manifest, and are not reparsed while the SHA
  is unchanged;
- stale manifest/index SHA divergence drops only the structural candidate, while changed live source
  suppresses stale structural summary/evidence without losing the safe locator;
- deleted paths cascade all code-unit state;
- the per-file unit bound fails closed without a partial unit set;
- existing exact coverage, query-time symbol navigation, migrations, quality gates, wheel smoke, and
  exact-head CI remain green before merge.
