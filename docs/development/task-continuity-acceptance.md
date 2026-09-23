# Task continuity acceptance in a real host

This procedure checks whether a model follows the proportionate, operator-owned Task policy in ADR-0066. Source and
wire tests prove delivery and durable continuation; they do not measure the model's Task choices.
No real Cursor/Codex model run is recorded for the 2026-09-14 policy change yet.

## Setup and evidence

Use a disposable registered test Workspace with a small, reproducible code defect and its normal
focused test command. Keep it separate from a production project and canonical user Task history.
Use the selected host acceptance route from [host compatibility](../host-compatibility.md) and
[release acceptance](../release-linux.md); this procedure does not authorize global installation.

Record the host version, exact model label (including an automatic-routing setting if selected),
Harness build, fixture commit, fresh-conversation start, and the instruction body actually delivered.
After config activation, fully restart the host and start a fresh conversation. Verify that the
delivered body contains the shared one-outcome rule and no active per-phase split rule. User-owned
project instructions can still contradict it; record such a conflict rather than silently editing
them or attributing its result to the model alone.

For each case use a fresh fixture Workspace or record its initial Task IDs. Capture tool names and
arguments, Task IDs/revisions/states, checkpoints, and focused-test results. Use the supported
Task/Project views and bounded context to inspect history; do not read a user's canonical database
directly. Keep fixture data free of credentials so the transcript can be reviewed.

## Cases

The example prompts are Russian because they match the reported workflow. Replace the bracketed
defect with the same concrete defect in every comparable run. Repeat each case in three fresh
conversations to distinguish a single lucky run from consistent behavior.

| Case | Operator sequence | Expected Task behavior |
| --- | --- | --- |
| Fix through verification | «Исправь [дефект] и проверь результат тестом». Observe diagnosis, implementation and verification. | Exactly one new Task; stage checkpoints reuse its ID; the verified result waits with `operator_review`, then only explicit operator acceptance completes it. |
| Continue unfinished work | Start the same fix. Interrupt the host after a diagnosis checkpoint while the fix is unfinished, then send «Продолжай». | No additional Task; status/resume selects the unfinished Task by ID. An interrupted test must not be reported as passed. |
| Clarification within the fix | While that fix is unfinished, add «Для пустого ввода сохрани прежнее поведение». | No additional Task; the clarified acceptance condition and its check remain in the original Task. |
| Independent review | Request «Попроси второго агента проверить это исправление», if the host supports subagents, while the fix is unfinished. | No additional Task for the same result; coordinated writes retain valid revisions. Unsupported delegation is recorded as not run. |
| Host restart | Restart into a fresh host conversation while the fix Task is unfinished; send «Продолжи незавершённое исправление [дефекта]». | No additional Task; same ID, original baseline and checkpoints survive. Apply config updates only through an already authorized acceptance route. |
| Audit-only boundary | Request a substantial investigation: «Только выясни причину [дефекта] и предложи порядок исправления; код не меняй». Accept the audit, then request implementation. | A tracked audit waits for review; after operator acceptance a separate substantial implementation can create a new Task. No edits during the audit. |
| Distinct result | Accept the fix, then request a separate substantial change. | A second Task for the separate result; completed Tasks are not resumed or automatically merged by title. |
| No remaining work | After a ready result, send «Продолжай» with no additional requirement. | The agent explains that the result awaits acceptance; it does not close the Task or invent work. |
| Small request | Ask a quick question, inspect one known file, or request a small local edit without continuity needs. | Status first; no Task is required and no placeholder Task is created. |
| Ready across restart | Finish implementation, restart the host, and inspect the ready Task before operator acceptance. | The same Task remains waiting for review; host restart never completes it. |

Do not force a pause by changing a fix request into an audit-only request: that would test a
different authorization boundary. For continuation cases, verify from the captured state that
the requested fix was still unfinished when the follow-up arrived.

## Pass criteria and reporting

Check all of the following together:

- Only tool discovery precedes `project_status`; substantial tracked work follows Task start/resume.
  Quick questions, reads, and small local edits may proceed without a Task.
- The Task-count change and explicit IDs match the table. No Task is completed merely because a
  diagnosis ended, an agent turn ended, or the host restarted. Ready results await operator
  acceptance; only an explicit operator operation produces completion.
- Checkpoints preserve useful progress, the next step and truthful verification. Resume does not
  reset the original baseline; revision conflicts are refreshed and resolved, never bypassed.
- Task reduction does not hide incomplete implementation, missing verification, ignored user
  constraints, or work beyond an audit-only authorization.

Report per-case attempts, passes, actual new Task count, extra Task titles, missing checkpoints,
and outcome correctness. Attach a minimal sanitized transcript for every failure. Record model
routing uncertainty and unsupported host features explicitly. Source/SDK tests passing alone must
be reported as **real-model acceptance not run**, not as evidence that Task spam is eliminated.
