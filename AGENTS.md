# Harness engineering instructions

These rules apply to every coding agent and human contributor in this repository.

## Read before changing code

For any non-trivial change, read the relevant parts of:

1. `docs/specification.md` — original product requirements.
2. `docs/audits/2026-08-21-spec-audit.md` — corrections and host/protocol risks.
3. `ARCHITECTURE.md` — current implementation contract.
4. Relevant ADRs under `docs/decisions/`.
5. Existing implementation and tests for the subsystem being changed.

The audited architecture and accepted ADRs control implementation when the original specification depends on a protocol or host assumption that has since changed.

## Bounded workflow

- Work on one logically complete, independently verifiable task at a time.
- Do not mix unrelated refactors, cleanup, dependency changes, or feature work into the same task.
- Prefer the smallest change that fully satisfies the task and preserves architectural boundaries.
- Before implementation, verify existing APIs, invariants, tests, and external contracts; do not invent them.
- After implementation, review the diff as a critic and run checks proportionate to the changed risk.
- Never report a check as verified unless it actually ran successfully.
- If a task uncovers a larger follow-up, document it and stop at the current boundary rather than silently expanding scope.
- If normal Git remote transport is unavailable but authenticated repository-object access still exists, follow `docs/development/network-constrained-git.md`; preserve exact base/tree identity and do not move a feature branch ref away from the verified base until the complete remote tree matches the locally verified expected tree.

## Architecture invariants

- `harnessd` owns durable business state. Host adapters and the MCP bridge are thin integration layers.
- Core business logic must not depend on Claude, Codex, Cursor, or Antigravity-specific APIs.
- MCP is stateless at the protocol level for the 2026-07-28 path. Never use an MCP session identifier as a domain invariant.
- Task identity and continuity are Harness domain state. Read relevance may be workspace-scoped. Creating a new Task has no prior revision, but every mutation of an existing Task must explicitly carry `task_id` plus `expected_revision`; never infer a write target from Workspace-current state or accept stale same-Task writes.
- Harness `Task` is not the MCP `io.modelcontextprotocol/tasks` extension. Do not use protocol task handles as Harness Task IDs or lifecycle state.
- `AgentSession` records observed bridge/client activity; it is not a protocol session and is not authoritative for task identity.
- Filesystem is source of truth for code; Git is source of truth for Git state/history. Structural Index is derived data.
- Model-visible responses are bounded contracts. New fields require exposure-budget and negative-disclosure tests.
- Raw source is never bulk-embedded or sent to external services by default.
- Hooks are optional observability/enhancement only and must not be required for correctness.
- `visibility_mode=hidden` is fail-closed: agents may edit/research but must not perform durable SCM mutations, and Harness-owned project artifacts must remain untracked/ignored without changing `.gitignore` or tracked instruction files. Hidden is human-selected; model-facing tools may read the effective mode but may not change it. Hidden host admission uses Harness-owned adapter/profile identity, never self-reported `clientInfo`; unsupported profiles fail closed. Mode transitions and admissions sharing a Git common directory must serialize; never report `hidden` effective while an already-admitted Normal-capability agent can retain SCM-write authority. Do not claim Hidden enforcement from prompt text or `.git/info/exclude` alone.
- Workspaces sharing one Git common directory share one effective visibility mode in v1; do not introduce per-worktree Hidden/Normal divergence without a separately verified isolation mechanism.
- Host adapters may discover/configure hosts, resolve workspace hints, project native skills/rules/local settings, enforce supported Hidden-mode policy, clean up owned artifacts, and run host-specific doctor checks. They may not contain search/task/index/knowledge business logic.
- Use the official MCP SDK in production. Raw JSON-RPC is allowed only for independent wire-level tests.

## Host integrations

Host configuration formats and discovery paths change. Before modifying an adapter:

- Check current official host documentation.
- Keep exact paths/field names in adapter-owned code, not core domain code.
- Treat `clientInfo` as self-reported metadata, never as security identity or authoritative behavior selection.
- Do not depend on deprecated MCP roots for correctness.
- Any assumption about current-workspace propagation must have a real-host acceptance test.

## Skills

- Keep canonical Harness skills outside repositories.
- Project only the relevant subset into host-native locations.
- Do not overwrite unknown user-owned files.
- Generated files must carry ownership metadata where the host format permits it.
- Avoid duplicate visibility across hosts that scan compatibility directories, especially Cursor.
- Skill relevance, project visibility, and cleanup need acceptance tests.
- Hidden projections resolve the local exclude file with `git rev-parse --git-path info/exclude`, then use exact Harness-owned root-anchored entries; never use `assume-unchanged` or `skip-worktree` to hide tracked files.
- Before projecting Hidden artifacts, fail on tracked targets or unknown user-owned collisions.

## Tests

The implementation test strategy must include:

- unit tests for domain logic;
- integration tests for SQLite/index/search/task behavior;
- real subprocess MCP stdio wire tests;
- exact model-visible contract and negative-disclosure tests;
- response-size/budget tests;
- dashboard HTTP/SSE tests;
- synthetic repository fixtures;
- a real-host acceptance matrix kept separate from core automated proof.

## Documentation discipline

- Architectural behavior changes require an ADR or an update to an existing ADR.
- User-visible CLI/MCP/config behavior changes require documentation in the same task.
- Keep host-version uncertainty explicit. Do not turn an unverified host behavior into a core invariant.
- Do not rewrite the original specification silently. Preserve it and document amendments in the audit/ADR layer.
