# ADR-0044: Project Skill projection has no count cap

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0042](0042-project-stack-skill-selection.md)
- **Supersedes:** The model-visible Skill count budget in ADR-0042, `ARCHITECTURE.md` §14,
  `docs/specification.md` §73.1 and §81. Response-size budgets for MCP payloads are unchanged.

## Context

ADR-0042 correctly moved Skill selection from Task hints to detected Workspace stack and host-native
selection, but retained a count-based model-visible budget. That budget can discard a Skill that is
relevant to the detected project merely because other relevant Skills rank ahead of it. In a
polyglot project this means Harness can detect a real surface such as observability and then fail to
project its matching Skill.

This conflicts with the native-first responsibility boundary: Harness should decide which Skills are
relevant to the project, materialize every relevant Skill, and let the host choose which one to read
for the current request. A count cap duplicates host selection without improving project relevance.

## Decision

1. Harness projects every Skill that matches the detected Workspace stack or is explicitly included,
   except Skills explicitly excluded by project policy.
2. There is no count-based visibility budget and no `SkillResolutionPolicy` for truncating matching
   Skills.
3. Resolution remains deterministic. Match strength and explicit inclusion may order the returned
   set, but ordering never removes a matching Skill.
4. Task metadata remains outside Skill selection. The host continues to choose among projected
   Skills by native name/description discovery.
5. MCP response-size limits and other bounded context contracts are unchanged; projected Skill files
   are host-native filesystem resources, not an MCP payload dump.

## Consequences

- A project cannot lose a relevant built-in merely because its detected stack is broad.
- Adding future built-ins does not require retuning a global visibility number.
- Explicit include/exclude remains available for deliberate project policy rather than accidental
  ranking pressure.
- Harness owns relevance and delivery only; it does not add task-time orchestration, model routing,
  or a second Skill-selection layer.

## Verification

Existing resolver tests should assert deterministic all-match resolution, explicit include/exclude,
and stack-driven relevance. Built-in fixture tests should prove that a busy polyglot keeps every
matching surface instead of asserting truncation. No model-evaluation framework is introduced by
this decision.
