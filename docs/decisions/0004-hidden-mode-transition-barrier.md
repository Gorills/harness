# ADR-0004: Hidden mode transitions require capability revocation

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline
- **Refines:** ADR-0003 §1 (mode transition semantics)

## Context

ADR-0003 makes `visibility_mode=hidden` fail-closed and requires every later Hidden admission to validate its host/profile. Its original transition wording, however, only required repository/projection preflight before Hidden became effective.

That is insufficient when a Normal-capability agent is already running. A previously admitted host execution context may already have staging, commit, push, or remote-SCM authority. If Harness flips the durable Project flag first and only validates future admissions, that older context can keep publishing even while `project_status` reports `hidden`.

Harness does not own the agent runtime, so it cannot assume that changing a settings file, updating `harnessd`, or restarting the MCP bridge retroactively revokes capabilities in a proprietary host. The transition itself must therefore be a verified boundary.

## Decision

Mode transitions and agent admission for all Harness Workspaces sharing one Git common directory use the same daemon-controlled serialization boundary.

### Normal → Hidden

`normal → hidden` MUST NOT become effective/public until every already-admitted Harness host/profile that could retain Normal SCM-write capability has crossed an acceptance-proven revocation/revalidation boundary.

A host/profile may satisfy this requirement only through a mechanism whose real-host acceptance proves the old capability is gone, for example:

- live policy reload/revalidation that applies to the already-running execution context;
- explicit capability revocation enforced by the host;
- an enforced process/session restart or project reopen boundary that invalidates the old execution context.

If Harness cannot prove that the previous SCM capability is gone, the transition fails closed and the effective mode remains `normal`. Harness may return an actionable `restart/reopen required` or `unsupported live transition` result, but MUST NOT publish `hidden` merely because future admissions would be safe.

Repository/projection/enforcement changes made provisionally for the attempted transition must either commit with the mode transition or be restored on failure. Admission cannot race the transition and observe an intermediate policy state.

After Hidden becomes effective, ADR-0003's normal Hidden admission checks still apply to every later bridge/agent admission.

### Hidden → Normal

`hidden → normal` uses the same serialization boundary. Harness must not remove its Hidden enforcement/projection policy underneath an already-admitted Hidden execution context before the transition/restoration boundary completes. The effective mode changes only after Harness-owned policy restoration succeeds for the affected supported profiles.

### Capability contract

ADR-0003's normalized Hidden host capability set gains one required capability:

- `mode_transition_safety`: deterministic evidence that an already-admitted execution context cannot retain the previous mode's SCM capability after a transition is declared effective.

This can be satisfied by live revocation/revalidation or an enforced restart/reopen boundary. Prompt text, a new MCP bridge process, or a future-admission check alone does not satisfy it.

## Consequences

### Positive

- `project_status=hidden` never races ahead of the actual SCM restriction for an already-admitted Harness agent.
- A host with static per-process permissions can fail closed instead of pretending a settings write changed a running process.
- Mode transitions and new admissions cannot interleave into an unverified intermediate state.

### Costs

- Some hosts may support Hidden from a fresh session but not support live Normal → Hidden switching.
- Operators may be required to restart/reopen a host before a transition can complete.
- Adapters need explicit transition capability/verification, not only admission capability.

## Verification

Core/integration tests must prove:

- mode transition and admission serialize for one Git common directory;
- a Normal → Hidden attempt does not change the effective mode while an already-admitted Normal-capability profile remains unrevalidated;
- transition failure preserves the previous effective mode and restores provisional Harness-owned changes;
- a concurrent new admission cannot observe or use a half-applied transition;
- Hidden → Normal does not remove Harness-owned enforcement before its restoration boundary completes.

Real-host acceptance for every profile advertised as transition-capable must additionally prove:

- with a Normal-capability agent already active, the supported transition mechanism revokes/revalidates its SCM authority before `project_status` can report Hidden;
- the older execution context cannot stage, commit, push, or perform an equivalent remote-SCM mutation after Hidden becomes effective;
- if live revocation is unavailable, Harness fails closed with restart/reopen required instead of changing the effective mode;
- a restart/reopen requirement cannot be satisfied merely by spawning a new MCP bridge while the old privileged execution context remains usable.

## Relationship to ADR-0003

ADR-0003 remains authoritative for the two visibility modes, projection hygiene, attribution suppression, host admission, and the repository/SCM scope of Hidden mode. This ADR refines only its mode-transition semantics. Where ADR-0003 §1 could be read as allowing the Project flag to change before an already-admitted Normal capability is revoked, this ADR controls.
