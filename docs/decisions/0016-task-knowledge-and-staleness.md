# ADR-0016: Persist Task Knowledge with mechanical anchors and fail-closed staleness

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

ADR-0015 completed the transport-independent Task start/resume/checkpoint workflow and deliberately left Knowledge outside that slice. The audited delivery sequence places Knowledge + staleness immediately after Task lifecycle and before model-facing MCP contracts. The product specification requires semantic Knowledge to be learned only from real Task investigation, carry provenance, prefer code anchors, retain stale history, and never trigger autonomous LLM repair.

A Knowledge implementation must not turn agent-supplied source text into a second source of truth. Filesystem bytes remain authoritative for code, the Structural Index remains rebuildable derived state, and stale semantic claims must fail closed rather than continue to appear current after their mechanical anchors change.

## Decision

Add schema version 8 with durable `knowledge_cards` and `knowledge_anchors` tables.

A card records project ownership, one bounded semantic kind, normalized title/body, provenance, timestamps, and explicit freshness (`fresh` or `needs_revalidation`). The v1 source types in storage are `agent_asserted`, `operator`, `repository_document`, and `ADR`; this slice exposes creation only for `agent_asserted` cards produced by a real Task checkpoint. Agent-asserted cards retain stable `source_task_id` and `source_checkpoint_id` strings as historical provenance even if later lifecycle cleanup removes the originating rows.

Code/document anchors store only:

- source Workspace identity;
- normalized Workspace-relative path;
- optional symbol locator;
- mechanical entry kind (`file` or `symlink`);
- SHA-256 fingerprint.

Raw source bytes are never persisted in Knowledge. Regular-file fingerprints are captured from stable filesystem bytes without following paths outside the registered Workspace. Symlink fingerprints hash the link target text and never read the external target. A missing, unsafe, escaping, non-file anchor, changing file, or capture deadline failure rejects the entire checkpoint mutation.

`task_checkpoint(..., knowledge=...)` accepts a bounded batch of `KnowledgeDraft` values. Knowledge validation happens before the write transaction; mechanical anchor capture and card persistence happen inside the same `BEGIN IMMEDIATE` checkpoint transaction after Task identity/revision/state checks. The Task revision update, checkpoint, Knowledge cards/anchors, and checkpoint event either all commit or all roll back. A stale revision therefore cannot persist stale semantic content.

Bounds for this internal durable slice are:

- at most 8 cards per checkpoint;
- at most 8 anchors per card and 32 anchors per checkpoint;
- title <= 256 UTF-8 bytes;
- body <= 8192 UTF-8 bytes;
- anchor path <= 4096 UTF-8 bytes;
- anchor symbol <= 512 UTF-8 bytes.

These are persistence/work bounds, not the later MCP serialized-response budgets.

Workspace scan reconciliation compares every still-`fresh` source-Workspace anchor with the new mechanical scan snapshot in the same index transaction. Any missing path, kind change, or fingerprint change transitions the whole card exactly one way:

```text
fresh -> needs_revalidation
```

A later scan that happens to match the old fingerprint does not restore freshness. Only later real work may create/revalidate semantic Knowledge. Unanchored cards are not mechanically invalidated by file scans.

This slice does not add Knowledge search/ranking, Working Sets, embeddings, IPC, CLI, MCP payloads, operator-authored Knowledge APIs, or automatic semantic repair.

## Consequences

### Positive

- Semantic Knowledge can now grow only through a durable real-Task checkpoint path.
- Provenance and Task revision CAS are transactionally coupled to Knowledge creation.
- File/symlink staleness is mechanical and source bytes remain outside the database.
- Stale cards remain historical evidence but cannot remain marked current after a detected anchor change.
- Scan/index reconciliation and semantic durability fail together rather than silently diverging on corruption.

### Costs and limits

- Schema version increases from 7 to 8.
- Freshness is currently reconciled against anchors captured in the originating Workspace; cross-Workspace presentation remains a later search/MCP concern and must not assume a source-Workspace `fresh` flag proves another worktree matches.
- Unanchored semantic claims cannot be mechanically invalidated by filesystem scans.
- File fingerprinting adds bounded checkpoint work for explicitly supplied anchors.
- Operator/document/ADR source types are reserved by the durable schema but have no creation API in this slice.

## Verification

Automated tests must prove:

- schema v7 migrates to v8 without losing Task/checkpoint/event state;
- schema constraints reject invalid provenance, freshness, fingerprint kinds, and malformed hashes;
- a real checkpoint atomically persists agent-asserted Knowledge with Task/checkpoint provenance and mechanical file anchors;
- raw anchored source bytes are not persisted;
- stale revision and invalid/changing anchors leave Task/checkpoint/event/Knowledge state unchanged;
- a failure after Knowledge insertion still rolls the whole checkpoint transaction back;
- symlink anchors never read external target contents;
- a matching scan leaves anchored Knowledge fresh;
- changed, removed, or kind-changed anchors transition `fresh -> needs_revalidation`;
- later matching scans never auto-refresh stale Knowledge;
- unanchored Knowledge is unaffected by mechanical file scans;
- readers fail closed on unsafe/corrupt persisted anchor metadata.
