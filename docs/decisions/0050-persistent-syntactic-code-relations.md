# ADR-0050: Persist bounded unresolved syntactic code relations for Search v2

- **Status:** Accepted
- **Date:** 2026-09-03
- **Deciders:** Repository architecture baseline

## Context

ADR-0047 and ADR-0048 established current-source syntax navigation for exact identifier searches.
ADR-0049 then proved the persistent parser-derived lifecycle with schema-v18 definition-only code
units: bounded names/kinds/locations are rebuilt during Structural Index reconciliation and natural
code queries may use those definitions as an explainable candidate channel.

The next Search-v2 gap is relationship discovery when the user does not already have an exact symbol
seed. A query such as `who calls rotate refresh token` should be able to prefer a parser-proven caller
without reparsing every supported file during the query. Persisting a resolved call graph at the same
time would overclaim: the current providers prove syntax targets and lexical scopes, but do not prove
runtime receiver types, import binding resolution, override selection, or dynamic dispatch.

## Decision

### 1. Schema v19 adds a rebuildable syntactic-relation projection

`indexed_code_unit_files` gains `relation_status`. Successful parser manifests may persist
`indexed_code_relations` rows containing only:

- relation kind: `call`, `import`, or `inheritance`;
- source Workspace-relative path;
- bounded lexical source scope when available;
- the provider-proven target spelling;
- 1-based line and column;
- conventional test-path classification.

The rows are derived state under the existing per-file code-unit manifest and therefore inherit the
same source SHA, provider language, transaction, incremental/full reconciliation, and cascade-delete
boundary. A contentless FTS5 projection indexes target/scope/relationship terms for candidate lookup.
It is not a source-code store.

### 2. Persist syntax edges, not resolved program-graph edges

An edge means only that the parser proved the corresponding syntax in that source file. Examples:

- `rotateRefreshToken()` -> syntactic call target `rotateRefreshToken`;
- `client.fetch()` -> syntactic call target `client.fetch` when the grammar proves that member shape;
- `import { helper as h } ...` -> import syntax for the imported target while a later `h()` call stays
  a syntactic call to `h`;
- `class Child(Base)` / `extends Base` -> inheritance syntax targeting `Base`.

Harness does **not** join those spellings into a resolved callee/import/type graph in this slice. It
does not infer receiver types, resolve module exports, select overrides, claim reachability, or claim
that two equal target strings identify the same runtime symbol. Type-aware and binding-aware evidence
remain a separate later layer.

### 3. Scan-time extraction reuses the precise offline providers

The existing Python stdlib AST and locked offline `ast-grep-py` providers gain an all-structure mode
that emits definitions plus every supported relation. Exact-search `symbol_navigation` keeps its
existing query-targeted current-source behavior, and the definition-only helper keeps its existing
contract.

Structural Index reconciliation parses a changed eligible file once, splits definitions from
relations, and writes both projections in the same transaction. Existing schema-v18 manifests migrate
with `relation_status=unindexed`; the next normal reconciliation rebuilds their relation projection
without introducing an independent freshness token.

### 4. Relation overflow fails closed independently of definitions

A file may persist at most 8192 syntactic relations. Persisted target and lexical-scope strings are
capped at 1024 UTF-8 bytes each. If any relation bound is exceeded, the file records
`relation_status=relation_limit` and persists **no relation rows**, while otherwise-valid definition
rows remain available. Unchanged `relation_limit` state is cached under the same source SHA so the
file is not repeatedly reparsed.

Parser failure, non-text source, oversized source, or definition `unit_limit` keeps
`relation_status=unindexed` and persists no relations. No partial relation set and no regex fallback is
allowed.

### 5. Natural code search may use relations as an explainable candidate channel

For `scope=code`, natural queries may fuse persisted relation candidates with path, lexical content,
and definition candidates. Two modes are supported:

- generic target matching, such as `rotate refresh token`, where a syntax relation is allowed to
  outrank an incidental same-tier lexical mention but remains weaker than a matching definition;
- explicit relation intent, such as `who calls rotate refresh token`, `imports session token`, or
  `inherits base client`, where bounded intent terms select the matching relation kind and the
  remaining terms search the target projection.

The result keeps the normal `code:<path>` ref and may expose a compact structural summary such as
`call rotateRefreshToken in issueSession`. It does not expose internal FTS scores or imply target
resolution.

### 6. Search freshness and stale-evidence rules remain unchanged

Candidate fusion requires an `ok` definition manifest, `relation_status=ok`, and matching manifest /
`indexed_files` source SHA. A stale relation candidate is ignored rather than failing the whole query.
The existing live-source evidence pass clears source-derived structural summaries together with
evidence when it observes `changed_since_index`.

Search-v2 current-worktree reconciliation therefore remains the authority; v19 does not create a
second relation revision or parser cache.

## Consequences

- Natural relationship queries can find parser-proven callers/importers/inheritance references without
  requiring an exact identifier seed or reparsing the whole Workspace at query time.
- The persistent structural layer now contains definitions and unresolved syntax edges, while staying
  rebuildable and source-body-free.
- Relation explosion cannot create a partial graph or discard otherwise-valid definition candidates.
- Equal target spellings remain syntax evidence only; callers must not interpret them as resolved
  runtime identity.
- The schema advances from v18 to v19. Existing databases migrate in place and lazily rebuild relation
  projections through normal Structural Index reconciliation.
- Binding-aware/type-aware resolution and semantic embeddings/reranking remain later bounded Search-v2
  slices.

## Verification

Acceptance coverage must prove:

- Python and at least one ast-grep language emit definitions plus supported calls/imports/inheritance
  without resolving aliases or receiver types;
- scans persist bounded relation rows and a contentless searchable projection without source bodies;
- an explicit caller-intent natural query prefers the parser-proven caller over a lexical question or
  comment containing the same words;
- incremental changes replace old relation rows/FTS entries and parser failure removes stale rows;
- relation overflow records `relation_limit`, persists zero relations, keeps valid definitions, and is
  cached while the source SHA is unchanged;
- schema-v18 databases migrate to v19 without losing existing durable state;
- stale manifest/index divergence drops only the structural relation candidate and existing
  `changed_since_index` behavior suppresses stale structural summaries/evidence;
- current exact coverage, query-time symbol navigation, migrations, quality gates, benchmark counter
  gates, wheel smoke, and exact-head CI remain green before merge.
