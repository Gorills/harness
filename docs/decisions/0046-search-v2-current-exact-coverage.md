# ADR-0046: Search v2 starts with current-worktree exact coverage before symbols or embeddings

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** Repository architecture baseline

## Context

`project_search` already combines path retrieval, local contentless FTS5 code/docs candidates,
Project-scoped Knowledge and Task history, explicit quality tiers, and bounded current-source evidence.
That substantially improves natural-language discovery, but it still leaves two common reasons for an
agent to repeat search with native `rg`/`grep`:

1. the watcher is intentionally asynchronous, so an edit, revert, ignore-rule change, or branch switch
   can occur before its next reconciliation; a live-SHA check prevents stale evidence on an already
   selected hit but cannot prevent a false negative when the new source was never a candidate;
2. file-level lexical retrieval returns the best candidate files but does not prove that every exact
   occurrence of an identifier or literal has been located.

The next long-term layers are parser-backed symbol/code-unit indexing, relationship navigation, and
hybrid semantic retrieval. Starting with those layers would not remove the two correctness gaps above:
a symbol or embedding index is still stale if it trails the worktree, and semantic top-k retrieval is
not a replacement for exhaustive exact reference discovery.

The product objective for Search v2 is therefore not primarily response-token reduction. It is to
remove repeated native discovery calls by making one `project_search` sufficient for the next
engineering reasoning step while preserving deterministic source-of-truth and bounded-response
invariants.

## Decision

### 1. `project_search` has read-your-worktree currentness

Before Project Intelligence retrieval, the daemon synchronously proves that the Structural Index
matches the active Workspace state instead of relying on watcher timing.

Search currentness reuses the existing watcher Git snapshot primitive. The snapshot token covers Git
HEAD/status plus stable metadata identity for dirty paths and `.harnessignore`. Schema v17 adds a
small persisted search-currentness record containing the last search-proven HEAD/token, the
Structural Index reconcile revision proved at that point, and the dirty paths present at that point.
No source body is persisted. The revision prevents an intervening watcher/explicit scan from becoming
invisible if source later returns to the same previously proven Git token.

For each search the daemon, under the same scan lock used by the watcher:

- samples the current Git change snapshot and reads the current Structural Index reconcile revision;
- enumerates the exact current Git/ignore candidate-path set and compares it with `indexed_files`;
- unions candidate-set additions/removals with both the previous and current dirty-path sets;
- when HEAD changed, obtains a bounded `git diff --name-only --no-renames` path delta between the two
  commits;
- performs incremental reconciliation when the complete changed-path set is at most the existing
  incremental limit, otherwise falls back to a full authoritative scan; if the live Git token returned
  to the previously proven value but the index revision changed meanwhile, it also falls back to full
  reconciliation because the intermediate indexed state cannot be reconstructed safely;
- resamples current state after reconciliation and retries once if the Workspace changed during the
  operation;
- records search-currentness only after the candidate-path set and index agree for a stable snapshot.

A first search after schema migration has no historical currentness proof and therefore performs one
full authoritative reconciliation. Failure to prove currentness fails Project search rather than
returning a stale-success response.

Retrieval is followed by another live change-snapshot **and index-revision** comparison. If source
changed or any watcher/explicit scan committed while candidates or evidence were being assembled, the
daemon retries from reconciliation rather than returning a mixed snapshot. Comparing the revision as
well as the live token closes an ABA race where source could move `A → B → A` while an intermediate B
scan changed the index. A successful model-facing result therefore reports `workspace_state="current"`.

This is search-level read consistency, not a filesystem lock on the developer: a change immediately
after the final validation can naturally occur after the search result was produced.

### 2. Explicit identifiers/literals get exhaustive exact coverage

When a code/docs/all query exposes one unambiguous exact needle, `project_search` adds
`exact_coverage` in addition to ranked hits. Initial needle recognition covers:

- a quoted or backticked literal;
- an identifier-shaped token with camelCase/snake_case/dotted structure inside a natural query;
- a query consisting of one non-whitespace term.

Exact matching is case-sensitive literal matching against current Workspace source, not FTS or an
embedding approximation. Harness safely rereads regular files using the current `indexed_files`
SHA as the expected source identity. It searches bounded UTF-8 text locally and returns aggregate
counts plus grep-like `path`, `line`, `column`, and line preview locations.

The exact-search source boundary is deliberately broader than FTS evidence: one file may be read up
to 8 MiB and one query may inspect at most 64 MiB of source. NUL/binary or invalid-UTF-8 files are
reported as `non_text_files` and do not make text coverage incomplete. Oversized, unstable,
unavailable, changed-since-index, or total-scan-budget files increment `unavailable_files` and make
`complete=false`.

All matches are counted even when the model-visible location list is capped. `locations_truncated`
therefore distinguishes exhaustive counting from complete location disclosure. Exact coverage itself
is capped at 4 KiB and currently exposes at most 24 locations.

Agents may treat exact coverage as a replacement for repeating that needle with native `rg`/`grep`
**only when** `complete=true` and `locations_truncated=false`. Otherwise native targeted fallback
remains valid.

### 3. Rich evidence and exact coverage share the existing response budget

This slice does not increase the 12 KiB model-facing `project_search` budget merely because Search v2
has started. The exact-coverage payload reserves its actual bounded encoded size before current-source
rich evidence is fitted. Existing evidence is removed from lower-priority hits when needed rather than
letting the combined response cross the established MCP budget.

This is not a commitment to keep 12 KiB permanently. Later symbol/relation packet work may justify a
larger budget based on real agent traces. The decision here is only to avoid increasing exposure before
information density and measured truncation demand it.

### 4. Search-quality acceptance measures redundant native search

The Codex JSONL search-behavior evaluator adds `complete_exact_to_native_search`. It is true when a
complete, untruncated Harness exact coverage result is followed by native `rg`/`grep` substantially
repeating the same needle. This is acceptance evidence rather than daemon telemetry or a hard host
policy.

### 5. Symbols and semantics remain the next layers, in that order

This ADR intentionally does **not** add Tree-sitter, SCIP, embeddings, a reranker, or an internal LLM.
The next Search v2 layer should index bounded code units/symbol definitions and relationship/navigation
signals so definitions, references, callers, implementations, tests, and imports can be assembled
around exact/lexical seeds. Hybrid semantic retrieval should follow that structural substrate and act
as another candidate channel, not replace deterministic exact search.

## Consequences

- An immediate dirty edit can become searchable in the same `project_search` call without waiting for
  watcher debounce/polling.
- Reverting a dirty edit removes its stale indexed candidate before search returns.
- Clean branch switches reconcile changed paths synchronously; large or unprovable deltas fail over to
  full scan rather than serving the prior branch's candidates.
- Ignore-policy changes are detected through the current candidate-path set, including paths that
  become newly visible or newly excluded.
- Explicit identifier/literal search now supplies exhaustive text counts and locations when bounded
  completeness can be proven, reducing the need for `rg` reference discovery.
- Search latency can be higher on the first v17 search, after a large branch switch, or when watcher
  lag requires reconciliation. Correct current source is preferred to fast stale candidates.
- The persisted currentness state contains only Git/source-change identities and paths, not source
  bodies.
- Binary files do not degrade text-search completeness; genuinely unsearchable or over-budget text
  does, and the model sees that explicitly.
- Symbol relations and semantic retrieval remain missing capabilities; this slice does not claim that
  native targeted source reads are obsolete yet.

## Verification

Automated coverage must prove:

- v16→v17 creates currentness tables without fabricating a current state for existing indexes;
- immediate dirty edits are found before watcher reconciliation and exact coverage uses the new text;
- revert removes the dirty-only candidate immediately, including `A → watcher scans B → revert A`
  where the live token alone would otherwise hide the intervening index revision;
- clean branch switching cannot leak the prior branch candidate and bounded branch deltas reconcile
  incrementally;
- Git ignore changes reconcile candidate-path additions/removals before retrieval;
- exact coverage counts all occurrences across multiple text files while binary content is classified
  non-text;
- location truncation preserves complete aggregate totals and is explicit;
- exact coverage plus rich evidence remains inside the established Project-search response budget;
- strict daemon IPC and real MCP expose `workspace_state=current` and exact coverage;
- search-behavior evaluation flags redundant native search only for complete, untruncated exact
  coverage;
- repository formatting, lint, strict typing, tests, and exact-head CI remain green before merge.
