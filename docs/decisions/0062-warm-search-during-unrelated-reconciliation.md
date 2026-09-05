# ADR-0062: Reuse proven warm search currentness while reconciliation is busy

- **Status:** Accepted
- **Date:** 2026-09-06

## Context

One daemon scan lock serializes reconciliation, visibility changes, and skill projection across
registered Workspaces. Release verification reproduced a slow watcher scan in one Workspace causing
an already-current search in a different Workspace to exhaust its deadline. SQLite WAL supports
reading the latter Workspace while the former has an uncommitted write transaction.

Splitting mutating locks would require a broader review of Git-common-directory visibility policy,
SQLite writes, projection ownership, and lock ordering. A warm search needs no mutation when its
existing persisted token, index revision, and complete candidate-path set still match.

## Decision

1. Attempt the existing scan lock without waiting. An uncontended request uses the established
   locked currentness/reconciliation path with no extra Git work.
2. If the lock is occupied, try a read-only proof in one short SQLite read transaction. Require a
   persisted search proof, equal live Git/change token and index revision, and equality of the live
   Git/ignore candidate-path set and indexed paths. Return the immutable proof only on full equality.
3. Close that read transaction on success, mismatch, or exception before retrieval or lock waiting.
   Reject callers with an already-open transaction without rolling back their work. The helper never
   reconciles, changes search state, or updates any durable record.
4. On an absent or mismatched proof, wait for the existing lock using only the remaining execution
   deadline, then recompute through the established locked path. Cold searches and immediate edits
   retain this requirement. No timeout is increased.
5. Retain the post-retrieval live token and latest index-revision comparison from ADR-0046, including
   its bounded retry. A commit during the read-only proof or retrieval invalidates the old proof;
   the temporary read transaction must not pin the revision used by this final check.

The optimization changes no mutating lock, visibility admission, shared Git-directory policy,
schema, or response budget. A long unrelated scan can still delay a search that needs reconciliation.

Currentness failures gain bounded daemon/MCP error distinctions: `search_timeout` reports expiration
of the existing execution deadline, and `search_workspace_changed` reports repeatedly moving source.
Both return fixed retry guidance without exception text, source paths, database details, or private
index state. Other inspection/storage failures retain their existing bounded failure contracts.

## Verification

- A real watcher holds the scan lock and an uncommitted SQLite write for a foreign Workspace while
  a previously proven search succeeds, with no writes by the search connection.
- Dirty and never-proven Workspaces still fail with the bounded deadline when the lock stays busy.
- An index commit during the read proof, including source changing A → B → A, causes a fresh retry
  and cannot return mixed current source and stale indexed candidates.
- Read-proof exceptions close only the helper's transaction; caller-owned transactions are rejected
  and remain open. Existing edit/revert, branch-switch, ignore-policy, and response-budget tests pass.
- Strict IPC and real MCP tests assert fixed timeout/unstable-source codes, guidance, and absence of
  private exception details.
