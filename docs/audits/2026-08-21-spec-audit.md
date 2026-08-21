# Independent audit of Harness specification v1.0

- **Audit date:** 2026-08-21
- **Specification:** `docs/specification.md`, version 1.0, status “Approved architecture baseline”
- **Method:** critical review of product invariants, process/domain boundaries, MCP assumptions, host integration claims, skill projection, testability, persistence/security, and current official documentation for MCP, Claude Code, Codex, Cursor, and Google Antigravity.
- **Outcome:** product direction is sound, but the implementation baseline requires several explicit amendments before coding.

## Executive assessment

The specification has an unusually strong product center: Harness is an accelerator/control plane, not a replacement agent; filesystem/Git remain authoritative; progressive disclosure is a first-class API constraint; semantic knowledge is sparse/provenanced; the model-facing surface is intentionally small; and proprietary-host uncertainty is already separated from core automated proof.

Those choices survive independent review.

The main defect is not product scope but **protocol-era coupling**. The specification treats “MCP session” as a stable runtime object to which a Task can be bound. MCP 2026-07-28 removed protocol-level sessions on the modern path, and the official Python SDK v2 is now stable around that model. Implementing sections 51/62 literally would introduce hidden state coupled to legacy protocol behavior.

The second major gap is **Workspace resolution**. A globally registered stdio server must know which project/worktree a host means. The specification assumes this context exists but never defines a cross-host contract. Claude Code provides a documented root environment variable; the other hosts need adapter-specific mechanisms and real-host acceptance before their current-workspace semantics can be treated as facts.

The third material risk is **skill projection collision**. Portable Agent Skills are a good direction and progressive disclosure is validated by current host docs, but skill roots overlap. Cursor intentionally loads Claude/Codex compatibility directories. Materializing the same Harness skill into every host-native project location can therefore surface duplicates.

Subject to the amendments below, the modular-monolith/SQLite/local-first architecture is an appropriate v1 baseline.

## Verdict legend

- **ACCEPT** — implement as specified; detail may remain internal.
- **AMEND** — product intent stands, but implementation contract must change.
- **HOST ACCEPTANCE** — cannot be proved by core automated tests or docs alone; requires maintained real-host verification.
- **DEFER** — valid idea but should not become v1 architecture until evidence requires it.

## 1. Product invariants and non-goals

### Global install + project-local context — ACCEPT

All four target host families document global/user-level MCP configuration, so “install once, use across projects” is feasible. Isolation must be enforced by Harness Workspace resolution and query scoping, never inferred from a host name.

### Native-agent-first — ACCEPT

Stdio MCP is documented by Claude, Codex, Cursor, and Antigravity. Keeping native file editing, shell, Git, and browser untouched is a sound compatibility/failure-isolation strategy.

### Progressive disclosure — ACCEPT, strengthened

The specification’s small bootstrap/search/context design matches modern host behavior. Codex and Antigravity explicitly document progressive skill disclosure, and Codex publishes an initial skill-list budget. Response byte/item budgets and negative-disclosure tests should be treated as API compatibility requirements.

### Five model-facing tools — ACCEPT

The surface is coherent and maps to distinct domain operations. Do not add a tool merely to expose internal index maintenance or host lifecycle events.

### Non-goals — ACCEPT

Avoiding cloud infrastructure, distributed stores, plugin marketplaces, orchestration DSLs, raw-code vector databases, and agent replacement is consistent with v1 needs.

## 2. MCP protocol and SDK

### Official MCP Python SDK — ACCEPT with version baseline

Use the official `mcp` Python SDK. As of this audit, v2 is the current stable release line and supports MCP 2026-07-28 plus earlier revisions. Python 3.13 is compatible with the SDK’s Python >=3.10 requirement.

Do not build production JSON-RPC/MCP framing by hand. Keep a raw test client only as an independent wire assertion tool.

### “MCP session” as a domain entity — AMEND

MCP 2026-07-28 removes the modern handshake/session model. There is no modern `Mcp-Session-Id` invariant on which Harness may rely.

Correct interpretation:

- Harness may observe a stdio bridge process lifetime and call that an agent activity/session record.
- Harness mints that record's identifier itself.
- It can store self-reported client info for display/debugging.
- It cannot treat protocol-session identity as durable Task binding, authorization, or routing state.

### `task_start` binds MCP session to Task — AMEND (critical)

Replace transport-hidden binding with a domain transition:

- `task_start` creates/resumes and makes a Task current for a Workspace.
- Subsequent calls derive current Task from Workspace state when unambiguous.
- Bridge activity may be associated with the Task for history, but a bridge restart cannot erase binding.
- Ambiguous recovery/admin states fail safely or require explicit task identity.

This preserves the intended low-ritual workflow and is compatible with stateless MCP.

### MCP roots as a project-resolution dependency — REJECT for correctness

Current MCP/SDK documentation marks roots as deprecated. Claude Code still documents roots in its compatibility behavior, but Harness must not make a deprecated protocol feature its cross-host Workspace source of truth.

Roots may be observed as an optional compatibility hint only if the official SDK/host path supplies them; they are not a required subsystem dependency.

### MCP server instructions — ACCEPT as optimization; HOST ACCEPTANCE for natural use

Codex explicitly documents reading server instructions and recommends making the first 512 characters self-contained. The MCP protocol supports server instructions, but proprietary hosts decide how these enter model context and how naturally a model uses tools.

Keep instructions very small and useful; do not treat them as enforcement. Normal agent behavior remains host acceptance.

### Async runtime — ACCEPT with boundary clarification

The product may use `asyncio` as its daemon/application concurrency model. The official MCP SDK v2 uses AnyIO internally. Keep the MCP interface isolated so core domain/application APIs are not coupled to AnyIO-specific primitives without need.

## 3. Workspace and Project identity

### Project/Workspace split — ACCEPT

Separating a logical project from physical worktrees is necessary for safe parallel work and correct dirty/branch/index state.

### Workspace resolution — AMEND (critical missing contract)

The specification needs a first-class internal resolution design because global MCP registration and per-project state otherwise conflict.

Required approach:

1. Host adapter emits documented root hints where available.
2. Normalize/canonicalize the path per platform.
3. Resolve/register the Workspace in the daemon.
4. Refuse ambiguous attachment.
5. Validate host-specific propagation in real-host tests.

Known evidence:

- Claude Code: `CLAUDE_PROJECT_DIR` is a documented strong root hint for stdio MCP servers.
- Codex: global/project MCP config is documented; active-root process semantics still require a real-host test.
- Cursor: global/project config and project `${workspaceFolder}` interpolation are documented; global active-root behavior must not be inferred beyond that contract.
- Antigravity: global/workspace MCP config is documented; runtime current-root propagation still needs real-host acceptance.

### One working Task per Workspace — ACCEPT

This is a valuable complexity bound. Enforce it transactionally, not only as UI guidance.

## 4. Host adapters

### Thin `HostAdapter` — ACCEPT

The adapter responsibilities in the specification are appropriate. Preserve host config mutation/cleanup/doctor/skill projection here and keep domain business logic elsewhere.

### One implementation per named host — ACCEPT with profile capability data

Do not prematurely create separate products for CLI/IDE variants, but adapters may expose explicit profiles/capabilities when official surfaces differ. Antigravity global skill locations differ between IDE documentation and Antigravity CLI migration documentation, so the implementation should model the exact target surface rather than silently assuming one path.

### Host self-identification from MCP `clientInfo` — AMEND

Treat client information as best-effort diagnostics. Current MCP guidance treats it as self-reported and not suitable for security/behavior decisions. Adapter selection should primarily come from the configured integration artifact/launch profile that Harness owns, not from trusting a request's client name.

## 5. Skills registry and projection

### Canonical external registry — ACCEPT

Keeping Harness-owned canonical skills outside repositories and projecting only relevant subsets is sound.

### Portable `SKILL.md` + Harness metadata — ACCEPT

Current Codex, Cursor, Claude, and Antigravity skill systems all use `SKILL.md` conventions compatible with the core idea. Harness-specific applicability belongs outside the portable instruction body.

### “Never materialize all skills” — ACCEPT and validated

Current hosts use metadata-first discovery/progressive disclosure. Codex explicitly caps initial skill-list context. Relevance filtering is a real token/usability feature, not speculative optimization.

### Project skill paths — AMEND to current documented paths

Do not freeze paths in core code. Current documentation checked in this audit shows:

- Claude: `.claude/skills` / `~/.claude/skills`.
- Codex: project `.agents/skills` up the repository path; user `~/.agents/skills`; admin `/etc/codex/skills`.
- Cursor: `.agents/skills`, `.cursor/skills`, user equivalents, plus Claude/Codex compatibility directories.
- Antigravity: workspace `.agents/skills`; IDE global `~/.gemini/config/skills`; Antigravity CLI migration docs list global `~/.gemini/antigravity-cli/skills`.

### Duplicate skills across compatibility roots — AMEND (material risk)

Because Cursor scans compatibility directories, “write each host's copy” is unsafe in a workspace used by several hosts. Projection needs collision detection and a deliberate target plan. Prefer one shared native root where semantics overlap, then add host-specific projection only where required.

This requires real-host acceptance because name de-duplication/selector behavior is host-owned.

### `.git/info/exclude` — ACCEPT with worktree caveat

Avoiding tracked generated artifacts is right. Implementation must account for Git worktree/common-dir behavior and confirm that exclusion changes are scoped correctly. Do not modify `.gitignore` unless explicitly required and owned.

## 6. Structural index, watcher, and source of truth

### Deterministic non-LLM scan — ACCEPT

This is essential for repeatability, offline operation, and testability.

### Filesystem/Git as truth — ACCEPT

The index must be rebuildable and converge after watcher loss/crash. Watcher events should be treated as invalidation hints, not an authoritative event log.

### ParserAdapter/degraded mode — ACCEPT

Do not make Tree-sitter or any parser library a public contract. Unsupported languages should retain path/FTS/docs/Git search.

### Fingerprints/staleness — ACCEPT with definition work needed

The concept is strong. Implementation needs an explicit canonical fingerprint algorithm and tests for formatting-only changes, symbol moves/renames, parser failures, and file deletion. Do not promise perfect semantic identity across refactors.

### Ignore/sensitive patterns — ACCEPT as defense-in-depth

`.gitignore` + `.harnessignore` plus defaults are appropriate. Sensitive-pattern exclusion reduces accidental indexing but must not be described as a secret-management security boundary.

## 7. Search architecture

### SQLite FTS5 + exact + structure + knowledge — ACCEPT

This is proportional to v1 and keeps local operation simple.

### Identifier normalization — ACCEPT

Case/style tokenization is a cheap, deterministic bridge from natural-language concepts to unknown legacy identifiers.

### Reciprocal Rank Fusion — ACCEPT as default candidate

A simple rank fusion avoids premature coefficient tuning. The exact formula remains an implementation detail, but ranking behavior needs acceptance fixtures.

### Embeddings — ACCEPT as optional interface; default should be off/local-capable

Do not bulk-embed raw source in v1. If semantic text embeddings are added, keep provider isolation and explicit external opt-in. A provider interface should not force a concrete embedding dependency into the MVP path.

### Search performance targets — ACCEPT as targets, not correctness gates everywhere

Measure on defined fixture sizes/hardware classes. Keep correctness tests independent from machine-speed flakes; add performance benchmarks/regression thresholds in a controlled environment later.

## 8. Semantic Knowledge

### Learn only from real task investigation — ACCEPT

This is a key differentiator and avoids speculative whole-repository summarization.

### Provenance and anchors — ACCEPT

Knowledge without provenance should be invalid. Code claims should prefer fingerprinted anchors.

### No automatic LLM repair — ACCEPT

Stale semantic knowledge should be marked and revalidated opportunistically through later real work, not through autonomous spend.

### Freshness state — ACCEPT, but avoid boolean semantics

`fresh` / `needs_revalidation` is a good start. Search/presentation contracts must ensure stale cards are clearly historical clues rather than current facts.

## 9. Task lifecycle and human coordination

### Task survives host/session — ACCEPT

This is the right durable unit and is even more important under sessionless modern MCP.

### Minimal states — ACCEPT

Avoid workflow-state explosion. `waiting` + reason is sufficient for the v1 human loop.

### Human Accept/Feedback on same Task — ACCEPT

Feedback should append an event and transition the same Task back to `working`. Do not create a second Task for review corrections unless the operator explicitly chooses a new work item.

### Agent-reported verification — ACCEPT for v1, clearly labeled

Persist evidence as reported, never elevate it to independently observed truth. Future hooks can add observed evidence without changing existing history semantics.

### Baseline and changed-file calculation — ACCEPT with dirty-tree semantics to specify

Capturing HEAD/branch/dirty state on start is valuable. The implementation must preserve enough baseline detail to distinguish pre-existing dirty files from changes after task start; “changed files” cannot safely mean only `git diff` at checkpoint if the Workspace was already dirty.

This is an implementation requirement that should receive dedicated tests.

## 10. Persistence, IPC, migrations, crash consistency

### SQLite WAL modular monolith — ACCEPT

Appropriate for a one-user local daemon and keeps transactional domain transitions easy to test.

### One daemon owns DB writes — ACCEPT

Avoid MCP bridge direct SQLite access. It would create duplicate transaction/business policy and undermine crash recovery.

### IPC choice — ACCEPT as boundary, keep internal protocol independent

Unix socket + Windows named-pipe/equivalent is sensible. Define a small versioned internal schema and strict message limits rather than tunneling raw MCP objects into the daemon.

### FTS5 assumption — AMEND operationally

Keep FTS5 as required v1 capability, but `harness doctor` must verify the runtime SQLite build actually supports it and report a clear failure. Do not assume every Python distribution ships identical SQLite features.

### Migrations/backups — ACCEPT

Destructive migration backup must be designed and tested before a destructive migration exists; do not build a generic migration framework beyond current needs.

## 11. Dashboard

### FastAPI/Jinja/vanilla JS/SSE — ACCEPT

This is a good low-complexity human UI stack for v1.

Dashboard SSE is unrelated to the deprecated MCP SSE transport; there is no architectural conflict.

### “Agent is working” semantics — ACCEPT

Only show observable signals. Do not infer internal model reasoning or claim continuous execution merely from an open client process.

### Shared source of truth — ACCEPT

Dashboard actions must call domain/application services through the daemon, not edit tables ad hoc.

## 12. Security and privacy

### Local-only defaults — ACCEPT

Loopback dashboard and OS-user-only daemon IPC are correct defaults.

### No raw source to external providers by default — ACCEPT

Make external provider enablement explicit, scoped, and auditable.

### No full transcripts by default — ACCEPT

Structured Task/Knowledge/verification/activity records align with the product goal while minimizing sensitive retention.

### Host metadata trust — AMEND explicitly

Self-reported `clientInfo` cannot be an auth boundary. Integration ownership and local OS identity are stronger facts.

### Config mutation safety — strengthen

Global installation touches user-owned host config. Every adapter needs:

- parse-before-write;
- atomic replacement where direct file editing is used;
- preservation of unknown fields/order where practical;
- ownership marker or exact registered identity;
- idempotent install;
- cleanup of Harness-owned entries only;
- backup/recovery strategy for mutation failures.

Prefer official host CLI/API registration when it offers safer semantics than raw config rewriting.

## 13. Testing and proof boundaries

### Core automated proof — ACCEPT, excellent requirement

The specification correctly requires real stdio subprocess tests and final serialized payload assertions. This should be treated as release-critical.

### Negative disclosure — ACCEPT, release-critical

Tests for absence of unrelated knowledge/tasks/file maps are necessary because context size/privacy regressions can pass normal positive assertions.

### Host acceptance matrix — ACCEPT, expand Workspace resolution

Add explicit “correct current Workspace/worktree resolved” and “two simultaneous worktrees are not confused” rows. These are essential to global MCP viability.

### Natural agent usage — HOST ACCEPTANCE only

No local Harness test can prove how a proprietary host composes final model context or whether a model naturally chooses a tool. Keep that evidence separate from deterministic MCP/server proof.

## 14. Dependency/reproducibility baseline

### Python 3.13 — ACCEPT

Current MCP SDK supports Python >=3.10, so 3.13 is within contract and is a reasonable baseline.

### Lock file — ACCEPT and mandatory before implementation baseline is called reproducible

Do not commit an invented or unverified lock. The current audit environment cannot resolve PyPI because outbound DNS is unavailable, so dependency/lock scaffolding is deliberately left to the next bounded task/environment where resolution can be verified.

### Dependency minimization — ACCEPT

Use dependencies only when a bounded feature needs them. The audit does not justify adding a generic DI framework, ORM, message bus, vector database, frontend toolchain, or plugin system.

## 15. MVP size and sequencing

### MVP content — product-appropriate but implementation-wide

The v1 list is large but internally coherent. Do not implement it as one vertical “platform construction” batch.

Recommended delivery sequence is by verifiable slices, not empty layers:

1. repository/package/tooling bootstrap;
2. storage + Project/Workspace registry + doctor capability checks;
3. daemon/local IPC skeleton with one read-only status path;
4. deterministic scan/index and reconciliation;
5. search contract;
6. Task lifecycle + baselines/checkpoints;
7. Knowledge + staleness;
8. official MCP v2 bridge + five contracts + wire/budget tests;
9. host registration + Workspace resolution acceptance one host at a time;
10. skills resolver/projection + collision tests;
11. dashboard/human review loop;
12. packaging/install/uninstall and complete acceptance matrix.

Each step must leave a coherent, tested state and stop before the next bounded task.

## Required amendments before coding

The following are architecture requirements, not optional suggestions:

1. **Target official MCP Python SDK v2 / MCP 2026-07-28 semantics.**
2. **Remove protocol-session identity from Task correctness.**
3. **Define `AgentSession` as Harness-observed bridge/client activity, not MCP identity.**
4. **Resolve active Task from Workspace domain state, with safe ambiguity handling.**
5. **Make Workspace resolution an explicit adapter/core boundary and acceptance criterion.**
6. **Do not depend on deprecated MCP roots for correctness.**
7. **Treat MCP `clientInfo` as untrusted/self-reported diagnostic metadata.**
8. **Use current host-native skill/config paths through adapters, never core constants.**
9. **Design skill projection against duplicate visibility across compatibility roots.**
10. **Add Workspace correctness to the real-host acceptance matrix.**
11. **Preserve pre-existing dirty-state semantics when computing Task changed files.**
12. **Verify FTS5/runtime platform capabilities through `doctor`.**

These amendments are captured in `ARCHITECTURE.md`, ADR-0001, ADR-0002, and `docs/host-compatibility.md`.

## Open questions that are intentionally not guessed

These do not block the architecture baseline, but they must be resolved before the corresponding implementation is called verified:

- Exact current-workspace propagation for globally registered Codex stdio MCP.
- Exact current-workspace propagation for globally registered Cursor stdio MCP, including multi-root workspaces.
- Exact current-workspace propagation for globally registered Antigravity stdio MCP.
- Cross-host duplicate handling when Cursor sees Claude-specific generated skill directories.
- Windows named-pipe implementation/library choice and service/autostart mechanism.
- Canonical cross-platform Workspace identity rules for symlinks, case folding, removable/moved checkouts, and Git worktree common dirs.
- Concrete symbol parser library/language coverage for the first implementation slice.
- Packaging/autostart mechanism per OS.
- Project license.

## Final audit conclusion

**Proceed with implementation, but not by coding the original session model literally.**

The product thesis, local-first architecture, modular monolith, small MCP surface, task continuity, deterministic index, semantic enrichment, bounded disclosure, and host-adapter isolation are all worth retaining. The audited implementation baseline must instead be sessionless at the MCP core, explicit about Workspace resolution, conservative about host metadata, and collision-aware in native skill projection.

That correction reduces hidden coupling rather than increasing architecture complexity and keeps the original primary complexity invariant intact.

## Official sources reviewed

### MCP

- https://modelcontextprotocol.io/specification/2026-07-28/
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/python-sdk
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md
- https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md

### Claude Code

- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/memory

### Codex

- https://developers.openai.com/codex/mcp/
- https://developers.openai.com/codex/skills/

### Cursor

- https://cursor.com/docs/mcp
- https://cursor.com/docs/skills

### Google Antigravity

- https://antigravity.google/docs/mcp
- https://antigravity.google/docs/skills/
- https://antigravity.google/docs/cli/gcli-migration/
