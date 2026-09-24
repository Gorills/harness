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
Cursor      ─┐
Antigravity ─┴─ stdio MCP ─ harness mcp ─ local IPC ─ harnessd
                                                    │
Codex ─ authenticated Streamable HTTP MCP ──────────┤
                                                    ├─ SQLite
                                                    ├─ Registry
                                                    ├─ Tasks
                                                    ├─ Indexer / Watcher
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
- semantic Knowledge and staleness;
- skill resolution state;
- dashboard API and event stream.

The daemon is the only process allowed to perform business-state transitions directly.

### MCP adapters

A thin host-facing adapter exposed through stdio where accepted and through daemon-owned
Streamable HTTP for Codex. It:

1. reaches daemon-owned state through local IPC; the Codex HTTP adapter runs with `harnessd`, so
   Codex never needs access to the Unix socket;
2. resolves the current Workspace using host-specific and generic hints;
3. exposes the bounded model-facing MCP tools;
4. applies exposure limits at the model boundary;
5. records bridge lifecycle/activity as observable metadata;
6. never owns a second copy of task/index/knowledge logic.

### Dashboard

The dashboard talks to the same daemon/domain state as MCP for Projects, Tasks and Knowledge.
It does not duplicate that database or Task workflow. The operator's personal notes and access
credentials are a separate private subsystem with selectable password protection, outside
Harness business state and every model surface; see [ADR-0069](docs/decisions/0069-project-hub-and-private-vault.md).

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

Physical checkout of a Project: a Git worktree when Git is present, otherwise a bound
filesystem directory. Filesystem state, current branch/HEAD when Git exists, dirty state,
watcher, and active Task constraint are Workspace-scoped.

Canonical identity is the normalized directory plus Git common directory when attached.
A filesystem Workspace stores `git_common_dir == workspace_root` until Git appears, then
keeps the same `workspace_id`. Hidden remains Git-only.

### Task

Durable logical work item. A Task survives host restart, bridge restart, agent-host switch, and human feedback.

Minimum states remain:

- `working`
- `waiting`
- `completed`
- `cancelled`

v1 enforces at most one distinct `working` Task per Workspace transactionally. Parallel tasks use separate Workspaces/worktrees.

Each Task carries a monotonically increasing `revision` used only as an optimistic-concurrency token. Every successful Task mutation increments it. A caller never uses timestamps, bridge identity, or Workspace-current state as a substitute for this revision.

Operator tracking is orthogonal to lifecycle state. A Task may carry one Jira URL and two independent deployment flags (`deploy_test` and `deploy_prod`, including both true), plus immutable operator comments in its event history. These human-maintained fields never substitute for `working`/`waiting`/`completed`/`cancelled` or a waiting reason. Their mutations use the same explicit Task identity and revision CAS as every other existing-Task write.

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

- Cursor project `.cursor/mcp.json` stores the canonical absolute root in `HARNESS_WORKSPACE_ROOT`; this also works in CLI builds that do not interpolate `${workspaceFolder}`.
- A leftover `HARNESS_HOST_PROFILE=claude-code` process is an unsupported profile ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md)); overlay refuse may still use `CLAUDE_PROJECT_DIR`.
- Codex, Cursor, and Antigravity require adapter-specific validation of how a globally configured stdio process is associated with the active workspace. Configuration support alone is not proof of runtime current-directory semantics.

No core logic should special-case host names. Adapters produce normalized hints; core resolves Workspace identity.

See ADR-0002.

## 7. Task resolution without protocol sessions

Normal workflow remains low-ritual:

```text
project_status
→ task_start/resume when durable tracking is useful
→ native repository discovery and work
→ task_checkpoint for tracked work; ready → waiting(operator_review)
```

Agents use native repository tools for code and documentation discovery. Harness no longer
provides project code/document search or requires a Harness call before native exploration.
`project_recall` can find durable Knowledge and Task references when earlier IDs are unknown;
it does not search code or documentation and is never a prerequisite for native exploration.
`project_context` expands selected explicit Knowledge and Task references. Explicit
code/document refs remain metadata-only compatibility inputs; no Harness discovery tool
generates them.

Start or resume a Task for substantial changes, multi-step work, needed durable continuity,
or an explicit operator request. Quick questions, read-only inspection, and small local edits
may proceed after status without a Task when tracking adds no useful continuity. If the scope
grows, start tracking before continuing. This is a workflow judgment, not a new required reason
field or approval ritual. A failed Harness call requires schema-guided retry.

For tracked work, one requested outcome owns one Task across diagnosis, implementation,
verification, clarifications, continuation, subagents, and host restarts. Resume explicitly by ID;
messages and phases are not Task boundaries. Checkpoint each logical stage. Keep unfinished work
`working`; use `waiting` with its dependency reason. A ready result, including an audit, uses
`waiting(operator_review)` until the operator accepts it. Agent checkpoint writers accept only
`working` and `waiting`; historical `completed` checkpoints remain readable. Only operator
operations complete or cancel Tasks. These decisions supersede ADR-0038's unconditional
Task-before-diagnosis requirement; see [ADR-0066](docs/decisions/0066-operator-owned-task-lifecycle.md).

The compact MCP and Codex bootstrap bodies share the same outcome-boundary wording from
`agent_instructions.py`; tool descriptions explain resume and completion semantics. For a
Harness-owned Codex registration, the same first-action bootstrap is also projected into a
root `AGENTS.override.md` ([ADR-0073](docs/decisions/0073-codex-root-agents-bootstrap.md)).
After actual Harness config reconciliation, fully restart the host and open a fresh conversation to refresh
the instruction snapshot while retaining the durable Task identity. Continue the same unfinished
Task after `project_status`; terminal
Tasks still require the separate human reopen operation. Checkout source changes alone do not
activate host config. These instructions guide model behavior; they do not infer duplicate Tasks
from titles or replace the explicit-ID state machine. Real-model compliance is a separate
[acceptance check](docs/development/task-continuity-acceptance.md).

But the implementation is explicitly workspace-domain based and write-safe:

- `task_start` without `task_id` creates a new Task only when the Workspace has no distinct `working` Task. Creation has no prior Task revision; the one-working-Task invariant and creation are enforced in one transaction, and the response returns the new `task_id` plus initial `revision`.
- `task_start` with `task_id` resumes an existing Task. If that Task is already the Workspace's `working` Task and its recorded origin is unchanged, resume is idempotent/read-like and returns its current revision. An explicit initial branch handoff changes origin with revision CAS and advances the revision (ADR-0067).
- If resume would mutate an existing Task (for example `waiting → working`), `task_start` MUST also include `expected_revision`; the transition uses the same compare-and-set rule as every other existing-Task mutation and returns the incremented revision.
- `completed` and `cancelled` Tasks are never reopened by `task_start`. Dashboard `task_reopen` is a separate human-only CAS operation that preserves Task identity, appends a `reopened` event, and still obeys the one-working-Task-per-Workspace invariant.
- Starting a different Task while the Workspace already has a `working` Task is a conflict; `task_start` must not silently replace it.
- Read-only calls may derive relevance/display defaults from Workspace + current Task and expose the current Task revision where a subsequent mutation may depend on it.
- `task_checkpoint` is a mutating operation and therefore MUST include both the intended Harness `task_id` and `expected_revision`.
- Agent-reported checkpoint verification is bounded semantic evidence (`name`, `passed|failed|not_run`, `evidence`) persisted atomically with the checkpoint; mechanical Git evidence remains Harness-derived.
- The daemon verifies Task/Workspace ownership and transition validity, then applies an existing-Task mutation only when the stored revision equals `expected_revision`; success increments and returns the new revision.
- Revision mismatch or a required-but-missing `expected_revision` is a bounded conflict/error with no state/event/knowledge mutation. The caller must refresh/reconcile before retrying; Harness must not silently replay stale semantic content against the newer Task state.
- A stale call for Task A must never be retargeted to whichever Task is current when the request executes, and a stale writer for Task A must never overwrite a newer checkpoint for Task A.
- Dashboard Task mutations (`Accept`, feedback, comment, Jira/deployment update, state selection, reopen, cancel, delete) use the same revision precondition at the application boundary; interfaces do not get a concurrency bypass.
- Operators may explicitly choose any lifecycle state with its required wait reason, preserving the one-working-Task invariant. Completion records acceptance; a ready agent report alone does not.
- Test and production deployment are independent durable booleans. Schema v22 backfills the legacy single marker; old event/checkpoint history remains readable. Both flags may be true.
- Confirmed operator Task deletion checks identity and revision, then removes source-Task Knowledge and the Task with its dependent history and retrieval projections in one transaction. It never deletes repository files.
- A bridge activity record may mirror the current Task for history, but losing/restarting the bridge does not lose Task continuity.

This preserves the intended cheap workflow without relying on obsolete MCP session state. Existing-Task writes carry stable identity plus a concurrency token; idempotent resume of an already-working Task still needs no extra ritual call.

## 8. Model-facing MCP surface

The model-facing surface has five tools:

- `project_status`
- `project_context`
- `project_recall`
- `task_start`
- `task_checkpoint`

`project_recall` is an optional, bounded lookup over durable Knowledge and Task records.
It returns only applicable references, titles, optional short summaries and freshness;
the caller uses `project_context` to expand a selected reference. The response bounds its
serialized size, removes previews before dropping any hits, and reports whether results
were truncated. It exposes no code/document/path search, raw source, or cross-checkout
Task/Knowledge that lacks current Git evidence.

Write targeting rule:

- creating through `task_start` returns a new Harness `task_id` and initial `revision`;
- resuming an already-`working` Task by `task_id` with unchanged origin is idempotent and returns its current revision; an explicit initial branch handoff requires revision CAS and advances it;
- a `task_start` resume that changes existing Task state requires `task_id` + `expected_revision`;
- `task_checkpoint` requires `task_id` + `expected_revision`; successful existing-Task mutation returns the incremented revision;
- revision mismatch or missing required revision is non-mutating and requires refresh/reconciliation;
- model-visible read calls may use the Workspace current Task for relevance, but mutating calls never infer identity or concurrency state from mutable Workspace-current state.

`project_status` includes the effective `visibility_mode` (`normal` or `hidden`) as a compact domain field so the model can obey the current publication policy. Host capability diagnostics and enforcement internals stay out of the model-visible payload and belong in `doctor`/dashboard surfaces.

The model-visible `index` object is a cheap SQLite snapshot: `indexed_file_count` is the current Structural Index inventory, and last-known reconcile provenance (`index_revision`, `last_successful_reconcile_at`, `last_reconcile_kind`) records when and how the index last successfully reconciled, including a no-op persist that still advances the watermark. Provenance is last-known success, not proof that nothing changed afterwards and not a live freshness claim. Status must not reread Workspace source, run a freshness scan, or add Git work to compute these index fields.

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

Front-load that operator-facing Task `title`, checkpoint `summary`/`next_step`, and Knowledge title/body are written in Russian for the dashboard. Tool names, field names, and enums stay English. Harness stores the supplied UTF-8 as-is and does not language-detect.

Keep operator-facing chat short. Checkpoints own durable continuity; chat carries only the human-relevant delta. Lead with the result, do not restate the Task/checkpoint or recap diffs/file lists, and report only material decisions, risks, blockers, and verification unless detail is requested. This is a soft native-host instruction, not a claimed hard output-token limit or model proxy. Unknown-argument errors name the tool's public allowed fields and tell the caller to retry; they do not echo unknown names.

## 9. Local IPC

Use a platform-specific local IPC transport owned by the daemon subsystem:

- Unix/macOS: Unix domain socket.
- Windows: named pipe or another equivalent local-user IPC mechanism if proven simpler and equally secure.

IPC requirements:

- accessible only to the current OS user;
- explicit request/response schema independent of MCP wire types;
- bounded message sizes;
- protocol versioning between bridge and daemon;
- cancellation/timeouts, with command-specific bounds;
- bounded concurrent client handling so one slow request does not head-of-line block unrelated callers;
- one SQLite connection per accepted IPC worker rather than sharing a connection across threads;
- a bounded 30-second SQLite writer wait on daemon-owned connections so Task mutations do not fail
  under a normal watcher reconciliation transaction;
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
- operator backups use SQLite's online backup API so committed WAL frames are included without
  stopping the daemon;
- restore validates archive checksum, SQLite integrity/foreign keys, contiguous exact schema, and
  creating runtime identity before taking the daemon's database lock and replacing state; the
  current database receives an automatic pre-restore backup;
- idempotent indexer writes;
- derived data rebuildable independently from durable task/knowledge state.

## 11. Indexing

The Structural Index is derived from the current Workspace filesystem and Git state. An
authoritative scan inventories eligible files and their mechanical identities under the
ignore/sensitivity policy, then updates the index transactionally. It supports status,
Knowledge staleness and skill relevance without exposing a repository search service.

Incremental watcher observations are debounced and reconciled against the filesystem.
Idle polls use a subprocess-free metadata token over directory/Git-control state plus one rotating
128-path metadata shard; Git status/HEAD confirmation runs only after that token changes. A bounded
same-HEAD dirty-path set can be reconciled incrementally. Initial, HEAD/policy/unknown/large
changes and the periodic safety pass receive a full authoritative scan. Watcher observations
are hints; the filesystem is authoritative. Rename may be observed as delete+create and must
still converge correctly.

Successful full and incremental reconciles persist last-known provenance in the same SQLite
transaction (`workspace_index_reconcile`: monotonic per-Workspace `index_revision`, timezone-aware
UTC `last_successful_reconcile_at`, and `last_reconcile_kind` `full` or `incremental`). A failed
reconcile rolls back without advancing those fields. An unchanged inventory that still commits is a
successful reconcile and still advances those fields. Watcher in-memory `last_reconciled_at` is not
the source of truth. New Workspaces have no provenance row until the first successful scan; status
then reports JSON nulls rather than fake zeros.

## 12. Task lookup

The dashboard retains human lookup and filtering over durable Task records. Task titles,
checkpoint summaries and next steps, branches, feedback, comments, Jira links and delivery
markers may contribute bounded Task hits. Exact Task IDs take precedence over textual matches;
the displayed hexadecimal prefix and `task:<id>` references remain accepted. Ambiguous prefixes
return bounded Task lists in the requested Project scope, or across registered Projects on the
home dashboard. Task lookup returns metadata and history, never repository source.

This is a Task-record navigation feature. It does not advertise code or documentation retrieval,
does not require a current-source scan, and does not change the agent's native repository
discovery workflow. The prior project search contract and its derived code/document retrieval
projections were retired by [ADR-0071](docs/decisions/0071-retire-project-code-search.md).

## 13. Knowledge and staleness

Task and Knowledge reads also require active-checkout applicability (ADR-0067). Schema v23
adds bounded origin/checkpoint Git evidence without backfilling historical fingerprints from
today's files. One request-local resolver checks live identity, HEAD/branch and consulted
content before return. Task history uses its latest checkpoint: originating-branch continuity
survives edits, while cross-branch visibility requires captured commit ancestry or complete
changed-path content evidence. Knowledge uses its own source checkpoint and current anchors;
operator/imported anchors receive the same live check. Unanchored agent Knowledge does not
inherit the Task's ongoing-work exemption. Filtering precedes Task lookup limits and
checkpoint history pagination; direct refs cannot bypass it. Operator dashboard Task lists,
counts, and Task-history search retain all branches of that home/Workspace/Project scope;
current-Task focus and model-facing reads stay Git-filtered. A missing checkout still
withholds Task disclosure.

Whole-file squash/cherry-pick proofs are intentionally conservative when other edits alter the
same file. One-working-Task-per-Workspace remains a persistence invariant even for hidden Tasks;
the generic conflict directs the operator to the archive without exposing hidden identities.

Knowledge is deliberately sparse. A card is created only when it can plausibly prevent future re-investigation.

Every card has provenance. Code-related cards should be anchored to file/symbol fingerprints when possible.

On anchor change:

```text
fresh → needs_revalidation
```

Stale knowledge is retained as a historical clue, receives ranking penalty, and cannot be presented as a current verified fact. Harness never invokes an LLM automatically to repair it.

## 14. Skills architecture

Canonical Harness skill registry lives outside repositories. Runtime load, built-in sync, doctor, and purge preflight share one fail-closed local filesystem-trust check: an existing registry root must be a real current-user directory without group or other write; missing roots stay empty or skip, unsafe existing roots are refused rather than chmod'd, and prepare also requires the immediate parent to meet that same owner/write contract (custom `HARNESS_SKILL_REGISTRY` ancestor replacement is out of v1). The resolver selects every relevant Skill using deterministic project stack and explicit include/exclude configuration. Project-level skill scope can Auto-detect, Include, or Exclude stable development facets; this durable policy applies across Project Workspaces and future pack updates, while `software-project` remains non-disableable. Harness ships a compact built-in quality pack of 16 skills into that registry through ownership-aware reconciliation. Six cores apply to every detected software project: `testing-strategy`, `secure-by-design`, `project-architecture`, `complex-change-planning`, `language-engineering`, and `legacy-preservation`. The built-ins are intent-oriented, composed through stack applicability, and validated against supported host surfaces; no second workflow/composition DSL is introduced. Detailed Docker, frontend discoverability, language-native, mobile, server, game, operations, and security guidance uses portable nested `references/` that the selected skill routes to only when relevant. All stack-matching Skills are projected; deterministic match strength may order them but never drops a relevant Skill because of a count cap. Same-id unknown or user-modified canonical content, including nested reference content, is never overwritten. When an ID leaves `BUILTIN_SKILLS`, exact-owned stale trees are removed through the same replacement-backup path as updates; user-modified stale trees stay as user-owned skills and leave the ownership manifest. Successful restore returns exact pre-sync trees, including retirements. If restore fails, remaining replacements still roll back, surviving backups are preserved, and sync raises an explicit recovery failure that includes the surviving backup path rather than re-raising only the original error.

The quality baseline includes explicit local/test/production container operations, Google/Yandex
public-route discoverability and web performance, project architecture, change quality (including
legacy compatibility), language-native correctness/tooling, and durable data integrity. A registered Workspace is `software-project` evidence even with an empty index, so the six cores
project after `harness init` of a Git worktree or ordinary folder
([ADR-0063](docs/decisions/0063-registered-workspace-baseline-and-godot-evidence.md),
[ADR-0064](docs/decisions/0064-opt-in-workspace-init-and-skill-include.md)).
Specialized language and domain Skills still wait for indexed manifests or source. Task
`stack_hints` remain
optional Task metadata and are not a Skill selector. Dependency matching retains the
existing portable exact-token contract, while deterministic derived facets capture cross-signal
project roles such as `web-frontend`, `mobile-app`, `backend-service`, `godot-project`, and
`deployment-ops`. Facets are calculated with manifest locality where needed: an Expo/React Native
package is mobile even when its compatibility dependencies include React DOM, and it does not make
`public-frontend` relevant unless independent web evidence exists. `godot-project` matches nested
`project.godot` plus `.gdextension`, `.gd`, `.tscn`, and `.gdshader`. Stack detection also parses
`CMakeLists.txt`, Dart
`pubspec.yaml`, Ruby `Gemfile.lock` (`Gemfile` only when that directory has no sibling lockfile), Maven
`pom.xml`, conservative Gradle/version-catalog text, and `.csproj` PackageReference/web SDK. Gradle
Groovy/KTS uses quoted coordinates and plugin ids only; TOML `module=`/`id=`/`group=`/`name=` applies
only to `libs.versions.toml`. Indexed XML manifests fail closed unless they are UTF-8 without a
document type declaration, then parse with ElementTree `fromstring`. Gradle remains text evidence,
not a full evaluator.
`secure-by-design` applies to detected software projects and progressively routes to web/backend,
browser, mobile,
infrastructure/supply-chain, and verification controls; it reduces risk but never claims that a
system cannot be compromised. `project-architecture` and `complex-change-planning` also apply to
detected software projects and route ADR/scalability and specification-audit/independent-review
playbooks through references. `legacy-preservation` is a dedicated always-on Skill for detected
software projects; `complex-change-planning` keeps only a one-line pointer to it. `frontend-design` accompanies recognized web and mobile frontend
surfaces through matching facets. Its compact entrypoint requires a subject-specific
design contract, progressively routes marketing/editorial versus product/mobile guidance, rejects
unjustified model-default aesthetics, and requires bounded rendered visual review before a design
claim is treated as verified.

Stack evidence describes the whole Workspace. The resolver does not narrow the pack from the
current Task `stack_hints`. Built-in descriptions state when the host should load each projected
skill, so host-native progressive disclosure remains discriminating inside the project pack.

Dashboard settings preview exact skill additions/removals per Workspace with the same resolver
and runtime host profiles used by Apply. A bounded read-only snapshot distinguishes selection,
current projected bytes, pending refresh, collisions, absent hosts and source-overlay exclusion;
current files never imply current-conversation loading. Settings SSE also observes filesystem
repairs. Python stdlib/embedded-web detection uses fresh indexed bytes, bounded AST evidence and
conservative lexical bindings; it does not turn Task hints into selection signals. Dart/Flutter
instructions remain progressive references in the existing catalog. See
[ADR-0072](docs/decisions/0072-skill-evidence-and-delivery-preview.md).

Projection is host-native and owned by adapters.

Important compatibility rule: several hosts scan overlapping compatibility directories. In particular, Cursor loads `.agents/skills`, `.cursor/skills`, and compatibility skill trees including Claude/Codex locations. Therefore a naïve strategy that copies the same Harness skill to every host directory can produce duplicate model-visible skills.

Projection design must include:

- a per-Workspace projection plan;
- collision detection by skill name and target path;
- Harness ownership marker/manifest outside portable `SKILL.md` where necessary;
- exact cleanup of Harness-owned artifacts only;
- preference for shared `.agents/skills` where multiple active hosts natively support it and semantics match;
- leftover `.claude/skills` remains on Cursor's visible compatibility roots so retired Harness-owned files can be cleaned, not as an active Claude projection ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md));
- `.git/info/exclude` for generated project artifacts where appropriate, never silent `.gitignore` mutation.

Skill hot reload is an optimization, not a correctness requirement.
Harness does not rotate project Skills by Task
([ADR-0042](docs/decisions/0042-project-stack-skill-selection.md)). The host receives the stable
project-visible pack; host-native selection chooses which Skill to use. Harness MCP does not deliver
skill bodies or treat `recommended_skills` as instruction delivery. Optional host hot reload of
changed projected files is not scored as Harness current-session delivery. Restart remains the
fallback when a host does not refresh changed files.

ADR-0032 closes the lifecycle gap between resolution and projection for project/index changes.
Installed host intent for every supported profile is durable daemon-adjacent state. Foreground
`scan` reconciles skills synchronously, while the Workspace watcher repeats resolution after
authoritative index changes. Task mutations do not enqueue skill reconciliation. Projection
failure remains a
repairable integration condition reported by doctor; it never rolls back or duplicates committed
Task state.

Lifecycle skill reconciliation refreshes the Workspace Structural Index under the daemon's
existing scan lock before resolving relevance. After the existing 30-second lock-acquisition
budget, refresh receives a fresh 180-second daemon deadline; the IPC timeout is 220 seconds. This
keeps the operation bounded, prevents watcher contention from consuming the repair budget, and
allows large stale generated-tree indexes to be removed after exclusion-policy changes.
Installation must not depend on the watcher having caught up after a daemon restart or offline
edits. Manifest parsing/currentness failures are
reported as relevance-resolution failures, separately from invalid durable Project skill policy;
bounded operator messages do not expose manifest content or nested exception text. Invalid
manifests and projection ownership collisions still fail closed.

## 15. Normal and Hidden visibility modes

Harness exposes a durable Project-level `visibility_mode` with exactly two v1 values. New Projects default to `normal`; changing the mode is an explicit operator/human action and is not exposed as a model-facing MCP mutation.

- `normal`: ordinary native-host SCM behavior is allowed subject to user/host permissions; Harness does not suppress host attribution by default.
- `hidden`: the agent remains an editing/research assistant, and durable SCM publication is human-owned. ADR-0028 makes this policy hygiene-effective: Harness projects untracked always-on host rules or owned project developer instructions plus Git-local excludes, and `project_status` reports `hidden` so the model must not publish. Host `scm_write_enforcement` is still required for *enforced* Hidden (ADR-0003); Codex and Cursor do not provide it, and operator surfaces must not claim that either host blocks git/PR.

Hidden mode also strengthens project-artifact hygiene:

- canonical Harness state/skills/rules remain outside repositories;
- host-required project projections are untracked and ignored through the path resolved by `git rev-parse --git-path info/exclude` (logically `$GIT_COMMON_DIR/info/exclude`);
- `.gitignore` and tracked instruction/config files are never modified merely to activate Hidden mode;
- tracked-path or unknown-user-file collisions fail before materialization;
- `assume-unchanged` and `skip-worktree` are not used as ignore mechanisms;
- cleanup removes only Harness-owned files and exclude entries.

`info/exclude` is not a security boundary: standard Git can force-add ignored files. Hidden correctness therefore depends on host-specific enforcement that denies agent-originated SCM mutations, with prompt rules and optional Git hooks only as defense-in-depth.

Because `$GIT_COMMON_DIR/info/exclude` is shared across linked worktrees, all Harness Workspaces resolving to the same Git common directory use the same effective visibility mode in v1. Contradictory per-worktree modes are rejected.

Hygiene-effective Hidden (ADR-0028, amended by ADR-0073) is operator-selected Project policy plus local instructions and `info/exclude`. It does not refuse MCP admission on Codex or Cursor. Codex uses exact project-scoped `developer_instructions` in Harness-owned `.codex/config.toml` and projects an owned root `AGENTS.override.md` carrying the bootstrap and Hidden text. Existing user or tracked `AGENTS.md` remains byte-for-byte untouched; the override directs Codex to read it after status. An existing override without the exact bootstrap fails before the visibility/install mutation. Enforced Hidden (ADR-0003–0006) remains capability-gated: a host/profile is *enforced* only when its adapter has real-host proof for local Hidden instructions, SCM-write enforcement, policy integrity, attribution suppression where applicable, safe projection paths, deterministic cleanup, and mode-transition revocation. Host-profile identity comes from Harness-owned adapter/registration metadata, never from self-reported `clientInfo`. Unknown or prompt-only behavior is not reported as enforced Hidden mode. Provider-side telemetry/analytics and unrelated agents/tools operating outside the Harness integration are outside this repository/SCM visibility contract.

See ADR-0003 and ADR-0028.

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

The implemented Linux/POSIX installation slice supports local Codex CLI/IDE/desktop project config and local Cursor IDE/CLI. `harness install --host cursor|codex|all` performs runtime, ownership, compatible-skill, Hidden-policy, and registered-Workspace preflight before mutation, then replaces a stale daemon only through the frozen schema/package-version/interpreter/code identity contract. Omitted `--host` selects Cursor. `--host all` installs the Codex+Cursor pair. Claude Code is no longer a supported Harness host ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md)). Codex production MCP is an ownership-marked `.codex/config.toml` in each trusted project, with an authenticated daemon-owned Streamable HTTP URL and exact absolute `X-Harness-Workspace-Root`; required initialization validates daemon connectivity, capability, and Workspace before Codex starts. Codex registrations also project an owned root `AGENTS.override.md`, preserving existing user `AGENTS.md`; an existing override must contain the exact bootstrap. Hidden adds exact project `developer_instructions` and Hidden root instructions; Harness never writes Codex trust or user-global config. Cursor remains project-only with the canonical absolute Workspace root, official enable/tool verification, and owned JSON cleanup. Historical untracked interpolation configs migrate; tracked configs require manual adoption. Direct subprocess probes never replace a failing Cursor host verification. Install and uninstall skip registered Workspace roots that cannot be resolved as directories, name them in the CLI, and leave those registry rows for doctor; live Workspaces stay fail-closed for ownership and tracked-config collisions. Generated configs, root instructions, and markers use Git-local exclusions. The Harness source checkout keeps a tracked Cursor overlay; Codex uses the same locally generated private HTTP config as production Workspaces.

`harness init` binds one folder (Git optional). `harness scan` inspects Harness-owned intent, reconciles active Codex/Cursor project config, enables/verifies Cursor, and submits one compatible profile set to daemon-owned skill reconciliation for an already-registered Workspace. `harness uninstall` removes selected host artifacts and reprojects remaining profiles; uninstall-all does not require the Codex CLI to clean owned config. Bare doctor reports Codex CLI/intent/project config separately from Cursor global/project/tool state, daemon runtime, and Project index. Core Task/Knowledge/index logic remains host-neutral. Automated stdio plus Streamable HTTP and installed-wheel tests prove Cursor → Codex continuity; real Codex acceptance exercises the configured HTTP path.

The checkout global-refresh helper activates its default Cursor+Codex set through one
`harness install --host all` lifecycle call, preserving joint preflight and the existing combined
Codex-then-Cursor adapter order. Post-install repair errors
identify the bounded phase, Workspace/root, selected profiles, and safe underlying cause, then state
that integration may be partially updated and provide exact idempotent retry and doctor commands.
The helper preserves the failing lifecycle status and prints no success epilogue after that failure.

ADR-0036 defines source-checkout global dogfood. Its `scan --global-dogfood` path is accepted only
from an external tool-installed interpreter and returns after registration/indexing, before host or
skill reconciliation. A versioned ignored marker selects the route atomically; invalid marker
state fails closed, and disabling preserves canonical Project Intelligence.

The isolated source checkout seeds built-ins into `.harness/skills` during `scripts/dev harness
init` and projects the relevant subset for the compatible development profile graph. The default
is Codex + Cursor through shared `.agents/skills`; `HARNESS_DEV_SKILL_PROFILES` can select another
compatible graph without reading or mutating user-global host state.

Current target profiles:

- Codex (shared CLI/IDE/desktop MCP config where documented)
- Cursor
- Antigravity IDE/CLI behavior represented by explicit adapter capabilities/profile data where their skill/config surfaces differ

Claude Code is retired as a Harness host; leftover `.claude/skills` visibility and Hidden-rule cleanup remain on Cursor ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md)).

Do not branch core business logic on host identity.

## 17. Dashboard

The dashboard uses the Python stdlib loopback HTTP server. HTML/CSS/JavaScript assets, Project/Workspace/Task drill-down, Task lookup, and SSE freshness hints live at the loopback root without a path token. Realtime remains presentation-only and does not create another source of truth. The listener starts with `harnessd`. Chrome copy is Russian and limited to the current work process. Persisted Task titles, summaries, next steps, and Knowledge cards are shown as stored; MCP instructions tell agents to write those fields in Russian.

The daemon also owns Codex's Streamable HTTP MCP endpoint on `127.0.0.1:17375` (isolated
development: `17376`). It is a separate authenticated protocol surface, not part of the dashboard
UI. Both listeners reuse the same persistent private capability, but Codex sends it as a bearer
header and must also send the exact project-scoped Workspace root. MCP initialization validates
both daemon reachability and Workspace identity before returning success. See ADR-0037.

Dashboard rules:

- bind loopback only by default, on `127.0.0.1:17373` for the canonical per-user daemon and `127.0.0.1:17374` for an isolated checkout;
- serve the operator UI at that loopback root (`http://127.0.0.1:17373/`); persist `dashboard.token` next to the selected database as the Codex bearer, not a dashboard path secret ([ADR-0040](docs/decisions/0040-dashboard-root-url-and-project-index.md));
- start with the daemon; do not require a separate `harness dashboard` start step;
- same daemon/domain state as MCP;
- show Project cards on the home page and sidebar links to `/projects/{id}/`, with direct Task
  and private-vault navigation, plus home Task search and a bounded Task list that pins live
  (`working`/`waiting`) Tasks ahead of recency; do not present Workspaces as copies;
- every Project screen shares Overview / Tasks / Notes/access / Settings navigation. Settings live
  at `/projects/{id}/settings/`, including skill scope, visibility and Project management. The legacy
  `/projects/{id}/#skill-scope` anchor links to the new location. Existing Workspace/Task routes remain;
- notes links preserve an explicitly validated source Workspace/Task in the dashboard wrapper and
  offer a direct return to that Task; these navigation identities never enter the private frame;
- show only observed activity, never claim access to model internal reasoning;
- state transitions (accept, feedback, cancel, Hidden/Normal) and registry mutations call daemon-owned domain services rather than editing dashboard-local state;
- mutation POSTs require the exact loopback Host and either a matching same-origin Origin or, when Origin is absent or `null`, `Sec-Fetch-Site: same-origin`; a foreign Origin stays non-mutating;
- Hidden/Normal operator control is on Project and Workspace detail;
- SSE is for dashboard realtime UI and is unrelated to deprecated MCP SSE transport; events carry freshness hints only, not Task/source payloads.
- dashboard navigation/search/actions must remain progressively usable without JavaScript; JavaScript may enhance freshness but must not become mutation authority.
- SSE and the explicit refresh control re-fetch the current same-origin HTML and replace the rendered layout in place; they must not force a full page navigation. Dirty operator input still blocks automatic apply ([ADR-0043](docs/decisions/0043-dashboard-in-place-html-refresh.md)).
- JavaScript enhances action forms with immediate saving feedback and applies the existing POST/303 HTML response in place. Concurrent submissions and competing refresh responses are suppressed; drafts and fresh server revision tokens remain protected. Workspace/Project live status reuses its request-local applicability proof to avoid duplicate identity inspections ([ADR-0068](docs/decisions/0068-dashboard-mutation-response-latency.md)).
- dashboard assets stay same-origin so CSP can forbid inline script/style.
- operator copy must not explain the product, loopback trust model, or Harness architecture.

Task cards expose the latest checkpoint's reported verification and its revision/time, with
historical verification attached to its original timeline checkpoint. An empty latest report does
not inherit older success; human acceptance remains the existing explicit CAS operation. Metrics
count all durable active/review Tasks, including multiple waiting Tasks in one Workspace. Home and
Workspace history pages contain 24 Tasks; Task timeline pages contain 60 events, with real
previous/next links and page-aware SSE snapshots. Older pages retain the latest Task header.

Failed same-origin POSTs preserve supported editable drafts and reconstruct available retry controls
from fresh authoritative state. Automatic refresh yields to dirty controls; explicit refresh
preserves compatible drafts and interaction state without restoring hidden revision tokens. When
the matching form disappeared, the draft remains available instead of being applied elsewhere.
See [ADR-0065](docs/decisions/0065-dashboard-evidence-history-and-draft-recovery.md).
Shared navigation, operator-focused layout, settings routing and draft-aware navigation follow
[ADR-0070](docs/decisions/0070-project-navigation-and-operator-focus.md). Task decisions precede
history in reading order; unavailable Workspaces retain an immediate relocation action.
- Task cards, Task lists, Task facts, and checkpoint timeline entries always show the durable Git branch recorded for that Task (latest checkpoint, otherwise the Task baseline). That identity is not the live Workspace checkout. Detached HEAD is shown as `(detached)`; Tasks that predate baseline capture show an em dash.
- Task detail supports bounded operator comments, one Jira link, the `deploy_test`/`deploy_prod` marker, and explicit reopen of terminal Tasks. Overview cards show the marker and direct Jira navigation when present. These fields are operator state, not additional Task lifecycle states.
- Project and Workspace detail support explicitly confirmed deletion of the logical Project and its Harness-owned durable state without touching repository files. The deletion form renders in Project settings; its POST must match that Project identity (the legacy Project URL remains accepted). Workspace settings and unavailable Workspace detail support explicit relocation to a canonical live Git path while preserving Project/Workspace/Task/Knowledge identity; relocation clears only rebuildable index rows and the watcher repopulates them from the new root. Relocation POST identity must match the Workspace page that rendered it.

## 18. Security and privacy boundaries

Personal project notes/credentials use a separately owned KDBX4 file and subprocess, with an
independent browser origin and unlock bearer. Only public Project identity/name pass from the
dashboard into the frame. Private records, keys and sessions never enter core SQLite, IPC, MCP,
Knowledge or SSE. Successful private writes require a durable full backup first;
restore retains the prior file. These backups are independent of Harness database recovery.
An explicit no-password mode uses empty-password KDBX, opens automatically and has no idle lock;
its files/backups have no password-based confidentiality. Mode transitions retain a prior snapshot
and commit the new mode in the KDBX itself, without a separate key/mode-file transaction.
In password-protected mode, opt-in automatic entry uses the Linux desktop Secret Service; no file keyring fallback exists.
Navigation preserves a short-lived bearer in the private origin's sessionStorage, never record
content or the master password. Explicit/idle lock pauses automatic entry; a normal restart
can open through the OS keychain again. Keychain enrollment is independent of KDBX backups.
This is not OS-user isolation against unrestricted same-account shell/browser access. See
[ADR-0069](docs/decisions/0069-project-hub-and-private-vault.md) and the
[operator guide](docs/private-vault.md).

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
- Task lookup acceptance fixtures;
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
    hosts/
    ipc/
  interfaces/
    cli/
    mcp/
    web/
tests/
  unit/
  integration/
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
