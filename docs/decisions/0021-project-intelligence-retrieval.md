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
