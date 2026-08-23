# ADR-0012: Capture a mechanical Task baseline before public task_start

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** Repository architecture baseline

## Context

ADR-0011 established durable Harness Task identity and revision compare-and-set without exposing the model-facing `task_start` workflow. The remaining prerequisite is the automatic baseline required by specification §48 and the audit's dirty-tree amendment.

A useful Task baseline cannot be only `HEAD + branch + dirty path names`. If a file was already dirty before the Task and is edited again during the Task, a later checkpoint must be able to distinguish that second change from the pre-existing dirty state. Likewise, recording only a last-scan timestamp would not prove whether the persisted Structural Index actually matched the filesystem when the Task began.

The baseline must therefore remain mechanical and local, preserve enough evidence for later changed-file calculation, and avoid storing raw source text.

## Decision

Add schema version 5 with a one-to-one durable `task_baselines` record and zero or more `task_baseline_dirty_paths` rows.

A baseline captures:

- owning Workspace through the Task relationship;
- Git `HEAD`, including `NULL` for an unborn repository;
- current symbolic branch, including `NULL` for detached HEAD;
- UTC capture timestamp;
- whether the current persisted Structural Index is actually fresh at capture time;
- persisted index file count;
- one SHA-256 digest of the persisted index snapshot;
- every pre-existing Git dirty path, including rename/copy origin where Git reports one;
- the two-character porcelain status code for that path;
- a local SHA-256 state fingerprint and an explicit fingerprint confidence kind.

`index_is_fresh` is not inferred from elapsed time or the existence of a previous scan. Harness builds the same deterministic live inventory used by the Structural Index and compares it to the current persisted `indexed_files` snapshot without mutating the index. The baseline therefore records the truth of that comparison at capture time. The persisted index digest identifies exactly which derived snapshot was observed. Invalid/corrupted persisted index rows fail baseline capture instead of being silently treated as stale or fresh.

Dirty-path fingerprints are local mechanical evidence, not persisted source:

- `file`: hash includes Git status metadata plus regular-file bytes read with before/open/after stability checks;
- `symlink`: hash includes Git status metadata plus the link target string and never follows the target for content;
- `missing`: hash records status metadata plus a missing-path sentinel;
- `opaque`: used for entries such as a dirty submodule/directory where Harness does not claim a complete content fingerprint.

Future changed-file calculation must treat an `opaque` pre-existing entry conservatively. Equality of an opaque fingerprint is not proof that nested content stayed unchanged.

Baseline capture is bounded by a finite deadline and fails closed if Git metadata, dirty-file state, Workspace registry identity, Git filesystem identity, or the persisted Structural Index changes during capture. Git state is sampled twice around the live index snapshot; dirty regular files are content-fingerprinted on both samples.

Every domain-level creation of a new Task now runs through the baseline-aware path in one `BEGIN IMMEDIATE` transaction:

1. validate the Workspace and one-working-Task invariant;
2. capture the stable mechanical baseline;
3. insert the new Task at revision `1`;
4. insert its baseline and dirty-path rows;
5. commit once.

A capture or persistence failure therefore leaves no newly created Task. The baseline is part of creation, not a later existing-Task mutation, so it does not consume a second revision. The convenience `create_task_record` API delegates to this same transaction; there is no normal domain creation escape hatch that can commit a new Task without its baseline. Historical schema-v4 Tasks migrated into v5 may legitimately predate baseline storage and are preserved without inventing evidence that was never captured.

This slice still does **not** expose daemon IPC, CLI, MCP `task_start`, resume semantics, Task events, checkpoints, changed-file calculation, verification, Knowledge, or Working Set updates.

## Consequences

### Positive

- Pre-existing dirty regular files can later be distinguished from additional Task-time edits without storing their source text.
- Rename provenance survives Task start.
- Structural Index freshness is a direct mechanical comparison rather than a timestamp heuristic.
- A crash or capture race cannot leave a newly created v5 Task without its required baseline.
- Dirty submodules and other incompletely fingerprinted entries are explicitly marked instead of producing false confidence.
- Corrupted persisted index state cannot silently seed an invalid Task baseline.
- No baseline field requires an LLM, cloud service, hook, or host-specific signal.

### Costs and limits

- Schema version increases from 4 to 5.
- Baseline creation performs a read-only deterministic live index snapshot and can therefore approach scan cost; capture has a finite 30-second deadline.
- Dirty regular-file content is read locally to compute a hash but raw content is not persisted or sent externally.
- Opaque dirty entries cannot support exact same-path attribution; later changed-file logic must include them conservatively when necessary.
- Schema-v4 Tasks migrated into v5 have no retroactive baseline because fabricating one would misrepresent historical state.
- The baseline records Workspace state, not line-level authorship. Human and agent edits in the same Workspace remain intentionally unattributed in v1.

## Verification

Automated tests must prove:

- v4 → v5 migration preserves existing Task state and creates empty baseline storage;
- clean/current Structural Index snapshots report `index_is_fresh=true` without mutating the index;
- filesystem changes after a scan report `index_is_fresh=false`;
- every normal domain Task creation writes Task + baseline atomically at revision `1`;
- capture failure leaves no Task or baseline rows;
- corrupted persisted index rows abort creation and leave no Task;
- pre-existing modified and untracked regular files receive deterministic content-sensitive fingerprints;
- changing an already-dirty regular file changes its baseline fingerprint;
- staged rename origin is preserved;
- dirty submodule/directory state is explicitly `opaque`;
- raw dirty source content is absent from persisted baseline rows;
- invalid freshness/fingerprint-kind rows are rejected by SQLite;
- corrupted unsafe persisted paths fail closed when read.
