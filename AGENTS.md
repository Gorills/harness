# Harness engineering instructions

These rules apply to every coding agent and human contributor in this repository.

## Isolated development

This checkout is isolated by default and must not accidentally mix checkout code with a separately
installed Harness process, database, socket, or host configuration. The only supported sharing
exception is the explicit ADR-0036 global-dogfood route, which runs the tool-installed executable
and skips checkout host/skill reconciliation.

- Run current-checkout CLI/daemon commands through `scripts/dev` (or `source scripts/dev-env.sh`
  then `uv run --frozen`). Use `scripts/dogfood` for the selected MCP/dogfood route. Do not invoke
  `harness` / `harnessd` from `PATH`.
- `uv run --frozen harness …` without `scripts/dev-env.sh` uses checkout code against the global daemon. Never do that in this repository.
- Do not read or write canonical per-user state (`~/.local/state/harness`, the per-user
  `harness.sock`) during ordinary isolated checkout work. When a human has explicitly enabled
  `scripts/dogfood` global mode, its MCP/Search/Task/Knowledge operations intentionally use that
  canonical state through the tool-installed runtime; do not enable or disable the mode without
  explicit user authorization.
- Do not run `harness install` or `harness uninstall` from this environment. Those commands mutate user-global host MCP and are refused while `HARNESS_DEV_ROOT` is set.
- Machine acceptance is the only agent-operated exception. After the user explicitly requests global installation or real-host acceptance, an agent may run `make accept-global-codex` with sandbox escalation. That target may replace the user-global uv-tool package, but all synthetic Harness/Codex/Workspace state must remain under its temporary roots and be cleaned on failure as well as success.
- Live activation is separate from synthetic acceptance. After the same explicit authorization, an agent may run `make install-global HOST=<explicit-profile>` and the corresponding read-only `make doctor-global` with sandbox escalation. Never default to multiple profiles, never create synthetic Workspaces in canonical state, and report which real registered Workspace configurations the live install reconciled.
- Do not invoke `scripts/install-global` directly except through those Make targets. Do not run global `harness uninstall --purge`, and do not delete or rewrite canonical per-user state to recover a failed acceptance.
- Tracked Codex/Cursor config names `harness-dev`; `.mcp.json` still names `harness` for Claude
  Code. All launch `scripts/dogfood mcp`. With no marker the router uses checkout-local
  `scripts/dev`; explicit global mode uses the tool-installed runtime and canonical state. Prefer
  reading this repository over broad Harness context when implementing Harness.
- Never invoke a leftover user-level/global MCP namespace (`user-harness` or equivalent) from this
  source checkout. The tracked project router is the only supported dogfood surface. Enable
  project `harness-dev` in Cursor Customize. `harness scan --global-dogfood` is reserved for the
  installed executable selected by `scripts/dogfood`; it indexes/registers this checkout without
  host or skill reconciliation.
- Ignore globally installed Harness skills if they appear in the host.

See [`docs/development/isolated-development.md`](docs/development/isolated-development.md).

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
- Once focused verification and independent review make a task safe to publish and repository/user authorization permits publication, create a durable task commit/PR before spending the remaining execution window on long repeatable full-suite runs. Do not treat maximal local verification as a prerequisite for first publication; the exact-head CI quality gate remains mandatory before merge.
- Publication readiness and merge readiness are separate gates. A reviewed candidate with proportionate focused checks and no known blocker may be published durably; merge still requires the exact published head, all required CI, and no known substantial correctness issue.
- In an ephemeral execution environment, never leave a reviewed publishable candidate only in temporary storage while repeatable long checks run. For tasks expected to be published, establish the Git transport and durable branch/ref strategy before substantial implementation.
- If execution/container loss exposes a durability failure, change the durability strategy immediately. Do not repeat the same local-only workflow and hope the next temporary environment lasts longer.
- If the user's acceptance condition explicitly includes opening a PR, passing CI, merging, or landing in `main`, those publication steps are part of the same bounded task. Do not STOP merely because implementation is locally verified. STOP only after the requested landing state is verified or a real blocker prevents it.
- If a long local gate is interrupted by the execution environment without a test failure, do not repeat the same approach indefinitely. Switch to a materially different verification strategy (for example, non-overlapping test partitions plus the component gates), record the interrupted full gate as NOT VERIFIED, and require the full exact-head CI gate to pass before merge. A real test/check failure must be fixed before publication.
- Never report a check as verified unless it actually ran successfully.
- If a task uncovers a larger follow-up, document it and stop at the current boundary rather than silently expanding scope.
- If normal Git remote transport is unavailable but authenticated repository-object access still exists, follow `docs/development/network-constrained-git.md`; preserve exact base/tree identity and do not move a feature branch ref away from the verified base until the complete remote tree matches the locally verified expected tree.
- For network-constrained publication, run a transport preflight before feature object writes. Prefer the repo-owned machine-side Git Data publisher when normal `git push` is unavailable. If only an authenticated GitHub Git Data connector is available, raw `utf-8` `create_blob` is allowed only for staged blobs that decode as UTF-8; send the exact staged text and require the returned blob SHA to equal the staged SHA before building any tree. Never hand-build or manually splice base64 across an agent/tool boundary. If any changed blob is non-UTF-8 and no byte-safe machine transport exists, fail before feature-object mutation.

## Correctness-review terminology

Keep review language aligned with the actual engineering domain.

- For concurrency, rollback, data-integrity, filesystem-ownership, compatibility, or state-machine defects that are not security issues, describe them as correctness defects.
- When critically reviewing prior work, prefer `independent correctness review` over security-oriented labels unless the task is specifically about security.
- Describe races in terms of the actual concurrent filesystem or state change being tested.
- Prefer terms such as `data-integrity`, `correctness`, `compatibility`, and `rollback safety` when they accurately describe the risk.
- Reserve terms such as `attack`, `exploit`, `vulnerability`, and `security boundary` for work that genuinely concerns a security property.
- Terminology choices must never reduce technical precision, test coverage, or review rigor; preserve the exact mechanics needed to reproduce and verify a defect.

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
