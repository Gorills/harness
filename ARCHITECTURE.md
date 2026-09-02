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

### MCP adapters

A thin host-facing adapter exposed through stdio where accepted and through daemon-owned
Streamable HTTP for Codex. It:

1. reaches daemon-owned state through local IPC; the Codex HTTP adapter runs with `harnessd`, so
   Codex never needs access to the Unix socket;
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

Operator tracking is orthogonal to lifecycle state. A Task may carry one Jira URL and one nullable delivery marker (`deploy_test` or `deploy_prod`), plus immutable operator comments in its event history. These human-maintained fields never substitute for `working`/`waiting`/`completed`/`cancelled` or a waiting reason. Their mutations use the same explicit Task identity and revision CAS as every other existing-Task write.

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

- Cursor interpolates `HARNESS_WORKSPACE_ROOT=${workspaceFolder}` into project `.cursor/mcp.json`: the implemented stdio root signal.
- A leftover `HARNESS_HOST_PROFILE=claude-code` process is an unsupported profile ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md)); overlay refuse may still use `CLAUDE_PROJECT_DIR`.
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

`project_context` is not a mandatory step after every search hit. Knowledge and Task refs use it
for selected semantic context. Code and doc hits that already include an exact path may be read
with targeted native tools immediately; `project_context` remains available for metadata
verification.

`task_start` / resume is required before diagnosis and native edits, including read-only
investigation. Broad discovery, including `project_search`, happens inside that Task. A failed
schema or tool call is a blocker: retry from the public schema rather than skipping Harness.
Checkpoint after each logical stage, even when no files changed. A new operator request or a
diagnosis-to-implementation shift completes or waits the current Task, then starts a new one.
Specification §71's "before meaningful changes" reading is superseded by
[ADR-0038](docs/decisions/0038-task-required-for-diagnosis-and-schema-retry.md).

But the implementation is explicitly workspace-domain based and write-safe:

- `task_start` without `task_id` creates a new Task only when the Workspace has no distinct `working` Task. Creation has no prior Task revision; the one-working-Task invariant and creation are enforced in one transaction, and the response returns the new `task_id` plus initial `revision`.
- `task_start` with `task_id` resumes an existing Task. If that Task is already the Workspace's `working` Task, resume is idempotent/read-like and returns its current revision without mutating Task state.
- If resume would mutate an existing Task (for example `waiting → working`), `task_start` MUST also include `expected_revision`; the transition uses the same compare-and-set rule as every other existing-Task mutation and returns the incremented revision.
- `completed` and `cancelled` Tasks are never reopened by `task_start`. Dashboard `task_reopen` is a separate human-only CAS operation that preserves Task identity, appends a `reopened` event, and still obeys the one-working-Task-per-Workspace invariant.
- Starting a different Task while the Workspace already has a `working` Task is a conflict; `task_start` must not silently replace it.
- Read-only calls may derive relevance/display defaults from Workspace + current Task and expose the current Task revision where a subsequent mutation may depend on it.
- `task_checkpoint` is a mutating operation and therefore MUST include both the intended Harness `task_id` and `expected_revision`.
- Agent-reported checkpoint verification is bounded semantic evidence (`name`, `passed|failed|not_run`, `evidence`) persisted atomically with the checkpoint; mechanical Git evidence remains Harness-derived.
- The daemon verifies Task/Workspace ownership and transition validity, then applies an existing-Task mutation only when the stored revision equals `expected_revision`; success increments and returns the new revision.
- Revision mismatch or a required-but-missing `expected_revision` is a bounded conflict/error with no state/event/knowledge mutation. The caller must refresh/reconcile before retrying; Harness must not silently replay stale semantic content against the newer Task state.
- A stale call for Task A must never be retargeted to whichever Task is current when the request executes, and a stale writer for Task A must never overwrite a newer checkpoint for Task A.
- Dashboard Task mutations (`Accept`, feedback, comment, Jira/status update, reopen, cancel) use the same revision precondition at the application boundary; interfaces do not get a concurrency bypass.
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

The model-visible `index` object is a cheap SQLite snapshot: `indexed_file_count` is the current Structural Index inventory, `content_search_document_count` is the number of those current search documents that actually participate in code/docs content FTS retrieval (`indexed_search_documents` joined to `indexed_content_search`), and last-known reconcile provenance (`index_revision`, `last_successful_reconcile_at`, `last_reconcile_kind`) records when and how the index last successfully reconciled, including a no-op persist that still advances the watermark. Binary, NUL, invalid-UTF-8, symlink, oversized, and generated `.log`/`.out` paths can remain in the mechanical inventory without becoming content documents. A zero search hit is therefore not proof of absence across every indexed file. Provenance is last-known success, not proof that nothing changed afterwards and not a live freshness claim. Status must not reread Workspace source, run a freshness scan, or add Git work to compute these index fields.

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
- cancellation/timeouts, with command-specific bounds (status stays short; search is longer than
  status so a large Workspace inventory cannot surface to MCP as `local IPC request timed out`);
- bounded concurrent client handling so one slow request does not head-of-line block unrelated callers;
- one SQLite connection per accepted IPC worker rather than sharing a connection across threads;
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

The exact required FTS5 capability set, including contentless-delete tables, must be checked by
`harness doctor` at runtime because Python/SQLite builds are an environment capability, not a safe
universal assumption.

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

Incremental watcher observations are debounced/coalesced and reconciled against the filesystem.
Idle polls use a subprocess-free metadata token over directory/Git-control state plus one rotating
128-path metadata shard; Git status/HEAD confirmation runs only after that token changes. A bounded
same-HEAD dirty-path set is reconciled through the canonical index/FTS/Knowledge rules without
hashing unrelated files. Initial, HEAD/policy/unknown/large changes and the periodic safety pass
remain full authoritative scans. Watcher observations are hints; the filesystem is authoritative.
Rename may be observed as delete+create and must still converge correctly.

`ParserAdapter` remains a narrow language parsing boundary. Unsupported languages degrade to paths/text/docs/Git rather than failing the project.

The implemented lexical projection indexes regular UTF-8 code/docs up to 1 MiB during the same
authoritative reconciliation. It revalidates stable bytes against the mechanical SHA-256 snapshot,
skips symlinks/binary/NUL/invalid-UTF-8/oversized content, and writes a contentless FTS5 index in the
same transaction as `indexed_files`. Generated `.log`/`.out` diagnostic artifacts remain in the
mechanical inventory but are not treated as code/docs search content. The durable mapping contains
no readable source body. A schema migration creates only the empty derived structure; live source is
backfilled by the next watcher or explicit scan.

Successful full and incremental reconciles persist last-known provenance in the same SQLite
transaction (`workspace_index_reconcile`: monotonic per-Workspace `index_revision`, timezone-aware
UTC `last_successful_reconcile_at`, and `last_reconcile_kind` `full` or `incremental`). A failed
reconcile rolls back without advancing those fields. An unchanged inventory that still commits is a
successful reconcile and still advances those fields. Watcher in-memory `last_reconciled_at` is not
the source of truth. New Workspaces have no provenance row until the first successful scan; status
then reports JSON nulls rather than fake zeros.

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

Task-history FTS fragments include Task titles, checkpoint summaries/next steps, durable Git branches, operator feedback/comments, Jira links, and operator delivery markers. Dashboard Workspace search combines these bounded Project-scoped Task hits with its existing Workspace-local indexed-path hits; the home dashboard searches Task history across every registered Project. Both channels return metadata/history only and never raw source.

The implemented Project Intelligence retrieval boundary is daemon-owned. Workspace hints resolve exactly one registered Workspace, the daemon validates its live Git identity before and after the read, and one read transaction fixes the corresponding Project identity. Current code/docs remain Workspace-local structural-index data; Knowledge and Task-history channels are Project-scoped. Rebuildable FTS5 tables are candidate/ranking indexes only: selected search/context payloads are reread from authoritative `indexed_files`, `knowledge_cards`/anchors, Tasks, checkpoints, and events. Contentless code/docs FTS never stores source bodies and is never queried with `snippet()`/`highlight()` as if it did. After FTS selects a code/doc candidate, `project_search` may attach a bounded current-source evidence window only when a live Workspace-contained regular-file reread (same stable-entry, containment, size, and UTF-8 checks as indexing) matches the indexed content SHA and the significant query terms still relocate in that text. A changed file keeps its locator and returns `evidence=null` with `evidence_reason=changed_since_index`; an unsafe or unrelocatable match uses `current_match_not_relocated`; dropping already-built evidence because the 12 KiB payload is full uses `response_budget`; path-only hits without a content document use `path_only`. Knowledge and Task hits never attach file evidence. Evidence is capped per snippet (lines/bytes) and per response (evidence-bearing hits), and the existing 12 KiB `project_search` budget still holds. Cross-Project refs fail closed. `project_context` expands only explicit bounded refs; source code is never returned there and remains a native-host read. `needs_revalidation` Knowledge is labelled as historical evidence and ranked after fresh Knowledge.

Natural queries use bounded Unicode/camel/snake term normalization, conservative English/Russian
filler removal, and prefix/inflection alternatives. Retrieval ranks explicit evidence tiers—exact
path, exact filename, exact filename stem, title/identifier phrase, all significant terms, then
partial match. BM25 is only an intra-channel candidate order. Code/docs results require every
significant query term across their indexed title, path, normalized identifiers, and lexical body;
one common normalized token cannot create a partial multi-term hit. Test and archived paths receive
only same-tier penalties unless the query explicitly requests them. For `scope=all`, comparable
quality/coverage tiers are ordered first and uncalibrated channel ranks are then deterministically
interleaved; this avoids presenting a heterogeneous BM25 value as a global score while allowing
directly relevant fresh Knowledge to beat a general lexical hit. Current-Task state is a boost, not
a hard filter or a substitute for query relevance. More sophisticated RRF/Working-Set/graph ranking
can replace this internal fusion later without changing refs or tool shapes.

## 13. Knowledge and staleness

Knowledge is deliberately sparse. A card is created only when it can plausibly prevent future re-investigation.

Every card has provenance. Code-related cards should be anchored to file/symbol fingerprints when possible.

On anchor change:

```text
fresh → needs_revalidation
```

Stale knowledge is retained as a historical clue, receives ranking penalty, and cannot be presented as a current verified fact. Harness never invokes an LLM automatically to repair it.

## 14. Skills architecture

Canonical Harness skill registry lives outside repositories. Runtime load, built-in sync, doctor, and purge preflight share one fail-closed local filesystem-trust check: an existing registry root must be a real current-user directory without group or other write; missing roots stay empty or skip, unsafe existing roots are refused rather than chmod'd, and prepare also requires the immediate parent to meet that same owner/write contract (custom `HARNESS_SKILL_REGISTRY` ancestor replacement is out of v1). The resolver selects a relevant subset using deterministic project stack and explicit include/exclude configuration. Harness ships a compact built-in quality pack into that registry through ownership-aware reconciliation. The built-ins are intent-oriented, composed through stack applicability, and validated against supported host surfaces; no second workflow/composition DSL is introduced. Detailed Docker, frontend discoverability, language-native, mobile, server, game, operations, and security guidance uses portable nested `references/` that the selected skill routes to only when relevant. The canonical pack may exceed the model-visible budget; the resolver still projects at most the configured bounded subset. Same-id unknown or user-modified canonical content, including nested reference content, is never overwritten. When an ID leaves `BUILTIN_SKILLS`, exact-owned stale trees are removed through the same replacement-backup path as updates; user-modified stale trees stay as user-owned skills and leave the ownership manifest. Successful restore returns exact pre-sync trees, including retirements. If restore fails, remaining replacements still roll back, surviving backups are preserved, and sync raises an explicit recovery failure that includes the surviving backup path rather than re-raising only the original error.

The quality baseline includes explicit local/test/production container operations, Google/Yandex
public-route discoverability and web performance, project architecture, change quality (including
legacy compatibility), language-native correctness/tooling, and durable data integrity. Stack-derived
applicability keeps language and domain guidance automatic after manifests/source appear. Task
`stack_hints` remain
optional Task metadata and are not a Skill selector. Dependency matching retains the
existing portable exact-token contract, while deterministic derived facets capture cross-signal
project roles such as `web-frontend`, `mobile-app`, `backend-service`, `godot-project`, and
`deployment-ops`. Facets are calculated with manifest locality where needed: an Expo/React Native
package is mobile even when its compatibility dependencies include React DOM, and it does not make
`public-frontend` relevant unless independent web evidence exists. Stack detection also parses Dart
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
detected software projects and route ADR/scalability and specification-audit/independent-review/legacy
playbooks through references. `frontend-design` accompanies recognized web and mobile frontend
surfaces through matching facets. Its compact entrypoint requires a subject-specific
design contract, progressively routes marketing/editorial versus product/mobile guidance, rejects
unjustified model-default aesthetics, and requires bounded rendered visual review before a design
claim is treated as verified.

Stack evidence describes the whole Workspace. The resolver does not narrow the pack from the
current Task `stack_hints`. Built-in descriptions state when the host should load each projected
skill, so host-native progressive disclosure remains discriminating inside the project pack.

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

Hygiene-effective Hidden (ADR-0028) is operator-selected Project policy plus local instructions and `info/exclude`. It does not refuse MCP admission on Codex or Cursor. Codex uses exact project-scoped `developer_instructions` in Harness-owned `.codex/config.toml`, so existing `AGENTS.md` remains byte-for-byte untouched; unknown/user-owned config that Harness cannot safely reconcile still fails before the visibility/install mutation. Enforced Hidden (ADR-0003–0006) remains capability-gated: a host/profile is *enforced* only when its adapter has real-host proof for local Hidden instructions, SCM-write enforcement, policy integrity, attribution suppression where applicable, safe projection paths, deterministic cleanup, and mode-transition revocation. Host-profile identity comes from Harness-owned adapter/registration metadata, never from self-reported `clientInfo`. Unknown or prompt-only behavior is not reported as enforced Hidden mode. Provider-side telemetry/analytics and unrelated agents/tools operating outside the Harness integration are outside this repository/SCM visibility contract.

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

The implemented Linux/POSIX installation slice supports local Codex CLI/IDE/desktop project config and local Cursor IDE/CLI. `harness install --host cursor|codex|all` performs runtime, ownership, compatible-skill, Hidden-policy, and registered-Workspace preflight before mutation, then replaces a stale daemon only through the frozen schema/package-version/interpreter/code identity contract. Omitted `--host` selects Cursor. `--host all` installs the Codex+Cursor pair. Claude Code is no longer a supported Harness host ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md)). Codex production MCP is an ownership-marked `.codex/config.toml` in each trusted project, with an authenticated daemon-owned Streamable HTTP URL and exact absolute `X-Harness-Workspace-Root`; required initialization validates daemon connectivity, capability, and Workspace before Codex starts. Hidden adds exact project `developer_instructions`, while Harness never writes Codex trust, user-global config, or `AGENTS.md`. Cursor remains project-only with interpolated `${workspaceFolder}`, official enable/tool verification, and owned JSON cleanup. Install and uninstall skip registered Workspace roots that cannot be resolved as directories, name them in the CLI, and leave those registry rows for doctor; live Workspaces stay fail-closed for ownership and tracked-config collisions. Generated configs and markers use Git-local exclusions. The Harness source checkout keeps a tracked Cursor overlay; Codex uses the same locally generated private HTTP config as production Workspaces.

`harness scan` inspects Harness-owned intent, reconciles active Codex/Cursor project config, enables/verifies Cursor, and submits one compatible profile set to daemon-owned skill reconciliation. `harness uninstall` removes selected host artifacts and reprojects remaining profiles; uninstall-all does not require the Codex CLI to clean owned config. Bare doctor reports Codex CLI/intent/project config separately from Cursor global/project/tool state, daemon runtime, and Project index. Core Task/Knowledge/index logic remains host-neutral. Automated stdio plus Streamable HTTP and installed-wheel tests prove Cursor → Codex continuity; real Codex acceptance exercises the configured HTTP path.

ADR-0036 defines source-checkout global dogfood. Its `scan --global-dogfood` path is accepted only
from an external tool-installed interpreter and returns after registration/indexing, before host or
skill reconciliation. A versioned ignored marker selects the route atomically; invalid marker
state fails closed, and disabling preserves canonical Project Intelligence.

The isolated source checkout seeds built-ins into `.harness/skills` during `scripts/dev harness
scan` and projects the relevant subset for the compatible development profile graph. The default
is Codex + Cursor through shared `.agents/skills`; `HARNESS_DEV_SKILL_PROFILES` can select another
compatible graph without reading or mutating user-global host state.

Current target profiles:

- Codex (shared CLI/IDE/desktop MCP config where documented)
- Cursor
- Antigravity IDE/CLI behavior represented by explicit adapter capabilities/profile data where their skill/config surfaces differ

Claude Code is retired as a Harness host; leftover `.claude/skills` visibility and Hidden-rule cleanup remain on Cursor ([ADR-0039](docs/decisions/0039-retire-claude-code-host.md)).

Do not branch core business logic on host identity.

## 17. Dashboard

The dashboard uses the Python stdlib loopback HTTP server. HTML/CSS/JavaScript assets, Project/Workspace/Task drill-down, bounded indexed-path search, and SSE freshness hints live at the loopback root without a path token. Realtime remains presentation-only and does not create another source of truth. The listener starts with `harnessd`. Chrome copy is Russian and limited to the current work process. Persisted Task titles, summaries, next steps, and Knowledge cards are shown as stored; MCP instructions tell agents to write those fields in Russian.

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
- show a sidebar of Project links to `/workspaces/{id}/`, plus home Task search and recent Tasks; do not present Workspaces as copies or a second dashboard;
- show only observed activity, never claim access to model internal reasoning;
- state transitions (accept, feedback, cancel, Hidden/Normal) and registry mutations call daemon-owned domain services rather than editing dashboard-local state;
- mutation POSTs require the exact loopback Host and either a matching same-origin Origin or, when Origin is absent or `null`, `Sec-Fetch-Site: same-origin`; a foreign Origin stays non-mutating;
- Hidden/Normal operator control is on Project and Workspace detail;
- SSE is for dashboard realtime UI and is unrelated to deprecated MCP SSE transport; events carry freshness hints only, not Task/source payloads.
- dashboard navigation/search/actions must remain progressively usable without JavaScript; JavaScript may enhance freshness but must not become mutation authority.
- dashboard assets stay same-origin so CSP can forbid inline script/style.
- operator copy must not explain the product, loopback trust model, or Harness architecture.
- Task cards, Task lists, Task facts, and checkpoint timeline entries always show the durable Git branch recorded for that Task (latest checkpoint, otherwise the Task baseline). That identity is not the live Workspace checkout. Detached HEAD is shown as `(detached)`; Tasks that predate baseline capture show an em dash.
- Task detail supports bounded operator comments, one Jira link, the `deploy_test`/`deploy_prod` marker, and explicit reopen of terminal Tasks. Overview cards show the marker and direct Jira navigation when present. These fields are operator state, not additional Task lifecycle states.
- Project detail supports explicitly confirmed deletion of the logical Project and its Harness-owned durable state without touching repository files. Workspace detail supports explicit relocation to a canonical live Git path while preserving Project/Workspace/Task/Knowledge identity; relocation clears only rebuildable index rows and the watcher repopulates them from the new root. Destructive and relocation POST identities must match the detail page that rendered them.

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

Optional Codex JSONL classification (`scripts/eval_search_behavior.py`, sanitized metrics on `accept_codex --run-model`) is acceptance evidence for search-vs-native-grep behavior, not a daemon or MCP contract.

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
