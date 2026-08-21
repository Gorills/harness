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

At most one distinct `working` Task should exist per Workspace. Parallel tasks use separate Workspaces/worktrees.

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

But the implementation is explicitly workspace-domain based:

- `task_start` creates/resumes a Task and establishes it as the Workspace's current working Task through a transactional domain transition.
- Subsequent read calls derive relevance from Workspace + current Task.
- `task_checkpoint` targets the Workspace current Task when unambiguous.
- If ambiguity is possible because of recovery/admin state, require explicit `task_id` or fail safely; do not guess.
- A bridge activity record may mirror the current Task for history, but losing/restarting the bridge does not lose task binding.

This preserves the intended cheap workflow without relying on an obsolete MCP session concept.

## 8. Model-facing MCP surface

Keep exactly five primary tools until data proves another operation is necessary:

- `project_status`
- `project_search`
- `project_context`
- `task_start`
- `task_checkpoint`

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

## 15. Host adapters

`HostAdapter` is the only public host extension boundary in v1.

Responsibilities:

- host detection/version discovery when available;
- safe global MCP registration/unregistration;
- workspace-root hint construction;
- minimal bootstrap instructions if needed;
- native skill projection/cleanup;
- doctor checks;
- optional hooks that never become correctness dependencies.

Adapters must be idempotent and preserve unknown user configuration.

Current target profiles:

- Claude Code
- Codex (shared CLI/IDE/desktop MCP config where documented)
- Cursor
- Antigravity IDE/CLI behavior represented by explicit adapter capabilities/profile data where their skill/config surfaces differ

Do not branch core business logic on host identity.

## 16. Dashboard

FastAPI/Starlette/Uvicorn + Jinja2/HTML/CSS/vanilla JS + SSE remains a sound v1 choice.

Dashboard rules:

- bind loopback only by default;
- same daemon/domain state as MCP;
- show only observed activity, never claim access to model internal reasoning;
- state transitions (`Accept`, feedback, cancel) call the same domain services used by other interfaces;
- SSE is for dashboard realtime UI and is unrelated to deprecated MCP SSE transport.

## 17. Security and privacy boundaries

- Local-only by default.
- Daemon IPC restricted to current OS user.
- Dashboard loopback only by default.
- No raw source to external providers without explicit opt-in.
- Full agent transcripts not persisted by default.
- Secrets/sensitive patterns excluded from indexing where feasible, but ignore rules are defense-in-depth, not a substitute for access control.
- `clientInfo` and host metadata are self-reported diagnostics, not authentication.
- Integration mutation must be ownership-aware and reversible.
- Logs use metadata/identifiers, not raw source/context payloads by default.

## 18. Failure and recovery

A crash must not invent progress.

- Last explicit Task state survives.
- A crash never auto-completes a Task.
- Stale agent activity records age out/inactivate independently.
- Watcher/index state reconciles from filesystem on restart.
- IPC reconnect is side-effect free until an explicit domain operation occurs.
- Index corruption/rebuild must not destroy durable Tasks or Knowledge.

## 19. Testing architecture

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
- Harness failure leaves native agent workflow usable.

No passing unit/integration suite may be described as proof of these host-specific behaviors.

## 20. Dependency direction for implementation

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

## 21. Planned repository shape

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

## 22. Architecture change control

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
