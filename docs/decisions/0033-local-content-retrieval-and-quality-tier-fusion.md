# ADR-0033: Index bounded local text and fuse search by explicit quality tiers

- **Status:** Accepted
- **Date:** 2026-08-30
- **Deciders:** Repository architecture baseline

> **Amended (2026-09-01):** `project_search` may attach bounded **current-source** evidence on
> code/doc hits. Contentless FTS (`content=''`) remains not a source store: Harness must not use
> SQLite `snippet()`/`highlight()` as if bodies were stored. Evidence is produced only by mapping
> the FTS candidate to `indexed_search_documents` / `indexed_files` SHA, safely rereading the live
> Workspace file with the same containment/regular-file/symlink/size/UTF-8 invariants as indexing,
> requiring the live SHA to equal the indexed SHA, relocating significant query terms, and returning
> a bounded line window. SHA mismatch yields `evidence=null` and `evidence_reason=changed_since_index`
> while keeping the locator. An unsafe or unrelocatable match uses `current_match_not_relocated`.
> When the global 12 KiB `project_search` payload is full, already-built evidence is dropped with
> `evidence=null` and `evidence_reason=response_budget` rather than pretending relocate failed.
> Path-only hits without a content document use `path_only`. `project_context` for code/doc refs
> stays metadata-only.

## Context

ADR-0021 made Knowledge and Task history searchable through rebuildable FTS5 indexes, but kept the
code/docs channels on the earlier indexed-path primitive. Consequently a natural query succeeded
only when all of its words happened to occur in a relative path. On the indexed Harness checkout,
queries such as `where project search happens`, `knowledge relevance`, and `retrieval ranking`
returned no results even though the repository contained directly relevant implementation and
documentation. `scope=all` also interleaved channel positions without distinguishing an exact fresh
Knowledge title from a weak one-token match.

The full specification already requires local lexical code/docs retrieval, normalized identifiers,
fresh Knowledge, explainable ranking, and deterministic fusion. It does not permit bulk source
disclosure, automatic LLM analysis, or a derived index becoming source of truth.

## Decision

Schema v14 adds a Workspace-scoped `indexed_search_documents` mapping and a contentless FTS5 table
for bounded code/document text. During the existing authoritative scan, Harness reads regular files
of at most 1 MiB a second time under the same stable-entry and Workspace-containment checks, verifies
their SHA-256 against the mechanical snapshot, and indexes valid UTF-8 text. Symlinks, binary/NUL
content, invalid UTF-8, oversized files, ignored paths, and excluded sensitive path patterns remain
path-searchable where applicable but do not enter content FTS. Generated diagnostic text with
`.log`/`.out` suffixes remains in the mechanical inventory but is excluded from scoped code/docs
retrieval rather than being treated as source.

The durable mapping stores only path/corpus/hash plus title and compound-identifier tokens. The FTS
table uses `content=''` and `contentless_delete=1`; source bodies are supplied for tokenization but
are not readable back as a SQLite text column. FTS terms remain private local derived data and are
never returned by IPC/MCP. Selected code/doc results are still reconstructed from authoritative
`indexed_files`. `project_search` may additionally attach a bounded live-file evidence window as
amended above; `project_context` remains metadata-only. Content rows are updated/deleted in the
same transaction as the Structural Index. A v13→v14 migration creates an empty derived content index;
the daemon watcher's initial authoritative reconciliation or an explicit scan backfills it from the
live Workspace rather than pretending stale stored hashes contain source text.

Natural query analysis is shared across retrieval channels. It:

- splits Unicode words and common camelCase/PascalCase/snake_case boundaries;
- removes a small deterministic English/Russian conversational stop-word set, falling back to the
  original terms if every term is filler;
- uses bounded FTS prefix alternatives, including conservative English/Russian inflection stems;
- caps the analyzed term count and preserves the existing UTF-8 query and result limits.

Per-channel ranking uses explicit categorical evidence instead of a manually tuned score matrix:
exact path, exact filename, exact extension-free filename stem, title/normalized-identifier phrase,
all significant terms, then partial match. Code/docs candidates must cover every significant query
term across the indexed title, path, normalized identifiers, and lexical body; normalized
identifier/path tokens never create a partial multi-term hit from one common token. BM25 orders
candidates only inside one channel/quality tier. Archived paths receive a same-tier penalty unless
the query explicitly asks for an archive, so current canonical documents win equivalent matches.
Fresh Knowledge keeps its freshness advantage; `needs_revalidation` remains historical evidence and
is penalized after fresh cards. Test-file paths receive only a same-tier tie-break penalty unless the
query explicitly asks for tests. Current-Task relevance is a tie-break boost, not a hard override of
a more complete lexical match. `scope=all` compares the shared quality/coverage tiers, then
deterministically interleaves uncalibrated per-channel ranks. A directly matching fresh Knowledge
card can therefore precede a general code/doc hit, while an exact path still wins globally.

MCP instructions also state the Knowledge quality rule: checkpoint only verified reusable findings,
prefer precise anchors, and do not summarize files broadly or persist speculation. Knowledge remains
agent-authored through real Task work; Harness does not fabricate cards from indexed source.

## Consequences

- Natural code/docs queries no longer depend on filenames containing every query word.
- Common compound identifiers and English/Russian inflections are discoverable without embeddings
  or an external service.
- Search reasons describe the evidence tier without exposing BM25 values or internal token data.
- The index adds bounded local scan I/O and FTS storage. Files larger than 1 MiB and non-UTF-8 source
  degrade to mechanical path retrieval rather than failing the Workspace scan.
- SQLite must support both FTS5 and contentless-delete tables; the runtime capability probe now checks
  the exact required feature set.
- This does not claim semantic translation, exact language-parser symbols, structural graphs, Working
  Sets, or embeddings. Those remain separate bounded changes.

## Verification

Automated coverage must prove:

- v13→v14 migration and rollback are transactional and do not fabricate content from hashes;
- authoritative scan insert/update/delete and missing-derived-row repair keep content FTS synchronized;
- binary/NUL and oversized files do not enter content FTS;
- content FTS has no readable source-body column; code/doc `project_context` remains metadata-only;
  `project_search` evidence, when present, comes from a current-file reread rather than FTS snippet;
- generated `.log`/`.out` artifacts remain mechanically indexed but do not become code/docs hits;
- natural filler queries, camel/snake identifiers, prefix inflections, and Russian inflections find
  relevant code/docs whose paths do not contain all query terms;
- a multi-term garbage query sharing only one common identifier token returns no code/docs hits;
- exact path/filename precedence remains compatible with the mechanical search contract;
- an exact filename stem and a current canonical path outrank equivalent archived document matches;
- directly matching fresh Knowledge outranks general lexical results, while stale Knowledge remains
  explicitly historical and follows fresh Knowledge;
- Workspace/Project isolation, item/byte budgets, and cross-Project negative disclosure remain green
  through domain, IPC, and real stdio MCP tests.
