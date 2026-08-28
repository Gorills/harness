# ADR-0029: Quality discipline is a compact built-in skill pack plus durable verification

## Status
Accepted.

## Context
Harness already owns durable Task state, Knowledge, stack-aware skill selection, host-native projection, and compact MCP tools. A reusable agent-rules kit supplied useful engineering discipline: specification audit, independent review, testing/security/infrastructure guidance, anti-duplication project conventions, and explicit verification. The kit also carried its own `PROJECT_STATUS.md`, epic directories, trigger-generation scripts, and host-specific rule mirrors.

Importing that structure literally would create a second source of Task truth, duplicate Harness projection, increase model-visible instructions, and move Harness toward an orchestration/workflow DSL. MCP responses are bounded but native hosts still own model inference and final chat output, so Harness can shape response economy through instructions but cannot honestly enforce a final-output token cap without becoming a model proxy.

The product specification already defines checkpoint verification as `name`, `status`, `evidence`, with `passed`, `failed`, and `not_run`, and allows `agent_reported` as the v1 source.

## Decision
1. Harness ships a compact 12-skill built-in quality pack as product-owned seed content for the canonical external skill registry.
2. Built-in skills use hyphenated portable ids/frontmatter and existing `task_hints`; Harness does not add a composition DSL.
3. `harness install` reconciles the pack before host/runtime mutation. `harness skills sync` provides explicit reconciliation and `harness skills validate` checks every currently supported host surface.
4. Reconciliation never overwrites unknown same-id content. A registry-root ownership manifest records exact installed hashes; owned skills update only while current bytes still match the recorded hash. Exact current built-ins may be adopted. Failures roll back in-process replacements.
5. Harness does not import `PROJECT_STATUS.md`, `STACK.md`, epic state, trigger wrappers, or host rule mirrors. Task/checkpoint state remains in Harness; mechanically detectable stack facts remain in the Structural Index; only non-mechanical project conventions belong in durable Knowledge.
6. Checkpoint verification becomes durable schema state. A checkpoint may carry bounded ordered `agent_reported` entries with `name`, `status`, and `evidence`, written atomically with Task revision/checkpoint/Knowledge/event. `project_status` returns compact name/status; selected Task context may return bounded evidence/source.
7. Operator chat follows a response-economy contract: checkpoints own durable continuity; chat is only the human-relevant delta. Server instructions require governing-contract inspection before risky cross-boundary implementation and independent complete-change review plus repository gates before publication.
8. Response economy remains a soft host instruction. Harness does not add a model proxy or claim a hard final-output token limit.

## Consequences
- Useful discipline becomes portable and host-neutral without a giant always-on rules prompt.
- Task hints compose relevant skills while the existing visible-skill budget remains authoritative.
- Verification survives host/session changes and need not be repeated verbatim in chat.
- User-modified or same-id custom skills fail closed instead of being silently replaced.
- Future observed hooks may add `source=observed` without changing the v1 agent-reported shape.
- Real-host compliance with response instructions remains acceptance evidence, not an enforcement claim.
