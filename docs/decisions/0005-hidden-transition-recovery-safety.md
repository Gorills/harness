# ADR-0005: Hidden transition proof must survive daemon recovery

- **Status:** Accepted
- **Date:** 2026-08-21
- **Deciders:** Repository architecture baseline
- **Refines:** ADR-0004 (mode-transition revocation proof)

## Context

ADR-0004 correctly requires `normal → hidden` to revoke or revalidate every already-admitted Normal-capability execution context before Hidden becomes effective. Its wording still leaves one recovery ambiguity: a proprietary host process can outlive `harnessd`.

If `harnessd` crashes or restarts, volatile admission bookkeeping may disappear while the old host execution context remains alive with staging, commit, push, or remote-SCM authority. A restarted daemon must not infer “there are no old admissions” from an empty in-memory registry and then publish `hidden`.

`AgentSession`/activity records are diagnostic history and are not authoritative host-runtime liveness. The correction therefore cannot turn them into a security identity or pretend Harness owns the host lifecycle.

## Decision

ADR-0004's transition barrier is **recovery-safe**.

For `normal → hidden`, every execution context that could still retain Normal SCM-write authority is treated as potentially live until the selected host/profile provides acceptance-proven evidence that the old capability is gone. Daemon restart, bridge restart, missing volatile session state, or absence of recent activity is never sufficient proof.

A host/profile may satisfy recovery-safe transition proof only through a mechanism whose scope covers all potentially pre-existing execution contexts for that profile, for example:

- authoritative host enumeration plus revocation/revalidation covering every relevant live context;
- explicit host-enforced all-context capability revocation;
- an enforced process/session restart or project-reopen boundary that invalidates every potentially pre-existing privileged context.

If none of those mechanisms is available or verifiable, `normal → hidden` fails closed. The effective mode remains `normal` and Harness returns an actionable restart/reopen or unsupported-transition result. It must not publish `hidden` merely because the restarted daemon has forgotten prior admissions or because future admissions would be safe.

This does not require Harness to own a new agent runtime. It makes the host/profile's revocation scope part of `mode_transition_safety` and keeps uncertainty fail-closed.

## Consequences

### Positive

- `harnessd` recovery cannot silently weaken Hidden's SCM guarantee.
- Volatile `AgentSession`/bridge bookkeeping remains diagnostic instead of becoming a false security authority.
- Hosts with authoritative all-context revocation can transition live; hosts without it can require a real restart/reopen boundary.

### Costs

- Some transitions may require more conservative restart/reopen behavior after daemon recovery.
- Host acceptance must test transition safety across daemon restart, not only within one uninterrupted daemon lifetime.

## Verification

Core/integration tests must prove:

- volatile admission/session state is not accepted as proof that a prior Normal-capability context is gone;
- after simulated daemon recovery, a `normal → hidden` transition remains blocked until the profile supplies recovery-safe revocation evidence;
- transition failure keeps the previous effective mode and does not expose a half-applied Hidden policy;
- the correction does not make `AgentSession` authoritative for authorization, routing, or host-runtime liveness.

Real-host acceptance for every profile advertised as transition-capable must additionally prove:

- start a Normal-capability host context, restart `harnessd` while that host context remains usable, then request Hidden;
- Hidden becomes effective only after the old context is authoritatively revoked/revalidated or invalidated by the required host restart/reopen boundary;
- after `project_status` reports `hidden`, the pre-recovery execution context cannot stage, commit, push, or perform an equivalent remote-SCM mutation;
- if the host cannot prove all-context revocation after daemon recovery, Harness remains Normal and reports the required operator action instead of silently downgrading the guarantee.

## Relationship to ADR-0004

ADR-0004 remains authoritative for serialization, transition ordering, rollback, and live capability revocation. This ADR refines only the evidence boundary across `harnessd` recovery: absence of volatile admission state is never evidence of capability revocation.
