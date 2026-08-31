# ADR-0029: Quality discipline is a compact built-in skill pack plus durable verification

## Status
Accepted.

## Context
Harness already owns durable Task state, Knowledge, stack-aware skill selection, host-native projection, and compact MCP tools. A reusable agent-rules kit supplied useful engineering discipline: specification audit, independent review, testing/security/infrastructure guidance, anti-duplication project conventions, and explicit verification. The kit also carried its own `PROJECT_STATUS.md`, epic directories, trigger-generation scripts, and host-specific rule mirrors.

Importing that structure literally would create a second source of Task truth, duplicate Harness projection, increase model-visible instructions, and move Harness toward an orchestration/workflow DSL. MCP responses are bounded but native hosts still own model inference and final chat output, so Harness can shape response economy through instructions but cannot honestly enforce a final-output token cap without becoming a model proxy.

The product specification already defines checkpoint verification as `name`, `status`, `evidence`, with `passed`, `failed`, and `not_run`, and allows `agent_reported` as the v1 source.

## Decision
1. Harness ships a compact built-in quality pack as product-owned seed content for the canonical
   external skill registry. The first implementation contained 12 skills; that count is not a
   product invariant. The separate model-visible resolver budget remains authoritative.
2. Built-in skills use hyphenated portable ids/frontmatter and existing `task_hints`; Harness does not add a composition DSL.
3. `harness install` reconciles the pack before host/runtime mutation. `harness skills sync` provides explicit reconciliation and `harness skills validate` checks every currently supported host surface.
4. Reconciliation never overwrites unknown same-id content. A registry-root ownership manifest records exact installed hashes; owned skills update only while current bytes still match the recorded hash. Exact current built-ins may be adopted. Failures roll back in-process replacements.
5. Harness does not import `PROJECT_STATUS.md`, `STACK.md`, epic state, trigger wrappers, or host rule mirrors. Task/checkpoint state remains in Harness; mechanically detectable stack facts remain in the Structural Index; only non-mechanical project conventions belong in durable Knowledge.
6. Checkpoint verification becomes durable schema state. A checkpoint may carry bounded ordered `agent_reported` entries with `name`, `status`, and `evidence`, written atomically with Task revision/checkpoint/Knowledge/event. `project_status` returns compact name/status; selected Task context may return bounded evidence/source.
7. Operator chat follows a response-economy contract: checkpoints own durable continuity; chat is only the human-relevant delta. Server instructions require governing-contract inspection before risky cross-boundary implementation and independent complete-change review plus repository gates before publication.
8. Response economy remains a soft host instruction. Harness does not add a model proxy or claim a hard final-output token limit.

## 2026-08-30 amendment: stack depth through progressive references

The initial flat 12-skill pack was broad enough for general discipline but too shallow for reliable
Docker environment design, Google/Yandex discoverability, language-native engineering, project
architecture, legacy preservation, and durable data integrity. Encoding every language in the
entrypoint would spend model context on irrelevant stacks; one skill per language would consume the
visible-skill budget in polyglot repositories.

Built-in skills may therefore carry portable nested `references/`. The entrypoint must route to the
smallest relevant reference set, and canonical reconciliation owns and verifies the complete nested
tree. The canonical built-in count may grow independently of the configured visible subset (about
12 or fewer by default). Existing stack detection and `task_hints` remain the only composition
mechanism; this amendment does not introduce a workflow DSL or a second source of project state.

## 2026-08-30 amendment: contextual stack facets and secure-by-design coverage

Flat dependency union is insufficient for project-role selection. Expo/React Native intentionally
ships React and web-compatibility dependencies such as `react-dom`; treating any of those tokens as
proof of a public web frontend projects irrelevant SEO/DOM guidance into a native application. The
same ambiguity appears in polyglot monorepos, where raw dependencies remain useful but package
locality determines whether a role is mobile, web, backend, or another surface.

The detected stack therefore gains deterministic derived `facets`. Facets are computed from indexed
source/config paths and dependencies parsed per manifest, then aggregated for the Workspace. Initial
facets cover software projects, web frontends, mobile apps, backend services, database-backed code,
Godot projects, containers, CI pipelines, and deployment operations. Portable `harness.yaml` may
match these facets alongside languages, dependencies, and manifests. Facet matches rank ahead of
broader raw stack signals but below explicit inclusion and Task intent. This is derived relevance
metadata, not a workflow/composition language.

The built-in pack adds focused mobile, server, Godot, and deployment-operations skills and expands
language coverage for GDScript, shell, Vue, and Svelte. Composer dependencies become part of stack
detection. `public-frontend` is selected by the `web-frontend` facet or unambiguous web Task intent;
generic `react`, `frontend`, and React DOM tokens no longer select it.

The pack also adds one broadly selected `secure-by-design` entrypoint for detected software projects.
It progressively routes to security architecture, web/backend, browser, mobile,
infrastructure/supply-chain, and verification references. The baseline follows current OWASP
ASVS/MASVS and NIST SSDF control families while requiring system-specific threat modeling and
evidence. It explicitly does not promise an unhackable system or authorize production/security-test
side effects.

## 2026-08-30 amendment: Task-focused projection and discriminating discovery

Workspace stack evidence is intentionally broad. In a mobile-plus-backend repository it proves that
both surfaces exist, but it does not prove that every Task needs both playbooks. Merely ranking Task
matches ahead of stack matches still projects the whole pack whenever the visible budget is not
exhausted, leaving native hosts to discover many unrelated constitutions.

When at least one non-excluded skill recognizes the current Task `stack_hints`, recognized Task
intent becomes a focus boundary. Resolution keeps task-matched skills and explicit inclusions, and
does not fill the remaining budget with stack-only skills from unrelated Workspace surfaces. If no
skill recognizes the hints, stack resolution remains the safe fallback so novel or incomplete hints
cannot empty the projection. `task_start` instructions ask agents to pass only affected technologies
and work kinds; the mechanism remains the existing bounded `stack_hints`, not a new workflow DSL.

Portable built-in descriptions state both capability and activation boundary. This preserves a
second, host-native progressive-disclosure layer when several genuinely relevant skills remain
projected. Detailed language references remain canonical portable resources, but the entrypoint tells
the host agent to read only the references for languages crossed by the current change.

## 2026-08-30 amendment: frontend design quality is a surface invariant

Functional frontend guidance did not give weaker models a sufficiently explicit visual decision
process. Public web work received discoverability, semantics, accessibility, and performance rules,
while native mobile work received platform/lifecycle rules; neither surface guaranteed a coherent
art direction, non-templated composition, or rendered visual review.

The built-in pack therefore adds `frontend-design` for both `web-frontend` and `mobile-app` facets.
Its Task hints cover the public-web and mobile surface hints so Task-focused projection cannot retain
one of those surface skills while silently dropping design guidance. Explicit project exclusions
remain authoritative. The entrypoint is intentionally executable by weaker models: inspect the
incumbent system, settle one compact design contract, load only the applicable marketing/editorial
or product/mobile reference, implement from semantic tokens and complete states, then run one batched
visual inspection and at most one confirmation pass. Named anti-patterns reject unjustified model
defaults without turning valid brand choices into universal bans.

## 2026-08-31 amendment: Task ritual is diagnosis-inclusive

ADR-0038 supersedes the specification §71 reading that a Harness Task starts only before
meaningful changes. Compact models treated diagnosis, feed inspection, and other read-only work as
exempt, then skipped retry after a schema error. Always-on instructions and unknown-argument
errors now require Task start/resume before diagnosis, schema retry without echoing unknown field
names, and a checkpoint after each logical stage. Compliance remains host acceptance evidence.

## Consequences
- Useful discipline becomes portable and host-neutral without a giant always-on rules prompt.
- High-precision facets prevent ambiguous ecosystem dependencies from selecting the wrong surface
  skill while preserving multi-surface monorepo coverage.
- Recognized Task hints focus polyglot projection while explicit inclusions and the existing
  visible-skill budget remain authoritative; unknown hints fall back to stack evidence.
- Verification survives host/session changes and need not be repeated verbatim in chat.
- User-modified or same-id custom skills fail closed instead of being silently replaced.
- Recognized user-facing frontend work receives one portable design-quality baseline across web and
  mobile without merging SEO, native delivery, and visual-design concerns into one giant skill.
- Future observed hooks may add `source=observed` without changing the v1 agent-reported shape.
- Real-host compliance with response instructions remains acceptance evidence, not an enforcement claim.
