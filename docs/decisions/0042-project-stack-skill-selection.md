# ADR-0042: Project skills are selected from the Workspace stack, not the Task

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0032](0032-continuous-project-skill-reconciliation.md),
  [ADR-0029](0029-quality-discipline-verification-and-response-economy.md)
- **Supersedes:** Task-specific skill selection in ADR-0032 (relevance-key enqueue after Task
  mutation) and task-selected filesystem delivery in
  [ADR-0041](0041-task-skill-session-delivery.md). MCP still does not deliver skill bodies or
  treat `recommended_skills` as instruction delivery.

## Context

The resolver mixed detected project stack with the current Task's `stack_hints`. Recognized Task
hints then dropped unrelated stack-only skills, and Task mutations compared a skill-relevance key
to enqueue native projection repair. Hosts already choose among discovered Skills from
name/description. Rotating the project pack on Task lifecycle duplicated that job, emptied
polyglot baselines, and made mid-session `task_start` look like skill injection.

## Decision

1. Harness selects the project-visible Skill pack from the detected Workspace stack plus existing
   explicit include/exclude. It does not read the relevant Task and does not rank or narrow on
   Skill metadata `task_hints` or Task `stack_hints`.
2. Task `stack_hints` remain optional durable Task metadata for Dashboard, history, and inspection.
   They are not a Skill selector. Agents may omit them.
3. Task create/resume/checkpoint and dashboard Task actions do not enqueue skill reconciliation.
   Watcher/scan reconciliation continues after authoritative project/index changes
   (ADR-0032 Decision 2 and Decision 4).
4. The host receives the stable project-visible pack under `.agents/skills`. Host-native
   selection chooses which Skill to use. Harness does not own current-session Skill-body
   delivery and does not add MCP skill fields or tools.

Skill metadata `task_hints` remains accepted parser input so existing custom registry Skills keep
loading. It is ignored at resolve time.

## Consequences

- A FastAPI+Expo Workspace keeps both surfaces in the projected pack while Tasks change.
- Greenfield Task hints no longer activate Skills before stack evidence exists.
- Built-ins that currently match only via `task_hints` stay unloaded until a later catalog pass
  gives them stack applicability or retires them.
- Projection safety, registry trust, and Git `info/exclude` are unchanged.

## Verification

Automated tests must prove:

- Task start, checkpoint, and terminal transitions leave `resolve_workspace_skills` unchanged;
- dashboard Task actions do not enqueue watcher skill reconciliation;
- adding real stack evidence (for example a Dockerfile) may change the resolved pack after scan;
- MCP tools and structured responses still omit `skill_body` and `recommended_skills`;
- `project_context` still rejects skill refs.
