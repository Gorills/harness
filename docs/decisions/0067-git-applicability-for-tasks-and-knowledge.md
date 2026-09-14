# ADR-0067: Apply Task and Knowledge evidence to the active Git checkout

- **Status:** Accepted
- **Date:** 2026-09-14
- **Deciders:** Operator request; repository architecture review

## Context

Project identity alone previously admitted unrelated branch Tasks to status/search/context.
Knowledge search had an anchor check, but explicit Knowledge scope and direct context refs could
bypass it. A recorded branch name identifies work; it does not prove that its code is present.
Conversely, requiring every Task fingerprint to remain unchanged on its original branch would
prevent ordinary ongoing work.

## Decision

One daemon-owned `WorkspaceApplicability` supplies request-local Git and live-content proofs.
It fixes registered Workspace identity, Git HEAD/branch, and a deadline, memoizes ancestry by
source commit and fingerprints by path, and validates identity, Git state and consulted file
evidence before return. Public project retrieval creates this resolver when none is supplied;
daemon callers pass their shared instance. Optional unfiltered domain getters are internal and
also support the explicit operator archive, not model-facing defaults.

Schema v23 adds Task origin metadata and checkpoint evidence tables. They store Git presence,
origin HEAD/branch, completeness, and at most 256 changed-path fingerprints (file, symlink or
deleted). Existing checkpoint HEAD/branch/changed paths remain authoritative. No raw source is
stored. Capture rejects files over 8 MiB as unknown evidence, limits aggregate fingerprint reads
to 32 MiB, and runs in the checkpoint transaction. Expected size/path exhaustion records
incomplete evidence without blocking the checkpoint; identity/race/deadline failures roll back
the mutation. Retrieval budget exhaustion still fails closed.
Readers have a five-second shared deadline; checkpoint capture has thirty seconds. Evidence is
reread for validation, so this may read the bounded consulted files twice. Symlinks are hashed
as link text and escaping parents cannot establish applicability.

Task applicability uses its latest checkpoint, otherwise its recorded origin:

- On the originating Workspace/branch, captured HEAD must still be an ancestor of active HEAD.
  Ordinary subsequent edits do not hide an ongoing Task. A reset that removes that commit does.
- A clean checkpoint with Task changes becomes historical evidence in another branch when its
  captured commit is included by ancestry. Subsequent edits or reverting that commit do not
  erase the fact that this Task was integrated.
- Dirty checkpoints and integrations that rewrite commit identity require complete matching
  current changed-path fingerprints and the available source/baseline ancestry proof. This
  admits work checkpointed before commit and exact-content squash/cherry-pick integrations.
- An initial baseline shared by several branches is insufficient to transfer a Task implicitly.
  Before its first checkpoint, explicit `task_id` plus revision CAS permits handoff to a branch
  descended from the baseline, including after edits/commits. Resume records the new origin
  and advances revision; checkpoint records its own branch. Filesystem-to-Git initialization
  before that first checkpoint preserves Task identity without fabricating an old Git baseline.
- Filesystem Workspaces without Git keep Workspace-local Task continuity. Unborn/detached
  checkouts do not acquire invented branch or commit identities.

Knowledge uses its **own source checkpoint**, never the Task's latest checkpoint. All anchored
cards, including imported/operator cards, require current live fingerprints. Unanchored agent
cards with checkpoint changes require the complete current checkpoint content proof; the Task's
same-branch continuation exemption cannot rescue them after a local revert. Unanchored cards
without changes remain confined to the captured unchanged source context. General operator
notes without code anchors or checkpoint provenance remain project-wide. Freshness remains
the separate `fresh`/`needs_revalidation` historical label; applicability never upgrades it.

Task/Knowledge filtering precedes FTS candidate limits, Task ID/prefix limits and history
pagination. Checkpoint fragments have their own applicability test, including search fragments,
direct refs and recent Task history. A visible latest Task cannot expose an incompatible older
checkpoint. Explicit model refs fail closed rather than bypassing eligibility. Workspace and
Project dashboard views use the same proofs; the global operator home and Task archive retain
all branches for management, deferral and deletion. Private Task-status IPC carries sampled
HEAD/branch so the bridge rejects mixed status round trips without adding model-visible fields.

## Compatibility and limits

- Migration does not populate new evidence from today's checkout. Existing clean committed
  checkpoints can use their old recorded commit/changed-path evidence; legacy dirty checkpoints
  cannot acquire invented cross-branch content proofs. Unknown metadata stays unavailable when
  it cannot be proven. The global operator archive remains available.
- There is still one `working` Task per Workspace. A hidden working Task can therefore block
  starting another tracked Task. The error exposes no hidden Task ID/title and directs the
  operator to defer it in the archive. Small work can also remain untracked under ADR-0066.
- Fingerprints are whole-file evidence, not patch equivalence. Squash/cherry-pick plus unrelated
  changes in the same file can conservatively hide a Task even though some code was integrated.
  Normal ancestry-preserving integration retains historical Task visibility after later edits.
- A Task on its originating branch remains a work item during edits or reverting uncommitted
  work; Knowledge does not receive this continuity exemption. This is not a general branch
  scheduler, semantic revalidation engine or Git patch-equivalence engine.
- Runtime identity batches root/common/private Git directories in one read-only `rev-parse`.
  Ambiguous multiline output falls back to the established individual reads to preserve POSIX
  paths containing newlines; paths are never guessed from ambiguous boundaries.

## Verification

Synthetic Git repositories cover ordinary merge/fast-forward/squash/cherry-pick, dirty checkpoint
then commit/integration, later edits/reset, explicit initial handoff and stale CAS, no-Git to Git
initialization, older Knowledge versus later Task work, hidden checkpoint fragments, manual
anchors, capture-race rollback, unknown evidence, legacy migration, deadline rejection, and
110 matching hidden Tasks ahead of one eligible Task with one memoized ancestry subprocess.
Dashboard/IPC/stdio tests cover caller wiring and mixed-round-trip status rejection. Existing
source, response-budget, negative-disclosure and Task migration checks remain required.
