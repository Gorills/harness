# ADR-0038: Require a Harness Task before diagnosis and treat schema errors as blockers

- **Status:** Accepted
- **Date:** 2026-08-31
- **Amended:** 2026-09-01, 2026-09-02, 2026-09-11
- **Amends:** specification §71, [ADR-0029](0029-quality-discipline-verification-and-response-economy.md)

## Context

Specification §71 and the first always-on bootstrap texts told agents to start or resume a Harness
Task before *meaningful changes* and to checkpoint *meaningful completed or waiting work*. Compact
models, including Cursor Composer, read that as a license to skip Task for read-only diagnosis,
treat one failed `task_start` as "Harness unavailable", and continue with native tools. Observed
failures also mixed diagnosis and implementation in one session without a Task boundary, and
omitted checkpoints after investigation that produced no diff.

Unknown-argument rejection named neither the extra fields nor the public allowed fields. That
protects negative disclosure of caller-controlled names, but the remaining `"Unknown tool argument
fields"` message did not tell a weak model to retry with `title` only. Agents that passed
`summary` or a placeholder `task_id` therefore abandoned the ritual.

Harness still cannot proxy the host model. Instruction and error-text changes remain soft, as in
ADR-0029. The written contract must not itself supply the skip.

## Decision

1. Always-on MCP, Codex, and checkout bootstrap instructions require `task_start` / resume before
   diagnosis and edits, including read-only investigation. "Before changes" is not an exemption.
2. A failed Harness tool or schema call is a blocker. The agent must read the public tool schema
   and retry. Native work without a Task is not a fallback.
3. `task_checkpoint` is required after each logical stage, including diagnosis with no code
   change, not only at completed or waiting work.
4. One requested user outcome owns one Task across diagnosis, implementation, checks and
   follow-ups. Start a new Task only for a distinct requested outcome. The 2026-09-11 amendment
   below supersedes the original per-request/per-phase boundary; the one-working-Task invariant
   is unchanged. An explicit operator discussion waiver is not a work request.
5. Unknown-argument errors list the tool's public allowed fields and tell the caller to retry.
   They still do not echo unknown field names. New `task_start` calls take `title` and optional
   `stack_hints` only; `summary` belongs on `task_checkpoint`, and `task_id` is omitted when
   creating.
6. Server instructions stay under the existing ~1 KiB budget with operator-facing Russian tokens
   in the first 512 bytes. This remains a soft host instruction, not a model proxy.

## Consequences

- Weaker models lose the written "diagnostics need no Task" reading.
- Schema mistakes become retryable from the error text without disclosing attacker-controlled
  field names.
- Real-host compliance is still acceptance evidence. Closing the loophole does not guarantee
  every Composer session will follow it.
- Previously generated Codex `developer_instructions` that used the changes-only bootstrap remain
  owned and are reconciled to the current body.

## 2026-09-01 amendment: one canonical MCP workflow

Always-on MCP, Codex, and checkout bootstrap texts previously allowed two readings after
`project_status`: search then Task, and Task then search. The second is the only allowed order.
`project_context` was also written as a mandatory ritual before native reads; for code and doc
refs that already include an exact path, that call is optional metadata verification.

Canonical sequence:

1. `project_status`
2. `task_start` or explicit Task resume
3. `project_search` before broad native repository exploration
4. `project_context` only for selected refs when it adds semantic information
5. targeted native source tools
6. `task_checkpoint` after each logical stage

Knowledge and Task refs still use `project_context` for selected semantic context. Tool discovery
before `project_status` remains allowed when the host needs it to call Harness. This remains a
soft instruction, not a model proxy. Previously generated Codex `developer_instructions` that
used the search-then-task bootstrap remain owned and are reconciled to the current body.

## 2026-09-02 amendment: complexity is not a Task skip

"Before meaningful changes" already taught compact models to skip Task. A second reading now
does the same: the agent judges the work small, the path obvious, or the operator tired of
ceremony, and continues without a Task. `project_search` friction is a real cost, but skipping
search is not a license to skip Task.

Always-on MCP and Codex instruction bodies stay fail-closed and must not describe a discussion
waiver (that text is a skip the contract would itself supply). They must say:

- do not skip Task because work looks small or the path is known;
- `project_search` is required before broad native exploration;
- an already-known exact path may skip search, not Task.

Checkout `AGENTS.md` may add an operator-explicit discussion waiver. The waiver applies only
when the operator said so for that phase, forbids diagnosis/edits/broad exploration while it
holds, and ends at the next implement, fix, or investigate request. Doubt without a waiver
starts a Task. Doubt under a waiver does not stretch into work.

Forbidden skip reasons remain: small diff, known path, unhelpful search, operator annoyance,
previous discussion, or creating a Task after finishing.

## 2026-09-11 amendment: one outcome, one durable Task

The original decision 4 required a new Task for each operator work request and for a shift from
diagnosis to implementation. That wording itself fragmented one authorized result across Tasks:
"fix this problem", an investigation checkpoint, "continue", and verification could each acquire
a separate Task. The domain already supported continuation across processes; the bootstrap policy
worked against that ability. The [utility audit](../audits/2026-09-11-agent-and-human-utility.md)
records this instruction-level cause. It does not establish how often a particular model follows
it.

The corrected boundary is the requested outcome:

- Resume the relevant unfinished Task from `project_status` by explicit ID. Diagnosis, edits,
  checks, clarifications, "continue", and subagents serving that outcome share it. Individual
  messages and tool calls do not each create a Task.
- Checkpoint each logical stage. Intermediate diagnosis or the end of an agent turn is not
  completion when authorized work toward the same outcome remains. Keep that Task `working`,
  or `waiting` for a real dependency with its reason. Mark it `completed` only when the requested
  outcome is done.
- A distinct requested outcome starts a new Task after existing work has been completed or
  checkpointed to `waiting` with its reason. A working checkpoint alone does not free the
  Workspace: the daemon still refuses a second working Task there.
- Respect authorization boundaries: "audit only" may end in a completed audit. A later request
  to implement its findings is a new deliverable. This policy does not authorize implementation
  during an audit-only request or reopen a completed/cancelled Task through `task_start`.
- A host restart or a fresh host conversation is not a new requested outcome. After actual
  Harness config reconciliation, fully restart the host, open a fresh conversation, call
  `project_status`, and resume the same unfinished Task by ID. Source edits in a checkout do not
  activate generated configs.

The compact MCP and Codex instruction bodies share a host-neutral outcome-boundary constant.
The `task_start` and `task_checkpoint` descriptions carry the longer resume/state guidance.
Historical exact Codex instruction bodies remain recognized as Harness-owned, including their
Normal and Hidden compositions; reconciliation can upgrade those artifacts without claiming
unknown user-owned text. Current instructions retain status-first discovery, Task-before-diagnosis,
schema retry, search fallback, and the 1 KiB bootstrap budgets. Search ordering follows the later
natural-language discovery rule in [ADR-0021](0021-project-intelligence-retrieval.md): the earlier
amendments' unconditional search step is not reinstated here.

No Task schema or API changes are needed. There is no title-based automatic merge, implicit write
target, or protocol-session identity. Every existing-Task mutation still checks explicit `task_id`
and `expected_revision`; terminal reopening remains a separate human operation. Subagents must
coordinate checkpoint writes against the latest revision rather than bypassing that condition.

The host/domain distinction also follows Codex's documented startup instruction discovery:
[AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md) describes a
chain assembled at startup, while [config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
defines session developer instructions and trusted project configuration. Neither host behavior
defines Harness Task identity.

Automated verification checks delivered instructions, ownership upgrades, fixed exposure budgets,
and continuation through independent real MCP processes: diagnosis → implementation → clarification
and verification → waiting → restart/resume → completion preserves one Task and its original Git
baseline. It also checks persisted checkpoints and verification. This proves the contract and
domain mechanics, not that a weak Cursor model chooses the right boundary. That claim requires the
separate [model acceptance scenarios](../development/task-continuity-acceptance.md), with model and
host versions recorded. Real-model Task-fragmentation acceptance has not been run for this change.

## Verification

- MCP and Codex bootstrap tests prove the new phrases, reject the changes-only ritual in current
  bodies, and keep the 1 KiB / first-512 contracts;
- current instruction bodies prove a unique status → task → search order and reject
  status → search → task wording;
- `project_search` / `project_context` descriptions prove targeted native read after a code/doc
  path hit and that `project_context` is not mandatory for those kinds;
- MCP `_SERVER_INSTRUCTIONS` (Cursor always-on initialize / `namespaceUseInstructions`) prove the
  same code/doc native-read exemption; `test_cursor_bootstrap_matches_canonical_workflow` covers
  that MCP surface, not checkout `AGENTS.md`;
- unknown-argument tests prove allowed field names appear, unknown names do not, and retry text
  is present;
- `task_start` description tests prove create-with-title-only, diagnosis-before-edits wording,
  and that small or known-path work is not a skip;
- checkout `AGENTS.md` bootstrap tests prove diagnosis, retry, stage-checkpoint, the same
  canonical sequence, the search-is-not-Task-skip rule, forbidden skip reasons, and that an
  operator discussion waiver is phase-scoped and ends on implement/fix/investigate;
- always-on MCP/Codex bodies prove the small/known-path skip prohibition, stay under 1 KiB,
  and do not contain discussion-waiver license text.
