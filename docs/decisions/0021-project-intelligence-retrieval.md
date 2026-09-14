# ADR-0021: Keep Project Intelligence retrieval daemon-owned and FTS-derived

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Repository architecture baseline

> **Amended by ADR-0033 (2026-08-30):** code/docs candidate retrieval now includes bounded local
> contentless FTS in addition to path signals, and `scope=all` first compares shared explicit
> quality/coverage tiers before deterministic per-channel rank interleaving. The daemon ownership,
> authoritative reread, freshness, bounds, and negative-disclosure decisions below remain intact.

## Context

The model-facing `project_search` contract already defines `all`, `code`, `docs`, `knowledge`, and `tasks` scopes, while the implementation only returned current Structural Index path hits. Durable provenance-bearing Knowledge and Task history already exist, but exposing them directly from the MCP bridge would duplicate Project scoping, SQL, freshness semantics, and response-budget policy outside the daemon. A naive `LIKE` scan over growing historical tables would also make every semantic query proportional to all stored history.

The retrieval boundary must preserve Harness invariants: one resolved Workspace establishes the active Project; current filesystem/index data remains Workspace-local; durable Knowledge/Task history is Project-scoped; stale Knowledge is historical evidence rather than a current fact; source code is still read with native host tools; and derived indexes cannot become a second source of truth.

## Decision

Add schema v11 rebuildable FTS5 candidate indexes for Knowledge title/body and Task semantic fragments. Task fragments include Task titles, checkpoint summary/next-step text, and operator feedback. The migration backfills existing durable rows and installs triggers that keep the derived indexes synchronized with later authoritative writes and deletions.

Implement Project Intelligence search/context in a dedicated retrieval domain layer owned by the daemon. Every model-facing search/context request resolves one registered Workspace, validates the registered live Git identity, opens one read transaction, fixes the owning Project, performs the bounded retrieval, then validates Git identity again. Current code/docs search only the resolved Workspace. Knowledge and Task-history candidates are filtered by exact Project identity.

FTS5 supplies candidates and lexical rank only. Search results and all `project_context` expansion reread authoritative `indexed_files`, Knowledge cards/anchors, Tasks, checkpoints, and events before constructing model-facing data. A stale Knowledge card is retained, explicitly labelled `needs_revalidation`, and sorted after fresh cards. Task search may return a stable fragment ref identifying the matched checkpoint or operator-feedback event. `project_context` accepts only explicit unique bounded refs and fails closed when a ref is missing, malformed, no longer current, or belongs to another Project.

Preserve the existing model-facing code context shape. `code:` and new `doc:` context items expose path metadata only (`title`, `location`, `path`, entry kind, size, freshness); they never expose source text or stored content hashes. Knowledge context may expose the selected durable semantic body/provenance/anchors. Task context exposes only the selected Task or bounded selected/recent durable history. Large semantic fields and repeated metadata are deterministically compacted with explicit truncation/count markers before IPC serialization, so a valid selected semantic ref does not accidentally become an unbounded wire payload. The five-tool MCP surface remains unchanged.

For `scope=all`, combine bounded per-channel rankings with deterministic interleaving rather than pretending heterogeneous path BM25/freshness/current-Task signals share one calibrated global score. More sophisticated RRF/Working-Set/graph ranking can replace this internal fusion later without changing refs or tool shapes.

## Task recall amendment (2026-09-11)

Apply the Task candidate cap to distinct Tasks after choosing each Task's best matching fragment.
The previous fragment-first cap could let one Task with hundreds of matching checkpoints or
comments hide all other matching Tasks. A query-local SQLite function computes the existing shared
title-phrase/term-coverage rank. A materialized CTE retains only identity/rank fields and FTS BM25;
a window selects one fragment per Task before the cap. Project filtering precedes this selection,
and current-Task preference remains subordinate to match quality and coverage. The function is
removed after each query, including failures. Matching-fragment work still grows with matching
history; this bounds candidate transfer and authoritative rereads, not total SQLite work.

Before lexical Task retrieval, look up a full Task ID or a generated hexadecimal ID prefix of
10–31 characters, optionally prefixed with `task:`. Generated hex IDs are case-insensitive; existing
opaque IDs with searchable text retain exact equality, with the raw ID checked before reference
normalization. Exact IDs win over prefixes. Ambiguous prefixes return a deterministic list up to
the requested limit rather than selecting a unique Task. A direct match suppresses incidental
lexical mentions within the Task channel; an unknown ID falls back to normal lexical search.
Direct results use `task:<id>` and the latest checkpoint summary. Lexical results keep the selected
checkpoint/event ref and its existing context expansion. All reads retain their existing Project
scope; only the home dashboard's global Task search spans registered Projects. This requires no
schema migration, FTS rebuild, or model-visible field additions.

Regression coverage includes 400 matching checkpoints/comments beside a weaker matching Task,
best-fragment selection and context verification, repeated queries and recovery after a ranking
failure, full/prefix/opaque IDs and prefix collisions, and real stdio MCP cross-Project isolation.

A local synthetic cost probe on 2026-09-11 kept both matching Tasks visible with 400 and 4000
matching checkpoints in the first Task. With 2016-byte summaries, the median of three subsequent
`search_tasks` calls was approximately 122 ms and 1153 ms respectively; six-byte summaries took
approximately 6 ms and 64 ms. These are informational local measurements excluding fixture setup,
IPC, and currentness reconciliation, not a service latency guarantee. A separate large-history
performance gate is needed before claiming scalable latency; restoring the fragment-first cap
would reintroduce the demonstrated recall defect.

## Consequences

- MCP no longer contains Knowledge/Task search stubs; the public scopes correspond to real local retrieval channels.
- The daemon remains the only owner of Workspace resolution, Project isolation, database reads, and semantic retrieval policy.
- FTS corruption can be repaired from durable state; it does not redefine Knowledge or Task truth.
- Existing databases migrate forward with semantic search immediately available through backfill.
- Freshness and negative disclosure are API behavior: stale Knowledge cannot masquerade as current, and unrelated/cross-Project Knowledge/Tasks must not appear in search or context.
- Search remains lexical/local in this slice. Symbol graphs, Working Sets, embeddings, and richer cross-channel fusion remain separate work.

## Verification

Automated coverage must prove:

- real v10 databases backfill Knowledge, Task title, checkpoint, and operator-feedback search fragments during v11 migration;
- post-migration Knowledge/Task writes, updates, and deletes keep FTS-derived candidates synchronized;
- Knowledge and Task searches are exact-Project scoped and stale Knowledge ranks after fresh Knowledge;
- Task results can preserve the exact matching durable checkpoint/operator-feedback ref;
- `project_context` expands only selected refs and rejects missing/cross-Project/wrong-kind identities;
- existing `code:` model-facing context fields remain backward compatible and docs use stable `doc:` refs;
- no source text, content hashes, unrelated Knowledge, unrelated Tasks, or ranking internals leak to MCP results;
- query/item/ref/IPC/model response byte limits remain enforced, including regression coverage for maximum Knowledge/checkpoint payload compaction;
- real stdio MCP integration exercises Knowledge, Task history, docs, stale Knowledge, and cross-Project negative disclosure;
- repo-wide formatting, lint, strict typing, tests, and wheel smoke remain green.
