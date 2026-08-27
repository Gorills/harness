# Harness architecture baseline

**Status:** accepted implementation baseline after independent audit of specification v1.0, 2026-08-21.

This document preserves the product intent of `docs/specification.md` while correcting assumptions that are no longer valid under current MCP and host contracts.

## 1. Architectural goals

Harness is a globally installed, local-first project-intelligence control plane for coding agents. It is not an agent runtime, model proxy, IDE, Git client, or workflow engine.

The architecture optimizes for three simultaneous costs:

- **Agent cost:** small, useful context and few ritual tool calls.
- **Human cost:** immediate understanding of current project work and next action.
- **Implementation cost:** one core business implementation with narrow host adapters.

When these goals conflict, choose the simplest design that keeps correctness and provides measurable user value.

## 2. System context

```text
                         Human
                           │
                           ▼
                       Dashboard
                           │
                           ▼
Claude Code ─┐
Codex       ─┤
Cursor      ─┼─ stdio MCP ─ harness mcp ─ local IPC ─ harnessd
Antigravity ─┘                                      │
                                                    ├─ SQLite
                                                    ├─ Registry
                                                    ├─ Tasks
                                                    ├─ Indexer / Watcher
                                                    ├─ Search
                                                    ├─ Knowledge
                                                    ├─ Documentation
                                                    ├─ Skill Resolver
                                                    └─ Dashboard API
```

`harness mcp` is a host-facing transport adapter. `harnessd` owns durable business state and all core behavior.

## 3. Process model and ownership

### `harnessd`

One daemon per OS user. It owns:

- schema and migrations;
- Project/Workspace registry;
- structural index and filesystem watcher;
- Task state and task events;
- agent activity records;
- search and Working Sets;
- semantic Knowledge and staleness;
- skill resolution state;
- dashboard API and event stream.

The daemon is the only process allowed to perform business-state transitions directly.

### `harness mcp`

A small process spawned by an agent host through stdio. It:

1. establishes a local authenticated-by-OS-user IPC channel to `harnessd`;
2. resolves the current Workspace using host-specific and generic hints;
3. exposes the five model-facing MCP tools;
4. applies exposure limits at the model boundary;
5. records bridge lifecycle/activity as observable metadata;
6. never owns a second copy of task/search/index/knowledge logic.

### Dashboard

The dashboard talks to the same daemon/domain state as MCP. It does not have an independent database or alternate task workflow.

## 4. Protocol baseline: MCP 2026-07-28

Production targets the official MCP Python SDK v2 and the MCP 2026-07-28 protocol path while retaining compatibility behavior provided by the SDK for older hosts.

The modern MCP path is sessionless:

- no protocol handshake invariant;
- no `Mcp-Session-Id` domain identity;
- request metadata is per-request;
- server-initiated request semantics from the handshake era cannot be a correctness dependency.

Consequences for Harness:

- A `Task` must never be bound to a protocol session ID.
- `AgentSession` is renamed semantically to an observed **agent/bridge activity record**, even if the persisted table keeps the historical name.
- Any client metadata is advisory/self-reported and unsuitable for authorization or behavior-critical branching.
- Workspace and task resolution must work on every request without hidden protocol-session state.
- MCP roots are not a core dependency; they are deprecated in the modern protocol line.
- Harness `Task` is an application-domain work item and is **not** the MCP `io.modelcontextprotocol/tasks` extension. The MCP extension represents a long-running individual protocol request; it must not become Harness task identity, persistence, or lifecycle.

See ADR-0001.

## 5. Core domain entities

### Project

Logical software project. Stable across multiple physical checkouts.

### Workspace

Physical checkout/worktree of a Project. Filesystem state, current branch/HEAD, dirty state, watcher, and active Task constraint are Workspace-scoped.

Canonical identity should be based on normalized filesystem identity plus repository identity, not only display path. Symlink/case behavior must be normalized per platform.

### Task

Durable logical work item. A Task survives host restart, bridge restart, agent-host switch, and human feedback.

Minimum states remain:

- `working`
- `waiting`
- `completed`
- `cancelled`

v1 enforces at most one distinct `working` Task per Workspace transactionally. Parallel tasks use separate Workspaces/worktrees.

Each Task carries a monotonically increasing `revision` used only as an optimistic-concurrency token. Every successful Task mutation increments it. A caller never uses timestamps, bridge identity, or Workspace-current state as a substitute for this revision.

The name intentionally overlaps with the MCP Tasks extension, but the semantics do not. In v1, `task_start` and `task_checkpoint` are ordinary bounded MCP tool calls that mutate/query Harness-owned durable Task state in `harnessd`; they do not return or manage MCP task handles. A future use of the MCP Tasks extension would be justified only for a genuinely long-running single MCP operation and must remain orthogonal to Harness Task identity.

### AgentSession / AgentActivity

Observed client/bridge lifecycle record, not MCP protocol state. A practical record may contain:

- `id` (Harness-minted);
- `bridge_instance_id`;
- `client_info` when present;
- `workspace_id` once resolved;
- associated `task_id` when observed;
- `started_at`, `last_activity_at`, `ended_at`;
- host adapter/profile metadata.

Its association with a Task is diagnostic/history data. It is not the source of truth for which Task a request modifies.

### Structural Index

Derived mechanical index of filesystem/Git structure. It is disposable/rebuildable and never supersedes filesystem/Git truth.

### Knowledge Card

Durable, provenance-bearing semantic knowledge learned during real task work. Code-related cards should carry anchors/fingerprints and explicit freshness.

### Working Set

Derived, bounded relevance state for a Task/Workspace. It boosts ranking but does not become a hard search filter or source of truth.

## 6. Workspace resolution

Workspace resolution is the most important host boundary that the original specification left implicit.

Create an internal `WorkspaceResolver` orchestration component. It is not a public plugin framework and should not leak into domain models.

Resolution order:

1. host adapter provides an explicit, documented project-root hint when available;
2. documented host interpolation/launch information may supply an explicit root argument/environment value;
3. bridge process current working directory may be used only for hosts where real-host acceptance proves its semantics;
4. a normalized path is matched/registered against Harness Workspaces;
5. if the root cannot be resolved unambiguously, return a bounded actionable error rather than silently attaching to the wrong project.

Examples:

- Claude Code provides `CLAUDE_PROJECT_DIR` to stdio MCP servers: strong documented signal.
- Codex, Cursor, and Antigravity require adapter-specific validation of how a globally configured stdio process is associated with the active workspace. Configuration support alone is not proof of runtime current-directory semantics.

No core logic should special-case host names. Adapters produce normalized hints; core resolves Workspace identity.

See ADR-0002.

## 7. Task resolution without protocol sessions

Normal workflow remains low-ritual:

```text
project_status
→ task_start/resume
→ project_search
→ native work
→ task_checkpoint
```

But the implementation is explicitly workspace-domain based and write-safe:

- `task_start` without `task_id` creates a new Task only when the Workspace has no distinct `working` Task. Creation has no prior Task revision; the one-working-Task invariant and creation are enforced in one transaction, and the response returns the new `task_id` plus initial `revision`.
- `task_start` with `task_id` resumes an existing Task. If that Task is already the Workspace's `working` Task, resume is idempotent/read-like and returns its current revision without mutating Task state.
- If resume would mutate an existing Task (for example `waiting → working`), `task_start` MUST also include `expected_revision`; the transition uses the same compare-and-set rule as every other existing-Task mutation and returns the incremented revision.
- `completed` and `cancelled` Tasks are not reopened by `task_start` in v1; a future reopen operation must define its own explicit transition contract.
- Starting a different Task while the Workspace already has a `working` Task is a conflict; `task_start` must not silently replace it.
- Read-only calls may derive relevance/display defaults from Workspace + current Task and expose the current Task revision where a subsequent mutation may depend on it.
- `task_checkpoint` is a mutating operation and therefore MUST include both the intended Harness `task_id` and `expected_revision`.
- The daemon verifies Task/Workspace ownership and transition validity, then applies an existing-Task mutation only when the stored revision equals `expected_revision`; success increments and returns the new revision.
- Revision mismatch or a required-but-missing `expected_revision` is a bounded conflict/error with no state/event/knowledge mutation. The caller must refresh/reconcile before retrying; Harness must not silently replay stale semantic content against the newer Task state.
- A stale call for Task A must never be retargeted to whichever Task is current when the request executes, and a stale writer for Task A must never overwrite a newer checkpoint for Task A.
- Dashboard Task mutations (`Accept`, feedback, cancel) use the same revision precondition at the application boundary; interfaces do not get a concurrency bypass.
- A bridge activity record may mirror the current Task for history, but losing/restarting the bridge does not lose Task continuity.

This preserves the intended cheap workflow without relying on obsolete MCP session state. Existing-Task writes carry stable identity plus a concurrency token; idempotent resume of an already-working Task still needs no extra ritual call.

## 8. Model-facing MCP surface

Keep exactly five primary tools until data proves another operation is necessary:

- `project_status`
- `project_search`
- `project_context`
- `task_start`
- `task_checkpoint`

Write targeting rule:

- creating through `task_start` returns a new Harness `task_id` and initial `revision`;
- resuming an already-`working` Task by `task_id` is idempotent and returns its current revision;
- a `task_start` resume that changes existing Task state requires `task_id` + `expected_revision`;
- `task_checkpoint` requires `task_id` + `expected_revision`; successful existing-Task mutation returns the incremented revision;
- revision mismatch or missing required revision is non-mutating and requires refresh/reconciliation;
- model-visible read calls may use the Workspace current Task for relevance, but mutating calls never infer identity or concurrency state from mutable Workspace-current state.

`project_status` includes the effective `visibility_mode` (`normal` or `hidden`) as a compact domain field so the model can obey the current publication policy. Host capability diagnostics and enforcement internals stay out of the model-visible payload and belong in `doctor`/dashboard surfaces.

Each tool contract owns:

- explicit allowed fields;
- forbidden internal fields;
- default and hard item limits;
- serialized byte limit;
- truncation/pagination behavior;
- stable error shape;
- negative-disclosure tests.

The boundary returns compact structured data. Full source remains a native host file-read concern.

### Server instructions

Keep server-wide guidance short and front-loaded. Codex documents use of MCP server instructions and recommends making the first 512 characters self-contained. Other hosts may treat instructions differently, so natural agent usage remains a host acceptance requirement rather than core proof.

## 9. Local IPC

Use a platform-specific local IPC transport owned by the daemon subsystem:

- Unix/macOS: Unix domain socket.
- Windows: named pipe or another equivalent local-user IPC mechanism if proven simpler and equally secure.

IPC requirements:

- accessible only to the current OS user;
- explicit request/response schema independent of MCP wire types;
- bounded message sizes;
- protocol versioning between bridge and daemon;
- cancellation/timeouts;
- no source/context logging by default;
- reconnect behavior that does not mutate Task state implicitly.

Do not make loopback HTTP the mandatory model-facing IPC merely for implementation convenience.

## 10. Persistence

SQLite in WAL mode is the v1 store. One daemon owns write coordination.

Persistence rules:

- transactional domain transitions;
- foreign keys enabled;
- explicit schema version;
- ordered migrations tested both forward and against representative prior databases;
- backup before potentially destructive migration;
- idempotent indexer writes;
- derived data rebuildable independently from durable task/knowledge state.

FTS5 availability must be checked by `harness doctor` at runtime because Python/SQLite builds are an environment capability, not a safe universal assumption.

## 11. Indexing

Initial scan is deterministic, local, and non-LLM.

Indexing pipeline:

```text
Workspace discovery
→ ignore/sensitivity policy
→ file inventory + hashes
→ language/parser selection
→ symbols/imports/exports where supported
→ docs/Git metadata
→ transactional index update
→ FTS refresh
```

Incremental watcher events are debounced/coalesced and reconciled against the filesystem. Watcher events are hints; the filesystem is authoritative. Rename may be observed as delete+create and must still converge correctly.

`ParserAdapter` remains a narrow language parsing boundary. Unsupported languages degrade to paths/text/docs/Git rather than failing the project.

## 12. Search

Search combines independent retrieval channels and fuses ranked results rather than relying on a single opaque score:

- exact path/symbol;
- normalized identifier tokens;
- SQLite FTS5;
- structural proximity;
- docs;
- fresh semantic Knowledge;
- relevant Task history;
- optional embeddings for semantic text only.

Use a simple fusion strategy such as Reciprocal Rank Fusion before introducing manually tuned weight matrices.

Search must explain why a result matched and must penalize stale semantic evidence.

The implemented Project Intelligence retrieval boundary is daemon-owned. Workspace hints resolve exactly one registered Workspace, the daemon validates its live Git identity before and after the read, and one read transaction fixes the corresponding Project identity. Current code/docs remain Workspace-local structural-index data; Knowledge and Task-history channels are Project-scoped. Rebuildable FTS5 tables are candidate/ranking indexes only: selected search/context payloads are reread from authoritative `indexed_files`, `knowledge_cards`/anchors, Tasks, checkpoints, and events. Cross-Project refs fail closed. `project_context` expands only explicit bounded refs; source code is never returned and remains a native-host read. `needs_revalidation` Knowledge is labelled as historical evidence and ranked after fresh Knowledge.

## 13. Knowledge and staleness

Knowledge is deliberately sparse. A card is created only when it can plausibly prevent future re-investigation.

Every card has provenance. Code-related cards should be anchored to file/symbol fingerprints when possible.

On anchor change:

```text
fresh → needs_revalidation
```

Stale knowledge is retained as a historical clue, receives ranking penalty, and cannot be presented as a current verified fact. Harness never invokes an LLM automatically to repair it.

## 14. Skills architecture

Canonical Harness skill registry lives outside repositories. The resolver selects a relevant subset using deterministic project stack, Task hints, and explicit configuration.

Projection is host-native and owned by adapters.

Important compatibility rule: several hosts scan overlapping compatibility directories. In particular, Cursor loads `.agents/skills`, `.cursor/skills`, and compatibility skill trees including Claude/Codex locations. Therefore a naïve strategy that copies the same Harness skill to every host directory can produce duplicate model-visible skills.

Projection design must include:

- a per-Workspace projection plan;
- collision detection by skill name and target path;
- Harness ownership marker/manifest outside portable `SKILL.md` where necessary;
- exact cleanup of Harness-owned artifacts only;
- preference for shared `.agents/skills` where multiple active hosts natively support it and semantics match;
- Claude-specific `.claude/skills` only when required, with Cursor duplicate visibility covered by acceptance tests;
- `.git/info/exclude` for generated project artifacts where appropriate, never silent `.gitignore` mutation.

Skill hot reload is an optimization, not a correctness requirement.

## 15. Normal and Hidden visibility modes

Harness exposes a durable Project-level `visibility_mode` with exactly two v1 values. New Projects default to `normal`; changing the mode is an explicit operator/human action and is not exposed as a model-facing MCP mutation.

- `normal`: ordinary native-host SCM behavior is allowed subject to user/host permissions; Harness does not suppress host attribution by default.
- `hidden`: the agent remains an editing/research assistant, but durable SCM publication is human-owned. Agent-originated staging/index writes, commit/amend, ref/branch/tag mutations, push, PR/issue/review/comment/release actions, and equivalent remote SCM mutations are denied for the supported host profile.

Hidden mode also strengthens project-artifact hygiene:

- canonical Harness state/skills/rules remain outside repositories;
- host-required project projections are untracked and ignored through the path resolved by `git rev-parse --git-path info/exclude` (logically `$GIT_COMMON_DIR/info/exclude`);
- `.gitignore` and tracked instruction/config files are never modified merely to activate Hidden mode;
- tracked-path or unknown-user-file collisions fail before materialization;
- `assume-unchanged` and `skip-worktree` are not used as ignore mechanisms;
- cleanup removes only Harness-owned files and exclude entries.

`info/exclude` is not a security boundary: standard Git can force-add ignored files. Hidden correctness therefore depends on host-specific enforcement that denies agent-originated SCM mutations, with prompt rules and optional Git hooks only as defense-in-depth.

Because `$GIT_COMMON_DIR/info/exclude` is shared across linked worktrees, all Harness Workspaces resolving to the same Git common directory use the same effective visibility mode in v1. Contradictory per-worktree modes are rejected.

Hidden is capability-gated and fail-closed. A host/profile is supported only when its adapter has real-host proof for local Hidden instructions, SCM-write enforcement, policy integrity (the agent cannot disable its own enforcement), attribution suppression where applicable, safe projection paths, and deterministic cleanup. Every agent/bridge admission to a Hidden Project re-validates the resolved adapter profile; an unsupported or unverifiable profile gets a bounded error rather than a silent downgrade to Normal. Host-profile identity for this decision comes from Harness-owned adapter/registration metadata, never from self-reported `clientInfo`. Unknown or prompt-only behavior is not reported as enforced Hidden mode. Provider-side telemetry/analytics and unrelated agents/tools operating outside the Harness integration are outside this repository/SCM visibility contract.

See ADR-0003.

## 16. Host adapters

`HostAdapter` is the only public host extension boundary in v1.

Responsibilities:

- host detection/version discovery when available;
- safe global MCP registration/unregistration;
- workspace-root hint construction;
- minimal bootstrap instructions if needed;
- native skill/rule/local-settings projection and cleanup;
- Hidden-mode capability reporting, enforcement setup, and restoration of Harness-owned policy;
- doctor checks;
- optional hooks that never become correctness dependencies.

Adapters must be idempotent and preserve unknown user configuration.

The implemented Linux/POSIX installation slice supports Claude Code plus local Cursor IDE/CLI. `harness install --host claude-code|cursor|all` performs runtime and registration-ownership preflight, replaces a stale daemon only through the frozen schema/package-version/interpreter/code identity contract, and then delegates host mutation to the selected adapters. Omitted `--host` remains Claude Code for compatibility. Claude uses the official `claude mcp` CLI and `CLAUDE_PROJECT_DIR`. Cursor edits only its documented `~/.cursor/mcp.json` and per-Workspace `.cursor/mcp.json` surfaces; each project override repeats the full stdio launch definition and adds `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` because same-name project config shadows the global server. Cursor-profile Workspace resolution requires that exact root hint and never falls back to cwd. Tracked Cursor project config is manual-adoption/removal only; Harness-created untracked config carries Workspace-local ownership metadata and Git-local exclusions. The Harness source checkout itself commits a tracked isolated-development overlay that launches `scripts/dev harness mcp` instead of the production Cursor signature; production install/scan/uninstall must not rewrite or delete it, a system `harness scan` of that overlay root is refused, and isolated `harness install`/`uninstall` are refused while `HARNESS_DEV_ROOT` is set. A production MCP process with `HARNESS_HOST_PROFILE` lists no tools and refuses calls when launched against that overlay checkout; cwd is used when the profile's documented root hint is absent or unresolvable and is not Workspace identity. The overlay launch without that profile marker still exposes the five Harness tools.

`harness scan` inspects all current supported Harness registrations, reconciles the scanned Cursor project override when needed, and submits one combined host-profile set to daemon-owned skill reconciliation. This keeps Cursor compatibility roots from producing duplicate generated skills. `harness uninstall` removes only selected host artifacts. If another supported host remains, every registered live Workspace is reconciled against the remaining profile set and the shared daemon stays running; otherwise owned skills are cleaned and the daemon shuts down. `--purge` is refused while another supported host remains active. Bare `harness doctor` reports Claude and Cursor registrations separately, checks Cursor project overrides against the exact `${workspaceFolder}` contract, and inspects generated skills against the union of current profiles. Core Task/Knowledge/index logic remains host-neutral. This is automated local integration proof, not proprietary-host UI/CLI acceptance; Cursor Cloud Agents remain a separate unsupported profile.

Current target profiles:

- Claude Code
- Codex (shared CLI/IDE/desktop MCP config where documented)
- Cursor
- Antigravity IDE/CLI behavior represented by explicit adapter capabilities/profile data where their skill/config surfaces differ

Do not branch core business logic on host identity.

## 17. Dashboard

The dashboard uses the Python stdlib loopback HTTP server with capability-scoped HTML/CSS/JavaScript assets. Project/Workspace/Task drill-down, bounded indexed-path search, and SSE freshness hints are implemented without an async web stack; realtime remains presentation-only and does not create another source of truth.

Dashboard rules:

- bind loopback only by default;
- same daemon/domain state as MCP;
- show only observed activity, never claim access to model internal reasoning;
- state transitions (`Accept`, feedback, cancel) call the same domain services used by other interfaces;
- SSE is for dashboard realtime UI and is unrelated to deprecated MCP SSE transport; events carry freshness hints only, not Task/source payloads.
- dashboard navigation/search/actions must remain progressively usable without JavaScript; JavaScript may enhance freshness but must not become mutation authority.
- dashboard assets stay capability-scoped and same-origin so CSP can forbid inline script/style.

## 18. Security and privacy boundaries

- Local-only by default.
- Daemon IPC restricted to current OS user.
- Dashboard loopback only by default.
- No raw source to external providers without explicit opt-in.
- Full agent transcripts not persisted by default.
- Secrets/sensitive patterns excluded from indexing where feasible, but ignore rules are defense-in-depth, not a substitute for access control.
- `clientInfo` and host metadata are self-reported diagnostics, not authentication.
- Integration mutation must be ownership-aware and reversible.
- Logs use metadata/identifiers, not raw source/context payloads by default.

## 19. Failure and recovery

A crash must not invent progress.

- Last explicit Task state survives.
- A crash never auto-completes a Task.
- Stale agent activity records age out/inactivate independently.
- Watcher/index state reconciles from filesystem on restart.
- IPC reconnect is side-effect free until an explicit domain operation occurs.
- Index corruption/rebuild must not destroy durable Tasks or Knowledge.
- In `normal`, Harness failure leaves the native host/Git workflow unchanged. In `hidden`, source editing, shell work, and read-only Git inspection must remain usable, while agent SCM publication stays denied by host policy; human Git/SCM actions outside the agent execution path remain unaffected.

## 20. Testing architecture

### Core automated proof

Automate deterministically:

- schema/migrations;
- registry/workspace identity;
- deterministic scan and incremental reconciliation;
- search/ranking acceptance fixtures;
- Task lifecycle and concurrency invariant;
- Knowledge staleness;
- exact MCP tool schemas and descriptions;
- real stdio subprocess wire payloads;
- byte/item budgets and truncation;
- negative disclosure;
- dashboard HTTP/SSE/action behavior;
- skill resolver/projection planning independent of proprietary hosts.

### Real-host acceptance

Keep a separate matrix because core tests cannot prove proprietary host behavior:

- global MCP registration discovered;
- tools visible/callable;
- current Workspace resolved correctly;
- instructions influence normal usage where the host documents/supports them;
- relevant native skill visible and irrelevant skills absent;
- host switch resumes the same Harness Task;
- Harness failure preserves the mode contract: Normal stays native/unrestricted by Harness; Hidden keeps agent publication denied while ordinary edits/read-only Git and human Git outside the agent path remain usable.

No passing unit/integration suite may be described as proof of these host-specific behaviors.

## 21. Dependency direction for implementation

Target modular-monolith package direction:

```text
interfaces/cli ─┐
interfaces/mcp ─┼────► application ─────► domain
interfaces/web ─┘            │              ▲
                              ▼              │
                    infrastructure ──────────┘
                    (sqlite, git, fs, parsers,
                     watcher, host adapters)
```

Rules:

- domain has no framework/host/SQLite/FastAPI/MCP dependency;
- application orchestrates use cases and transactions through ports;
- infrastructure implements ports;
- interfaces translate external contracts to application commands/queries;
- host adapters live in infrastructure/integration, not domain/application business policy;
- model exposure DTOs are explicit interface contracts, never raw persistence models.

Avoid a generic dependency-injection/plugin framework. Plain constructors/protocols are enough until complexity proves otherwise.

## 22. Planned repository shape

The first implementation bootstrap should create something close to:

```text
src/harness/
  domain/
  application/
  infrastructure/
    db/
    indexing/
    search/
    hosts/
    ipc/
  interfaces/
    cli/
    mcp/
    web/
tests/
  unit/
  integration/
  search/
  mcp_contract/
  mcp_wire/
  dashboard/
  fixtures/
```

This is guidance, not permission to create empty layers pre-emptively. Add modules when a bounded feature needs them.

## 23. Architecture change control

The original specification is preserved verbatim. Corrections enter through audit findings and ADRs so the reason for divergence stays reviewable.

Any future host/protocol change should follow:

```text
official contract changes
→ audit impact
→ ADR if architectural
→ smallest implementation change
→ core tests
→ host acceptance if applicable
```
