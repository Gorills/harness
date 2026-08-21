# ADR-0006: Hidden mode transitions use crash-safe commit ordering

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline
- **Refines:** ADR-0004 (transition ordering/rollback) and ADR-0005 (recovery evidence)

## Context

ADR-0004 makes visibility transitions a revocation/revalidation barrier, and ADR-0005 prevents daemon recovery from treating lost volatile admission state as proof that old Normal capability disappeared.

One atomicity gap remains because a visibility transition spans two different state owners:

- durable Harness domain state in SQLite; and
- host-owned settings, permissions, projections, attribution controls, or other enforcement side effects outside that SQLite transaction.

Those changes cannot be committed in one database transaction. In particular, a naïve `hidden → normal` implementation could remove Hidden enforcement first and crash before persisting `visibility_mode=normal`. On restart the durable Project would still report `hidden` even though agent SCM publication was no longer denied.

The reverse direction can also leave provisional Hidden policy behind if Harness hardens the host and crashes before persisting `hidden`. That state is over-restrictive rather than unsafe, but it still requires deterministic recovery.

## Decision

Visibility transitions that touch external host policy are implemented as a **crash-safe saga** under the ADR-0004 serialization boundary. Harness persists an operator-facing transition record before the first external side effect. The record contains enough ownership/phase information to resume or compensate Harness-owned changes after restart; it is not model-facing and does not add a third `visibility_mode` value.

The security invariant is:

> Harness must never have durable/effective `visibility_mode=hidden` after it has relaxed the Hidden SCM enforcement required for that Project/profile.

### Normal → Hidden

1. Persist transition intent/phase while the effective mode remains `normal`.
2. Apply only Harness-owned Hidden projection/enforcement changes.
3. Complete ADR-0004/0005 revocation or revalidation proof for every relevant current or potentially pre-recovery Normal-capability context.
4. Verify the complete Hidden host/profile contract.
5. Only then atomically persist `visibility_mode=hidden` as the security commit point.
6. Clear/finalize the transition record after any non-security cleanup.

A crash before step 5 leaves the durable effective mode `normal`. Provisional Harness-owned hardening may temporarily make the host more restrictive, but recovery must compensate/reconcile it to Normal before the transition is reported complete. It must never infer Hidden merely from partially installed policy.

### Hidden → Normal

Relaxation uses the opposite commit order:

1. Persist transition intent/phase.
2. Atomically persist `visibility_mode=normal` **before** removing or weakening any Harness-owned Hidden SCM enforcement.
3. Remove/restore Harness-owned Hidden enforcement, attribution suppression, projections, and settings using ownership-aware compensation.
4. Clear the transition record only after restoration succeeds.

A crash after step 2 may leave a Normal Project temporarily over-restricted by residual Hidden policy. Recovery resumes exact cleanup. This is preferable to the unsafe inverse state (`hidden` reported while enforcement has already been removed).

The operator transition action is not reported as fully complete until cleanup/restoration finishes, even though the model-visible effective enum may already be `normal` during a pending cleanup. `normal` does not promise that every host capability is available; host/user permissions and temporary recovery restrictions can still deny an operation.

### Recovery

On daemon startup, incomplete visibility-transition records are reconciled before Harness performs any new mode transition for the same Git common directory.

- pending `normal → hidden` before the security commit point reconciles/compensates toward durable `normal` unless the transition can be safely resumed through the full ADR-0004/0005 proof;
- pending `hidden → normal` with durable `normal` completes removal/restoration of Harness-owned Hidden policy;
- durable `hidden` without a committed transition to Normal must still satisfy the existing Hidden failure/recovery contract: supported host policy keeps agent SCM publication denied while `harnessd` is unavailable.

Transition records, backups, and ownership manifests are internal recovery state. `AgentSession` remains diagnostic and is not used as authorization or runtime-liveness truth.

## Consequences

### Positive

- There is no crash window where Harness durably reports Hidden after it has deliberately relaxed the enforcement that makes Hidden true.
- External host configuration and SQLite state have an explicit recovery protocol instead of pretending to share one transaction.
- Crashes may cause temporary over-restriction, but not silent publication authority under an effective Hidden mode.

### Costs

- Visibility changes need a small durable transition journal/state machine in addition to the two-value Project policy.
- `hidden → normal` can expose `normal` before Harness has finished removing residual restrictions; operator UI must distinguish effective mode from transition completion/recovery status.
- Adapter restoration remains ownership-aware and idempotent.

## Verification

Core/integration tests must inject a crash after every transition phase and prove:

- no crash point can produce durable `visibility_mode=hidden` after required Hidden enforcement was removed or weakened;
- `normal → hidden` cannot persist Hidden before ADR-0004/0005 revocation proof succeeds;
- a pre-commit Normal → Hidden crash recovers to/reconciles with durable Normal without leaking a third model-visible mode;
- `hidden → normal` persists Normal before relaxing Hidden enforcement, and a crash during cleanup resumes idempotent restoration;
- transition records serialize with admissions/transitions for the same Git common directory;
- cleanup/compensation changes only Harness-owned policy and preserves unknown user configuration;
- `AgentSession` is not used as the recovery source of truth.

Real-host acceptance for each transition-capable profile must repeat representative crash/restart points around its actual host-policy mutation path and prove that no effective Hidden state permits staging, commit, push, or equivalent remote-SCM publication.

## Relationship to earlier ADRs

ADR-0003 remains authoritative for Normal/Hidden semantics. ADR-0004 remains authoritative for transition revocation/serialization, and ADR-0005 for recovery-safe proof of old-context invalidation. This ADR refines their external-side-effect commit ordering and crash recovery. Where ADR-0004 says `hidden → normal` changes effective mode only after restoration succeeds, this ADR supersedes that ordering: durable/effective Normal is the security commit point **before** any relaxation, while the operator transition remains incomplete until restoration finishes.
