# ADR-0048: Extend current-source symbol navigation with a locked offline polyglot parser

- **Status:** Accepted
- **Date:** 2026-09-03
- **Deciders:** Repository architecture baseline

## Context

ADR-0047 established the `symbol_navigation` contract with Python stdlib `ast`. That proved the
current-source, bounded, fail-closed relation model, but left every non-Python exact identifier as
unclassified text. For JavaScript/TypeScript, Go, Rust, and Java work this still gives an agent a
reason to open several exact hits merely to distinguish definitions from callers and tests.

A broad parser dependency is useful only if normal Harness search remains local-first. The evaluated
`tree-sitter-language-pack` distribution is intentionally small because grammars are fetched and
cached on first use; prefetching moves that network operation earlier rather than removing it. That
is not an acceptable hidden prerequisite for repository search.

`ast-grep-py` takes the opposite packaging approach for its built-in languages: the Python extension
links the project's built-in `SupportLang` parsers into platform wheels. Harness verified version
`0.45.1` on the supported Python 3.13/Linux CI baseline by installing from the locked dependency and
successfully parsing JavaScript, TypeScript, TSX, Go, Rust, and Java inside a Linux network namespace
with no network interface available. The same probe produced the exact project lock and offline uv
cache used for local verification.

Parser availability alone is not enough to claim precise navigation. Every language needs explicit,
adversarially tested node-shape mappings so a generic parser does not become a generic false-positive
generator.

## Decision

### 1. Keep a provider boundary and retain Python stdlib AST

`src/harness/symbol_navigation.py` continues to dispatch by current file path. Python `.py`/`.pyi`
uses stdlib `ast`; this remains the most language-native provider and avoids changing established
Python relation semantics.

Harness adds locked runtime dependency `ast-grep-py==0.45.1` for the first polyglot provider. The
initial model-facing precise set is deliberately narrower than the parser bundle:

- JavaScript/JSX (`.js`, `.jsx`, `.mjs`, `.cjs`);
- TypeScript (`.ts`, `.mts`, `.cts`);
- TSX (`.tsx`);
- Go (`.go`);
- Rust (`.rs`);
- Java (`.java`).

Other languages bundled by ast-grep remain `matching_unsupported_files` until Harness has explicit
relation mappings and adversarial tests for them. Physical parser presence is not product support.

### 2. Classify syntax relations from current source, not a persistent parser cache

The polyglot provider receives the same SHA-confirmed UTF-8 text already read by
`search_exact_source_inspection`. It parses only plausible identifier candidates, keeps the existing
1 MiB per-file parser bound, and introduces no symbol database, parser cache, or second freshness
state.

For the supported languages the provider classifies language-appropriate subsets of the existing
relation vocabulary:

- named type/function/method/constructor/variable definitions where the grammar gives a stable name;
- call expressions, including simple qualified/member forms;
- JavaScript/TypeScript, Rust, Java, and useful package-level imports where the syntax exposes the
  queried identifier;
- JavaScript/TypeScript and Java inheritance plus Rust trait implementations.

Go receiver methods are qualified by receiver type. Rust trait/impl methods and Java/JS/TS methods
preserve enclosing type scope. JavaScript member calls such as `new Client().fetch()` are normalized
to `Client.fetch` when the syntax tree proves that receiver shape. Complex/dynamic expressions that
cannot be represented safely are left unclassified; their exact text occurrence remains available.

The provider is syntactic, not type-resolved. `obj.fetch()` can establish a call to leaf `fetch` but
cannot prove the runtime type of `obj`. No relation entry claims dispatch resolution, override
selection, or whole-program reachability.

### 3. Parser errors fail closed and completeness now covers unsupported matches

An ast-grep tree containing an `ERROR` node is a parser failure for navigation. Harness returns no
weak regex fallback. Oversized candidates remain explicit parser skips.

`precise_classification_complete` is tightened: it is true only when the current-source scan is
complete, every precise-provider candidate parsed successfully, there were no parser skips/failures,
**and no exact-matching code file lacked a precise provider**. This makes the flag useful to an agent
as an actual relation-completeness signal rather than a statement about an internal subset.

`precise_languages` contains the sorted set of providers actually attempted for the query. Mixed
repositories can therefore expose, for example, `go` plus `typescript` while separately reporting
an unsupported `.vue` exact match.

### 4. Offline packaging is a required product invariant

The committed uv lock pins `ast-grep-py==0.45.1` and its platform artifacts. CI installs the normal
locked environment; there is no first-search parser download path, grammar registry, or mutable
runtime parser cache in Harness.

A future ast-grep upgrade must keep a no-network parser smoke in acceptance evidence for the precise
language set. Adding another language requires relation tests and path mapping, not merely adding its
name to `precise_languages`.

### 5. Existing response and source bounds remain unchanged

This slice does not increase the 64 MiB exact-source scan bound, 1 MiB parser-candidate bound, 16
visible relation limit, 5 KiB symbol-navigation budget, or 12 KiB total `project_search` response.
Aggregate relation counts and explicit truncation continue to survive model-facing compaction.

No schema migration is required because relations are produced from current source per search.
Persistent code-unit indexes, SCIP-style edges, semantic retrieval, and reranking remain later Search
v2 layers.

## Consequences

- Exact identifier searches in six additional language modes can directly identify definitions,
  callers, tests, selected imports, and inheritance without a native `rg` classification loop.
- The parser runtime remains deterministic after installation and does not need a network connection
  when an agent performs search.
- Python behavior stays on stdlib AST instead of being silently changed by the polyglot provider.
- Unsupported files remain visible exact evidence and now prevent a misleading completeness claim.
- The runtime dependency grows by one compiled wheel, so lock/platform compatibility becomes part of
  the normal installation and CI contract.
- Syntax navigation still does not replace future type-aware/persistent relation graphs.

## Verification

Automated/acceptance coverage must prove:

- the locked Python 3.13 environment installs `ast-grep-py==0.45.1` from a wheel and the selected
  parsers operate with network access removed;
- TypeScript definitions, production/test calls, and imports are classified;
- JavaScript method definitions/member calls and inheritance are classified;
- TSX definitions/calls are classified;
- Go receiver methods are qualified by receiver type and calls are classified;
- Rust trait/impl method definitions, calls, simple/aliased/grouped `use` imports, and trait
  implementation relations are classified without claiming dynamic resolution;
- Java method definitions/calls, imports, inheritance, and class-field definitions are classified
  without promoting method/lambda locals to class scope;
- qualified member-call tests prove that receiver runtime types are never inferred from variable names;
- malformed polyglot source keeps exact coverage while precise parsing fails closed;
- mixed supported/unsupported exact matches set `precise_classification_complete=false`;
- strict IPC/MCP, relation budgets, repository quality gates, and exact-head CI remain green.
