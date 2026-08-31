# ADR-0038: Require a Harness Task before diagnosis and treat schema errors as blockers

- **Status:** Accepted
- **Date:** 2026-08-31
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
4. A new operator request or a shift from diagnosis to implementation completes or waits the
   current Task, then starts a new one. The one-working-Task invariant is unchanged.
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

## Verification

- MCP and Codex bootstrap tests prove the new phrases, reject the changes-only ritual in current
  bodies, and keep the 1 KiB / first-512 contracts;
- unknown-argument tests prove allowed field names appear, unknown names do not, and retry text
  is present;
- `task_start` description tests prove create-with-title-only and diagnosis-before-edits wording;
- checkout `AGENTS.md` bootstrap tests prove diagnosis, retry, and stage-checkpoint requirements.
