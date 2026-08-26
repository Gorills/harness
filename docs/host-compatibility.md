# Host compatibility baseline

**Baseline checked:** 2026-08-26 against current official host documentation.

**Claude Code MCP evidence re-checked:** 2026-08-24.

**Antigravity skill evidence re-checked:** 2026-08-25.

**Cursor MCP/CLI/Skills evidence re-checked:** 2026-08-26.

This file is evidence for adapter design, not a promise that undocumented host internals remain stable. Re-check official docs when adapter behavior changes.

## Matrix

| Host | Global/user MCP | Project MCP | Project root signal | Project skills | Global/user skills | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code | `~/.claude.json` user scope | `.mcp.json` | `CLAUDE_PROJECT_DIR` documented for stdio server | `.claude/skills/` | `~/.claude/skills/` | MCP registration/root adapter implemented; real-host acceptance still required. Reads `CLAUDE.md`, not `AGENTS.md`; use `CLAUDE.md` importing `@AGENTS.md`. |
| Codex CLI / IDE / ChatGPT desktop local config | `~/.codex/config.toml` | `.codex/config.toml` in trusted project | No universal active-root guarantee established by docs reviewed here; requires acceptance | `.agents/skills/` from CWD through repo root | `~/.agents/skills/`; admin `/etc/codex/skills` | MCP server instructions are used; first 512 chars should be self-contained. |
| Cursor | `~/.cursor/mcp.json` | `.cursor/mcp.json` | `${workspaceFolder}` in project override, mapped to exact `HARNESS_WORKSPACE_ROOT` | `.agents/skills/`, `.cursor/skills/`, plus compatibility roots | `~/.agents/skills/`, `~/.cursor/skills/`, plus compatibility roots | Local IDE + CLI adapter implemented; Cursor Cloud Agents are a separate out-of-scope profile. |
| Antigravity | `~/.gemini/config/mcp_config.json` | `.agents/mcp_config.json` | Needs real-host acceptance for global stdio current-root behavior | IDE + current CLI: `.agents/skills/<skill>/SKILL.md`; IDE also supports legacy `.agent/skills/` | IDE: `~/.gemini/antigravity/skills/`; CLI: `~/.gemini/antigravity-cli/skills/` | IDE and current CLI both use folder-based `SKILL.md`; CLI 1.1.9 runtime `/skills` also exposed Shared `~/.gemini/skills/`, but current 1.1.20 docs do not document that root. Treat it as versioned runtime evidence pending acceptance. CLI does not claim the IDE legacy `.agent/skills/` compatibility root. Keep separate profiles because their visibility contracts differ. |

## Official evidence notes

### Claude Code

- User-scoped MCP servers are stored in `~/.claude.json` and available across projects.
- For local stdio MCP servers Claude Code sets `CLAUDE_PROJECT_DIR` to the project root.
- Project/personal skills use `.claude/skills` and `~/.claude/skills`.
- Claude Code reads `CLAUDE.md`, not `AGENTS.md`, and explicitly documents importing `@AGENTS.md` to share instructions.

### Implemented Claude Code adapter boundary

The Claude adapter uses the official `claude mcp` CLI rather than editing Claude configuration files directly. The supported Linux/POSIX `harness install`/`uninstall` lifecycle now wires this adapter into canonical daemon preparation/shutdown and generated project-skill reconciliation/cleanup. Harness registers one user-scope stdio server named `harness` with a Harness-owned `HARNESS_HOST_PROFILE=claude-code` environment marker and launches the installed Python interpreter with `-m harness.mcp_process`. Registration is idempotent for the exact owned entry, replaces only a stale entry carrying the same Harness ownership marker, and fails on a same-name foreign entry instead of overwriting it. Removal likewise requires the Harness marker before invoking the host CLI.

When that marker is present in the bridge process, Workspace hint construction is adapter-owned: `CLAUDE_PROJECT_DIR` is required and becomes an exact `ROOT` hint. A generic `HARNESS_WORKSPACE_ROOT` value or process cwd cannot override that stronger documented host signal. Without a Harness host-profile marker, the pre-adapter generic behavior remains available for manual/development launches.

Automated tests prove command construction, registration-state classification, ownership/collision handling, idempotence, error fail-closed behavior, normalized root-hint selection, a real Harness MCP stdio subprocess using the Claude profile, daemon-owned skill reconcile/cleanup, clean shutdown, full read-only operational doctor state, a full fake-Claude install→scan→uninstall/purge lifecycle, and installed-wheel reinstall through a second Python 3.13 environment with stale-daemon replacement. They do **not** prove that a proprietary Claude Code build discovers the registration, injects `CLAUDE_PROJECT_DIR`, exposes all five tools, or preserves continuity across real host restarts; those items remain unchecked in the real-host acceptance matrix below.

### Implemented skill resolver/projection boundary

The current core skills slice loads Harness-owned canonical skills from `~/.harness/skills/`, with portable `SKILL.md` content separated from strict Harness applicability metadata in `harness.yaml`. Deterministic relevance combines indexed languages/manifests/dependencies, durable Task `stack_hints`, and explicit resolver include/exclude inputs under a bounded visible-skill budget. No proprietary host is trusted to perform that selection.

Projection planning takes explicit host visibility surfaces and chooses a minimal set of native project roots where every active profile sees exactly one generated Harness copy; if the compatibility graph cannot satisfy that invariant, projection fails closed. Reconciliation removes only exact Harness-owned stale projections, refuses user-owned or Git-tracked collisions (including same-name content in another active compatibility root), rechecks filesystem identity before mutation, and maintains generated-path exclusions through `git rev-parse --git-path info/exclude` without changing `.gitignore`. Linked-worktree behavior is covered by automated Git fixtures. Claude Code contributes its documented `.claude/skills` project surface. Codex contributes its documented repository `.agents/skills` surface, and that surface requires the portable `SKILL.md` frontmatter fields (`name` and `description`) documented by Codex before projection is planned; plain Markdown skills remain usable for host surfaces that do not impose that contract. Cursor contributes `.agents/skills` as its preferred shared project target while declaring `.cursor/skills`, `.claude/skills`, and `.codex/skills` as additional visible compatibility roots. Cursor's recursive skill discovery is also modeled across its native and compatibility roots: projection refuses a same-id nested user skill under `.agents/skills`, `.cursor/skills`, `.claude/skills`, or `.codex/skills` and rechecks recursive visibility immediately before mutation. Cursor projection requires `name` and `description`, requires the frontmatter `name` to match the projected directory, and enforces Cursor's documented lowercase-letter/number/hyphen character set. This lets the generic planner reuse one Claude or Codex projection when Cursor can already see it, and fail closed when simultaneous active surfaces would make Cursor see duplicate Harness skills. Antigravity CLI contributes its current `.agents/skills/<skill>/SKILL.md` workspace surface without claiming the IDE-only legacy `.agent/skills` root; it can therefore reuse the same generated `.agents/skills` projection as the IDE, Codex, or Cursor when the active visibility graph permits it. Full Codex MCP/root integration, Antigravity CLI MCP/root integration, and proprietary-host visibility remain later/acceptance work. Cursor local MCP/root integration is implemented; proprietary Cursor acceptance remains separate.

Automated tests prove registry parsing, legacy and greenfield relevance, bounded selection, compatibility-root collision planning, idempotent/rollback-safe projection, late-race refusal, linked-worktree Git exclusion, and installed-wheel projection mechanics. They do **not** prove that Claude Code, Codex, Cursor, or Antigravity displays or de-duplicates these generated skills in a proprietary build; the matrix below remains the acceptance authority for that behavior.

### Codex

- MCP config defaults to `~/.codex/config.toml`; trusted projects may use `.codex/config.toml`.
- The desktop app, Codex CLI, and IDE extension share that MCP configuration.
- Codex reads MCP server instructions as server-wide guidance; current docs recommend the first 512 characters be self-contained.
- Repository skills are discovered from `.agents/skills` from current directory through repo root; user skills live at `~/.agents/skills`.
- Skills use progressive disclosure; current Codex docs cap the initial skills list to at most 2% of context or 8,000 characters when context size is unknown.

### Cursor

- Global MCP config: `~/.cursor/mcp.json`; project config: `.cursor/mcp.json`. Global and project MCP servers are merged, and a same-name project server has priority over the global entry.
- Cursor IDE and Cursor CLI use the same MCP configuration. Current CLI inspection commands include `agent mcp list` and `agent mcp list-tools <identifier>`.
- Cursor supports interpolation including `${workspaceFolder}` for project configuration; it expands to the project root containing `.cursor/mcp.json`.
- Skills load from `.agents/skills` and `.cursor/skills`, with user equivalents.
- Cursor also loads compatibility skill directories including `.claude/skills` and `.codex/skills`. A projection that writes duplicates to several roots is therefore unsafe by default.

### Implemented Cursor local adapter boundary

The production Linux Cursor adapter edits only Cursor's documented local JSON MCP surfaces. `~/.cursor/mcp.json` contains the global Harness stdio entry using the exact installed Python, `-m harness.mcp_process`, and `HARNESS_HOST_PROFILE=cursor`. Every registered Workspace receives a complete same-name `.cursor/mcp.json` override because the project entry shadows the global entry. That override repeats the launch definition and adds `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. When the bridge sees the Cursor profile marker, that environment value is mandatory and becomes an exact `ROOT` hint; cwd is never a fallback.

Config mutation preserves unrelated top-level fields and MCP servers, refuses a foreign same-name `harness`, revalidates bytes before mutation, and uses atomic no-clobber replacement/recovery. Harness never deletes the global config container on uninstall because it cannot prove file ownership across runs. Project config is stricter: tracked config may be accepted only when it already contains the exact current Harness entry, while any required tracked mutation fails closed for manual adoption/removal. If Harness creates an untracked project config, a Workspace-local ownership marker records that fact and Git `info/exclude` keeps the generated config/marker untracked without touching `.gitignore`; linked worktrees are handled against their shared Git common exclude file.

`harness install --host cursor` reconciles the global entry and all already registered Workspace overrides. `harness install --host all` and matching uninstall selection coordinate Claude + Cursor over one daemon. `scan` reconciles one combined active profile set, so Cursor compatibility roots reuse an existing Claude projection where possible instead of creating duplicates. Partial uninstall recalculates skills for the remaining host and leaves the daemon alive. Doctor reports Cursor global state and every bounded live Workspace override separately from Claude.

Automated acceptance now includes a real Harness MCP subprocess switching Claude → Cursor → Claude, two linked Workspaces with distinct IDs, Project-wide Knowledge/Task retrieval, Workspace-local current Task/index isolation, and installed-wheel multi-host upgrade/uninstall lifecycle. Proprietary Cursor IDE/CLI discovery remains in the real-host matrix. Cursor Cloud Agents are not covered by the local `.cursor/mcp.json` adapter; their personal/team/cloud MCP configuration and API are a separate future profile.

### Antigravity

- Official MCP docs list `~/.gemini/config/mcp_config.json` globally and `.agents/mcp_config.json` per workspace.
- Antigravity IDE workspace skills use `.agents/skills/<skill>/SKILL.md` and retain legacy `.agent/skills` compatibility.
- Antigravity IDE requires `description`; `name` is optional and defaults to the skill directory name.
- Current IDE docs list global skills under `~/.gemini/antigravity/skills/`.
- The CLI documentation page still describes flat `.agents/skills/*.md`, but current CLI runtime/release evidence uses folder skills at `.agents/skills/<skill>/SKILL.md`; Harness follows the current product behavior and treats the flat page as stale evidence.
- Current CLI docs list global skills under `~/.gemini/antigravity-cli/skills/`. Runtime `/skills` output from CLI 1.1.9 additionally exposed Shared `~/.gemini/skills/<skill>/SKILL.md`; issue #730 confirms the listing bug was fixed in 1.1.11, but current 1.1.20 docs do not document that Shared root. Treat it as versioned runtime evidence pending current-host acceptance, not a current documented contract.
- IDE skills are discovered by name/description first and full `SKILL.md` is loaded on activation.

## Hidden-mode capability baseline

Hidden mode is stricter than ordinary project instructions. Prompt/rule loading is necessary but not sufficient: the supported profile must deny agent-originated durable SCM mutations, protect its enforcement from agent tampering, suppress host-injected durable attribution where applicable, and prove safe mode-transition behavior for already-admitted sessions/profiles. Admission is based on Harness-owned adapter/profile metadata rather than self-reported `clientInfo`; unknown capability fails closed.

| Host/profile | Project/local rule or settings surface | SCM-write enforcement evidence | Attribution evidence | Hidden status before acceptance |
| --- | --- | --- | --- | --- |
| Claude Code local CLI/IDE | `CLAUDE.local.md`, `.claude/rules/`, `.claude/settings.local.json` | Official permissions + OS-level sandbox support deny rules / filesystem write restrictions; bypass/unsandboxed escape must be disabled for the tested profile | Official `attribution.commit` / `attribution.pr` can be empty; built-in Git instructions can be disabled | Candidate; real-host acceptance required |
| Codex local CLI/IDE | Project `AGENTS.md` discovery; `AGENTS.override.md` exists but replaces same-directory base instructions | Current source has managed permission profiles capable of workspace writes with a denied `.git` subpath (explicitly tested in the Windows sandbox); end-to-end injection, cross-platform behavior, remote-SCM denial, and anti-bypass still require acceptance | No host-injected attribution contract established here | Candidate primitive; acceptance-gated, never prompt-only |
| Cursor IDE/CLI | `.cursor/rules/`; user/admin attribution setting is documented | Safe project-local hard SCM denial is not established by docs reviewed here | Cursor changelog documents per-user/admin attribution control | Acceptance-gated; do not assume enforcement |
| Cursor Cloud/background | Cloud profile is separate from local project execution | Local project settings cannot be assumed to control server-side branch/commit/PR behavior | Official docs reviewed do not establish a complete cloud suppression contract; Cursor staff support has separately reported cloud `Co-authored-by: Cursor` attribution as independent of the local setting | Unsupported until current vendor behavior and acceptance prove suppression |
| Antigravity IDE | `.agents/rules/`; project-scoped settings/permissions are documented | Official Deny > Ask > Allow permission engine and project settings are candidate controls | No automatic durable attribution behavior established by docs reviewed here | Candidate; real-host acceptance required |
| Antigravity CLI | `.agents/rules/` plus CLI permission settings | CLI permissions are documented, but scope/escape behavior must be accepted separately from IDE | No automatic durable attribution behavior established here | Candidate; separate acceptance required |

Git-local artifact hiding is common across profiles: Hidden projections resolve `git rev-parse --git-path info/exclude` (logically `$GIT_COMMON_DIR/info/exclude`), never mutate `.gitignore`, and must fail on tracked/user-owned target collisions. Because the common exclude file is shared by linked worktrees, v1 uses one effective visibility mode for Workspaces sharing the same Git common directory.

## Required real-host acceptance matrix

For every supported host/profile and supported OS family where behavior differs, verify:

- [ ] Harness global/user MCP registration is discovered.
- [ ] `harness mcp` launches successfully.
- [ ] The five tool names and schemas are visible.
- [ ] Current Workspace is resolved to the correct worktree/repository.
- [ ] Two simultaneously registered Workspaces cannot be confused.
- [ ] A Task started in one host is visible/resumable in a fresh process of the same host.
- [ ] A Task can be resumed after switching to another supported host.
- [ ] Relevant generated skill is visible.
- [ ] Irrelevant generated skills are absent.
- [ ] No duplicate Harness skill appears because of compatibility directory scanning.
- [ ] Removing Harness deletes only Harness-owned integration artifacts.
- [ ] Hidden projection leaves `.gitignore` byte-for-byte unchanged and all Harness-owned project artifacts untracked/ignored.
- [ ] Hidden agent attempts to stage/commit/amend/create refs or tags/push/create or edit PRs/issues/reviews/comments are denied by the host profile, not merely discouraged by prompt text.
- [ ] Hidden enforcement configuration is tamper-resistant for the tested profile: the agent cannot edit/disable it or escalate into a bypass/full-access mode.
- [ ] Unsupported/spoofed host profiles are rejected for Hidden admission and cannot gain support by changing self-reported client metadata.
- [ ] With a Normal-capability agent/profile already admitted, switching to Hidden either revokes/revalidates that live capability before the mode becomes effective or fails closed with an actionable restart/reopen requirement; an older Normal-capability agent cannot commit/push after `project_status` reports Hidden.
- [ ] Hidden → Normal restoration does not remove Harness-owned enforcement underneath an admitted Hidden session before the transition boundary completes.
- [ ] Hidden mode emits no host/model/Harness attribution into durable Git/SCM artifacts; if the host injects unavoidable attribution, the profile fails Hidden acceptance.
- [ ] Switching Hidden → Normal restores only Harness-owned settings/policy and preserves unknown user configuration.
- [ ] When `harnessd` is unavailable, Normal remains native; Hidden still permits ordinary edits/shell/read-only Git while agent publication stays denied and human Git outside the agent path remains usable.

## Source URLs

- Claude MCP: https://code.claude.com/docs/en/mcp
- Claude skills: https://code.claude.com/docs/en/skills
- Claude project memory: https://code.claude.com/docs/en/memory
- Claude settings/attribution: https://code.claude.com/docs/en/settings
- Claude permissions: https://code.claude.com/docs/en/permissions
- Claude sandboxing: https://code.claude.com/docs/en/sandboxing
- Codex MCP: https://developers.openai.com/codex/mcp/
- Codex skills: https://developers.openai.com/codex/skills/
- Codex AGENTS discovery source: https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs
- Codex managed-permission sandbox source: https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/resolved_permissions.rs
- Cursor MCP: https://cursor.com/docs/mcp
- Cursor skills: https://cursor.com/docs/skills
- Cursor rules: https://cursor.com/docs/rules
- Cursor attribution changelog: https://cursor.com/changelog/3-0
- Cursor Cloud attribution staff support (secondary/current behavior evidence): https://forum.cursor.com/t/autonomous-commit-attribution/157699
- Antigravity MCP: https://antigravity.google/docs/mcp
- Antigravity skills: https://antigravity.google/docs/skills/
- Antigravity rules: https://antigravity.google/docs/ide/rules/
- Antigravity permissions: https://antigravity.google/docs/permissions/
- Antigravity CLI migration: https://antigravity.google/docs/cli/gcli-migration/
- Antigravity CLI plugins/skills docs (currently stale flat-layout evidence): https://antigravity.google/docs/cli/plugins/
- Antigravity CLI versioned runtime skill-path evidence (1.1.9; listing fixed in 1.1.11): https://github.com/google-antigravity/antigravity-cli/issues/730
- Antigravity CLI changelog (`SKILL.md` frontmatter): https://github.com/google-antigravity/antigravity-cli/blob/main/CHANGELOG.md
