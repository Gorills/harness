# ADR-0047: Classify exact references with current-source symbol providers before a persistent graph

- **Status:** Accepted
- **Date:** 2026-09-03
- **Deciders:** Repository architecture baseline

> **Extended by ADR-0048 (2026-09-03):** the provider boundary now includes a locked offline
> ast-grep provider for JS/JSX, TS/TSX, Go, Rust, and Java; Python remains stdlib AST.

## Context

ADR-0046 made explicit identifier/literal search current-worktree and exhaustive when bounded
`exact_coverage` reports `complete=true` and `locations_truncated=false`. That removes the need to
repeat the same needle with native `rg`/`grep`, but an agent can still spend follow-up reads answering
which occurrence is a definition, which production function calls it, which tests exercise it, or
whether an occurrence is only an import.

The long-term Search-v2 plan still needs parser-backed code units and relationship navigation across
languages. The obvious broad parser dependency is not yet an acceptable runtime foundation: current
`tree-sitter-language-pack` releases provide hundreds of precompiled grammars but fetch parser bundles
on first use and cache them afterwards. Harness is local-first and must not silently turn a clean
runtime's first symbol search into a network operation. Shipping a weak regex classifier under a
"symbol" label would be worse: its false definitions/callers would look more authoritative than exact
text locations.

Python is a useful first precise provider because Harness already requires CPython 3.13 and the stdlib
`ast` parser is available offline with no packaging or daemon-download lifecycle. The initial provider
can therefore prove the model-facing relation contract and agent value while keeping unsupported
languages honest until their parsers can be distributed offline.

## Decision

### 1. `project_search` may return `symbol_navigation` beside `exact_coverage`

Identifier-shaped exact queries in `scope=code` or `scope=all` may return a bounded
`symbol_navigation` object. Quoted literals and docs-only search never claim symbol semantics.

`exact_coverage` remains the exhaustive text source of truth. `symbol_navigation` is a syntax
classification layer over current source; it does not replace the exact occurrence counts and does
not imply cross-language semantic resolution.

The response exposes:

- the exact identifier needle;
- `precise_languages` (initially only `python`);
- counts of parser-candidate, successfully parsed, failed, skipped, and unsupported matching files;
- aggregate definition/call/test-call/import/inheritance counts even when relation disclosure is
  truncated;
- `precise_classification_complete`, which is true only when the overall exact source scan completed
  and every matching precise-provider candidate was parsed successfully;
- bounded relation entries carrying path/line/column, enclosing scope, target, optional definition
  kind, test-path classification, and current-source evidence;
- independent relation/evidence truncation flags.

Unsupported matching code remains visible through `exact_coverage`; Harness does not classify it with
regex guesses.

### 2. Syntax classification reuses the exact current-source pass

The daemon does not reread files or introduce a second symbol freshness state for this slice.
`search_exact_source_inspection` performs the existing current-source literal scan and feeds the
already verified UTF-8 text to a precise provider when relevant. The Search-v2 before/after worktree
and index-revision validation from ADR-0046 therefore covers both exact locations and syntax
relations. If source changes during retrieval the whole search retries rather than returning mixed
relations.

For a simple identifier, a Python file becomes a parser candidate only when its current text contains
that identifier. For a dotted identifier such as `Client.fetch`, Python candidates are broadened to
files containing the leaf `fetch`; this allows `class Client: def fetch(...)` to be returned as a
qualified definition even when the literal `Client.fetch` occurs only in a different caller file (or
not at all). AST matching then requires the precise simple/qualified symbol shape; the broader leaf
candidate is not itself a relation.

### 3. The first precise provider is bounded Python AST

`src/harness/symbol_navigation.py` owns the provider-side syntax projection. The Python provider uses
stdlib `ast` for `.py`/`.pyi` current source and initially classifies:

- class/function/method/module-or-class-variable definitions;
- call expressions whose simple or dotted target matches the needle;
- imports, including aliases;
- class inheritance references.

Relations preserve the nearest lexical class/function/method scope and mark conventional test paths.
Definition evidence contains the beginning of the code unit; call/import/inheritance evidence is a
small line window around the relation. Evidence is current source, not a stored parser body.

Parser CPU is bounded independently of exact text search: a Python provider candidate larger than
1 MiB is not parsed and increments `parse_skipped_files`. Syntax errors or parser failures increment
`parse_failures`. Neither case falls back to a weaker classifier, and both prevent
`precise_classification_complete=true`.

This is syntactic navigation, not Python type inference. `obj.fetch()` is a call to the syntactic
leaf `fetch`; Harness does not claim which runtime class owns `obj`. Qualified queries require a
compatible dotted syntax/qualified definition rather than inventing dynamic dispatch resolution.

### 4. Symbol navigation has its own compact budget inside the existing search envelope

At most 16 relation entries are model-visible and the encoded `symbol_navigation` object is capped at
5 KiB. When needed, lower-priority relation evidence is removed before relation entries themselves;
aggregate counts remain intact and truncation is explicit.

Its actual encoded size reserves response bytes together with `exact_coverage` before file-level rich
evidence is fitted. The overall model-facing `project_search` hard limit therefore remains 12 KiB in
this slice. For explicit symbol work, current definition/caller/test evidence is more useful than a
lower-ranked whole-file lexical window, so it is allowed to displace that evidence.

### 5. Broad Tree-sitter/code-unit indexing requires an offline parser distribution decision

This ADR does not add a persistent symbol table, SCIP, Tree-sitter, type inference, or embeddings.
The provider contract is intentionally compatible with a future Tree-sitter-backed implementation,
but Harness will not depend on on-demand grammar downloads during normal search. A later packaging
change must prove how the supported grammar set is installed, versioned, upgraded, and available
offline before it becomes a production provider.

Once broad parsers are available offline, persistent code-unit/edge indexes may be added for natural
queries that have no exact identifier seed and for larger relationship graphs. Those indexes remain
rebuildable derived state and must preserve ADR-0046 current-worktree validation. Semantic retrieval
still follows the structural layer rather than replacing exact search.

## Consequences

- A single Python identifier search can now return its definition, production callers, test callers,
  imports, inheritance references, and bounded source evidence without a second native search/read
  loop for the common case.
- Qualified Python definitions can be discovered even when their dotted spelling is absent at the
  definition site.
- Dirty syntactically invalid Python still has exact text coverage; Harness explicitly reports that
  precise classification failed instead of fabricating relations.
- Other languages keep exhaustive current exact references but do not yet receive precise relation
  labels. This is a visible capability boundary, not a hidden quality downgrade.
- No schema migration or second derived freshness model is introduced for the first relation slice.
- Python AST parsing adds bounded CPU only for current files that are plausible relation candidates.
- The 12 KiB search response remains a hard envelope; symbol relation counts survive disclosure
  truncation.

## Verification

Automated coverage must prove:

- current Python definitions, production callers, test callers, imports, and relation evidence are
  returned for a simple identifier;
- a qualified definition can be returned from a different file that does not contain the dotted
  literal while exact occurrence counts remain literal-only;
- import aliases and inheritance relations are classified without duplicate weak guesses;
- dirty syntax errors retain complete exact text coverage but set parser failure and precise
  completeness correctly;
- matching unsupported-language code is counted and receives no regex relation classification;
- oversized Python parser candidates are skipped under the 1 MiB parser bound without weakening exact
  source coverage;
- quoted literals and docs-only queries do not claim symbol navigation;
- aggregate relation counts survive relation/evidence truncation and the combined
  `exact_coverage` + `symbol_navigation` + ranked hits response remains within 12 KiB;
- strict daemon IPC and real MCP expose the exact symbol-navigation contract;
- repository formatting, lint, strict typing, tests, and exact-head CI remain green before merge.
