# ADR-0008: Public Workspace scan is daemon-owned and fail-closed

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** Repository architecture baseline

## Context

The approved product specification defines `harness scan [PATH]` as the public legacy-project registration and deterministic initial-index entry point. The implementation already has durable Project/Workspace registry primitives, deterministic file-index reconciliation, canonical per-user daemon paths, and a strict local IPC protocol, but registration and scan are still internal-only.

A public command must preserve the architecture boundary that `harnessd` is the only process allowed to perform durable business-state transitions. The CLI must not open SQLite directly.

Project identity also needs a bounded rule. A physical linked Git worktree can safely be recognized as sharing the same repository administration directory as an already registered Workspace, while an independent clone must not be silently assumed to be the same logical Project.

Initial scan may take materially longer than a status read, so a new mutating IPC path must remain bounded rather than allowing the single daemon process to block indefinitely.

## Decision

Harness adds one internal protocol-v1 method, `workspace_scan`, consumed by the human-facing command:

```text
harness scan [PATH] [--socket PATH]
```

`PATH` defaults to the current directory. The CLI canonicalizes it, requires a directory, and sends only the absolute filesystem location to the daemon. With no `--socket`, the canonical per-user socket and its existing runtime-directory checks are used.

The daemon performs all durable work:

1. inspect the Git worktree and canonical Workspace root/common directory;
2. resolve or create durable registration under one transaction;
3. run deterministic file-index reconciliation;
4. return only compact registration/index counts.

### Project registration rule

Automatic registration is intentionally conservative:

- an already registered canonical Workspace root keeps its existing Project;
- a new linked worktree reuses a Project only when every existing Workspace with the same Git common directory resolves to exactly one Project identity;
- if no Workspace shares the Git common directory, Harness creates a new Project with the normal visibility default;
- if the same Git common directory is already associated with multiple Project identities, automatic registration fails closed as ambiguous;
- independent clones have different Git common directories and therefore are not automatically merged into one logical Project.

Explicit internal registration with a caller-provided Project identity remains available for later workflows that intentionally associate independent clones.

### Scan failure and retry

Project/Workspace registration commits before file-index reconciliation. If indexing subsequently fails or exceeds its bounded deadline, the valid registration is retained and the command returns a bounded failure stating that retry is safe.

This is deliberate: registration is durable identity, while `indexed_files` is rebuildable derived state. Deleting a newly registered identity on a later scan failure would create a more dangerous compensating-delete race and could remove state observed by another daemon request.

Repeated scan of the same unchanged Workspace is idempotent: it returns the same Project/Workspace identities and zero add/update/remove deltas.

### Time bounds

The daemon gives public Workspace scan a finite operation deadline. Git enumeration receives the remaining deadline as a subprocess timeout, file hashing checks the deadline between bounded chunks, and index mutation checks the deadline before commit. Deadline expiry during reconciliation rolls back that reconciliation transaction.

The IPC client timeout is longer than the daemon scan deadline so the daemon reaches a structured timeout result before the ordinary client gives up.

## Consequences

### Positive

- `harness scan` becomes a real installed user workflow instead of a test-only/internal primitive.
- The CLI remains thin and never duplicates registry/index business logic or touches SQLite directly.
- Linked worktrees naturally join an unambiguous existing Project while independent clones are not guessed together.
- Ambiguous historical registry state fails closed instead of silently choosing a Project.
- Initial scan has an explicit availability bound and safe retry behavior.
- Existing protocol-v1 status shapes remain unchanged.

### Costs and limits

- Registration may remain after a failed initial index reconciliation; this is explicit and retry-safe rather than transactional rollback of identity plus derived state as one unit.
- The current daemon still processes IPC clients sequentially; a bounded scan can delay other requests until it finishes. Background scan scheduling is deferred until evidence requires it.
- This slice indexes only the already implemented mechanical file inventory. It does not add parsers, symbols, FTS population, watcher behavior, search, MCP tools, or semantic Knowledge.
- This slice does not autostart the daemon or implement `harness install`.
- Windows local IPC remains unsupported until its separate transport task is proven.

## Verification

Automated tests must prove:

- an unregistered Git worktree is registered and indexed through daemon IPC;
- rescanning is idempotent and preserves Project/Workspace identity;
- a linked worktree reuses an unambiguous same-common-dir Project;
- multiple Project identities sharing one common directory make automatic registration fail closed;
- indexing failure retains valid registration and reports safe retry semantics;
- scan deadline expiry does not partially mutate `indexed_files`;
- Git enumeration receives a finite remaining subprocess timeout;
- installed CLI help exposes `scan` and its canonical-socket override contract;
- existing status/doctor behavior remains compatible.