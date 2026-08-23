# ADR-0013: Calculate Task changed files from baseline plus current Git state

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

ADR-0012 records a mechanical Task baseline before a new Task is committed. The specification requires Harness to determine changed files mechanically at checkpoint time, and the audit explicitly rejects using only a checkpoint-time `git diff` when the Workspace was already dirty before the Task started.

The first changed-file slice must therefore distinguish unchanged pre-existing dirtiness from Task-time state changes without storing source text, while also including committed changes that may leave the worktree clean. It must remain a read-only domain calculation; checkpoint persistence, events, Task revision mutation, verification, Knowledge, IPC, CLI, and MCP exposure are separate later slices.

## Decision

Add an internal `calculate_task_changed_files(connection, task_id)` domain operation. It loads the Task's durable baseline, samples the current Git/worktree state twice around the Git tree comparison, verifies registered Workspace/Git identity before and after the calculation, and returns a deterministic sorted tuple of relative paths.

The result is the conservative union of:

- paths whose committed Git tree differs between the baseline HEAD and the current HEAD;
- dirty paths that exist now but were not dirty at Task start;
- baseline-dirty paths whose current dirty record no longer exactly matches the baseline record;
- both sides of a rename/copy when a changed dirty record carries an origin path.

ADR-0012's dirty fingerprint is strengthened in the same prerequisite slice with a `task-dirty-state-v2` domain marker, exact `git ls-files --stage -z` bytes for that path, and stable regular-file permission mode. Without the index state, a pre-existing `MM` file could have its staged blob replaced while its worktree bytes and two-character status returned to the same values; without the file mode, a pre-existing dirty regular file could change executable/permission state while preserving the same status and bytes. Either case would otherwise produce a false negative. Existing v5 baseline rows created by older code have no fingerprint-version column; the v2 domain marker therefore makes such cross-version comparisons conservatively mismatch rather than silently treating unverifiable state as unchanged.

A pre-existing dirty path is excluded only when all of the following are true:

1. the path has no committed tree delta between baseline HEAD and current HEAD;
2. the current dirty record exactly equals the baseline record, including two-character porcelain status, rename/copy origin, fingerprint kind, and state SHA-256;
3. neither the baseline nor current fingerprint kind is `opaque`.

This means an unchanged pre-existing regular-file edit, untracked file, deletion, symlink, or rename is not attributed to the Task merely because it was already dirty. If that path is edited again, staged/unstaged differently, cleaned, committed, renamed differently, deleted/recreated, or otherwise changes mechanical Git/filesystem state, it is included. `opaque` entries are always included conservatively when present in the baseline/current dirty comparison because equality of their metadata fingerprint is not proof that nested content stayed unchanged.

Committed comparison uses Git tree truth rather than the Structural Index. Rename detection is disabled for that comparison so both the removed and added path are retained deterministically; external diff/textconv execution is disabled and submodule-ignore configuration is overridden so repository configuration cannot suppress mechanical path truth. For an unborn baseline followed by a first commit, every path in the current committed tree is a committed candidate. If a baseline has a HEAD but the current repository becomes unborn, every path in the baseline committed tree is a candidate.

The calculation has a shared finite 30-second deadline. The same deadline covers baseline-row streaming, Git identity inspection, content-sensitive dirty-state sampling, committed-tree comparison, and result merging. Current Git state is sampled before and after the tree comparison; any mismatch fails closed instead of returning a mixed-time result. Baseline rows are streamed with optional deadline checks rather than unconditionally materialized with `fetchall()`.

The operation is read-only. It does not mutate the Task, does not consume or increment `revision`, does not reconcile the Structural Index, and does not persist a checkpoint. Missing historical baselines and corrupt Git/baseline state fail closed. Raw source text is neither persisted nor returned; regular-file bytes are read locally only through the existing baseline fingerprint mechanism.

Changed files remain Workspace-state attribution, not human-versus-agent authorship. A human edit that occurs after Task start is mechanically part of the Task-time Workspace delta in v1, matching ADR-0012's explicit non-attribution limit.

## Consequences

### Positive

- Checkpoint work can later consume a deterministic mechanical changed-file set without asking the agent to enumerate files.
- Pre-existing dirty files are not falsely reported when their exact non-opaque state is unchanged.
- Clean worktrees after Task-time commits still report committed paths.
- Task-time rename/copy operations retain both affected path names.
- Opaque submodule/directory state fails conservative rather than false-negative.
- Structural Index freshness or corruption cannot suppress Git/filesystem truth for changed-file calculation.
- The calculation is bounded, race-aware, read-only, and leaves Task CAS state untouched.

### Costs and limits

- Content-sensitive current dirty-state sampling can approach baseline-capture cost for large dirty files.
- `opaque` entries may be over-reported by design.
- The result is a net mechanical delta, not a chronological edit log; a clean path changed and then restored to its baseline tree/worktree state is not reported unless another retained Git-state difference remains.
- This slice does not persist changed files, so completed-checkpoint history still requires the later checkpoint/event persistence slice.
- No model-visible item limit is defined here because this API is internal only; the future IPC/MCP checkpoint contract must impose its own exposure budget.

## Verification

Automated tests must prove:

- unchanged clean Tasks return no paths and do not change Task revision;
- new tracked and untracked worktree edits are included;
- identical pre-existing dirty state is excluded;
- a pre-existing dirty regular file whose permission mode changes is included even when its bytes and porcelain status remain identical;
- a pre-existing dirty file edited again is included;
- a pre-existing dirty file that becomes clean is included;
- a committed Task-time change is included even with a clean worktree;
- both sides of a Task-time rename are reported;
- an unchanged pre-existing rename is excluded;
- identical `opaque` dirty state remains conservatively included;
- an unborn baseline followed by a first commit reports the new committed paths;
- a missing baseline fails closed;
- Git/worktree state changing during calculation fails closed;
- deadline expiry while streaming baseline dirty rows fails closed.
