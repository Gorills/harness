# Host compatibility baseline

**Baseline checked:** 2026-08-28 against current official host documentation.

**Codex MCP/config/AGENTS/Skills evidence re-checked:** 2026-08-28.

**Claude Code MCP evidence re-checked:** 2026-08-24.

**Antigravity skill evidence re-checked:** 2026-08-25.

**Cursor MCP/CLI/Skills evidence re-checked:** 2026-08-27.

This file is evidence for adapter design, not a promise that undocumented host internals remain stable. Re-check official docs when adapter behavior changes.

## Matrix

| Host | Global/user MCP | Project MCP | Project root signal | Project skills | Global/user skills | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code | leftover user-scope `~/.claude.json` is operator-owned | leftover `.mcp.json` is not a Harness overlay | leftover `CLAUDE_PROJECT_DIR` overlay refuse only | leftover `.claude/skills/` on Cursor visible roots | `~/.claude/skills/` | Retired as a Harness host ([ADR-0039](../decisions/0039-retire-claude-code-host.md)). Leftover overlay refuse and Cursor compatibility-root cleanup remain; Harness no longer registers `claude mcp`. |
| Codex CLI / IDE / ChatGPT desktop local config | Not written by Harness | `.codex/config.toml` in trusted project | Authenticated daemon-owned Streamable HTTP plus exact `X-Harness-Workspace-Root`; lifecycle/wire integration is implemented, real-host acceptance remains | `.agents/skills/` from CWD through repo root | `~/.agents/skills/`; admin `/etc/codex/skills` | Ownership-aware project adapter implemented; required initialization validates the daemon and Workspace before use. MCP server instructions are used and the first 512 chars should be self-contained. |
| Cursor | leftover `~/.cursor/mcp.json` is removed if Harness-owned | `.cursor/mcp.json` | Project `${workspaceFolder}` mapped to `HARNESS_WORKSPACE_ROOT`. Leftover `user-harness` is not Workspace identity; Cursor-injected `WORKSPACE_FOLDER_PATHS` is ignored | `.agents/skills/`, `.cursor/skills/`, plus compatibility roots | `~/.agents/skills/`, `~/.cursor/skills/`, plus compatibility roots | Local IDE + CLI adapter implemented; production MCP is project-only and enabled with `agent mcp enable harness`. Cursor Cloud Agents are a separate out-of-scope profile. |
| Antigravity | `~/.gemini/config/mcp_config.json` | `.agents/mcp_config.json` | Needs real-host acceptance for global stdio current-root behavior | IDE + current CLI: `.agents/skills/<skill>/SKILL.md`; IDE also supports legacy `.agent/skills/` | IDE: `~/.gemini/antigravity/skills/`; CLI: `~/.gemini/antigravity-cli/skills/` | IDE and current CLI both use folder-based `SKILL.md`; CLI 1.1.9 runtime `/skills` also exposed Shared `~/.gemini/skills/`, but current 1.1.20 docs do not document that root. Treat it as versioned runtime evidence pending acceptance. CLI does not claim the IDE legacy `.agent/skills/` compatibility root. Keep separate profiles because their visibility contracts differ. |

## Official evidence notes

### Claude Code

- User-scoped MCP servers are stored in `~/.claude.json` and available across projects.
- For local stdio MCP servers Claude Code sets `CLAUDE_PROJECT_DIR` to the project root.
- Project/personal skills use `.claude/skills` and `~/.claude/skills`.
- Claude Code reads `CLAUDE.md`, not `AGENTS.md`, and explicitly documents importing `@AGENTS.md` to share instructions.

### Retired Claude Code adapter boundary

Claude Code is not a supported Harness host ([ADR-0039](../decisions/0039-retire-claude-code-host.md)). Harness no longer ships `ClaudeCodeAdapter`, does not register `claude mcp`, and does not project `.claude/skills` as an active target. Cursor still lists `.claude/skills` as a visible compatibility root so leftover Harness-owned files collide and can be cleaned. Hidden apply removes leftover `.claude/rules/harness-hidden.md`. A leftover `HARNESS_HOST_PROFILE=claude-code` process is unsupported; overlay refuse may still use `CLAUDE_PROJECT_DIR` so a leftover production process against this checkout lists no tools. `CLAUDE.md` remains a one-line `@AGENTS.md` pointer for editors.

Official Claude Code evidence below is leftover-host documentation, not a current Harness install contract.

### Implemented skill resolver/projection boundary

The current core skills slice loads Harness-owned canonical skills from `~/.harness/skills/`, with portable `SKILL.md` content separated from strict Harness applicability metadata in `harness.yaml`. Deterministic relevance combines indexed languages/manifests/dependencies and explicit resolver include/exclude inputs under a bounded visible-skill budget. Task `stack_hints` are optional Task metadata, not a Skill selector. No proprietary host is trusted to perform that project-pack selection.

Projection planning takes explicit host visibility surfaces and chooses a minimal set of native project roots where every active profile sees exactly one generated Harness copy; if the compatibility graph cannot satisfy that invariant, projection fails closed. Reconciliation removes only exact Harness-owned stale projections, refuses user-owned or Git-tracked collisions, rechecks filesystem identity before mutation, and maintains generated-path exclusions through `git rev-parse --git-path info/exclude` without changing `.gitignore`. Codex and Cursor share `.agents/skills`. Cursor also observes leftover `.claude/skills` and `.codex/skills` compatibility roots so retired Harness-owned files can be cleaned; Claude Code is not an active projection profile ([ADR-0039](../decisions/0039-retire-claude-code-host.md)). `--host all` is the Codex+Cursor pair. Antigravity can reuse `.agents/skills` where its active graph permits it. Codex and Cursor local MCP/root integration are implemented; Antigravity MCP/root integration and proprietary-host visibility remain later/acceptance work.

Installed profile intent is persisted for all supported local hosts. Foreground `scan` projects
synchronously; the daemon watcher repeats resolution after authoritative index changes
([ADR-0032](../decisions/0032-continuous-project-skill-reconciliation.md)). Task mutations do not
rotate the project pack ([ADR-0042](../decisions/0042-project-stack-skill-selection.md)). Host-side
discovery and progressive disclosure are native. Restart/reopen remains the documented fallback when
a host does not refresh changed files. Identifier lists such as `recommended_skills` are not
delivery. Harness MCP does not carry skill bodies.

Automated tests prove registry parsing, stack relevance, bounded selection, compatibility-root collision planning, idempotent/rollback-safe projection, late-race refusal, linked-worktree Git exclusion, and installed-wheel projection mechanics. They do **not** prove that Codex, Cursor, or Antigravity displays or de-duplicates these generated skills in a proprietary build; the matrix below remains the acceptance authority for that behavior.

### Codex

- MCP config defaults to `~/.codex/config.toml`; trusted projects may use `.codex/config.toml`.
- The desktop app, Codex CLI, and IDE extension share that MCP configuration.
- Codex reads MCP server instructions as server-wide guidance; current docs recommend the first 512 characters be self-contained.
- Repository skills are discovered from `.agents/skills` from current directory through repo root; user skills live at `~/.agents/skills`.
- Skills use progressive disclosure; current Codex docs cap the initial skills list to at most 2% of context or 8,000 characters when context size is unknown.

### Implemented Codex local project adapter boundary

ADR-0030 selects project-scoped Codex MCP rather than a user-level shared process whose active
Workspace is not established by current documentation. ADR-0037 replaces the failed stdio-to-Unix
socket route with a daemon-owned authenticated Streamable HTTP endpoint. The adapter writes the
canonical Workspace root as `X-Harness-Workspace-Root` and writes the private daemon capability as
an `Authorization` header. The generated server is required; initialization validates the bearer,
registered Workspace, and daemon path before Codex can start. Automatic mutation is limited to an
absent `.codex/config.toml` or a
complete container proven by Harness's adjacent ownership marker. Existing exact config may be
adopted manually; arbitrary user TOML, foreign same-name servers, tracked mutation, malformed
files, symlinks, and unknown additions to an owned container fail closed without rewrite.

Harness-created Codex config and marker files are kept untracked through exact root-anchored Git
`info/exclude` entries without changing `.gitignore`; cleanup preserves unrelated exclude content
and keeps the shared block while another linked worktree remains owned. Unit tests prove exact TOML,
ownership, idempotence, capability refresh, stdio-to-HTTP migration, manual adoption, collision
refusal, cleanup, explicit root headers, and CLI discovery. `install --host codex` records project-only intent; `scan` reconciles
the Workspace config and `.agents/skills`; doctor reports expected/configured endpoint, root, and
ownership state; uninstall removes only marker-owned config. An HTTP initialization without the
explicit root fails before publishing a usable tool session. Wire tests prove Cursor → Codex Task/Knowledge
continuity.

Codex and Cursor share one `.agents/skills` projection. Cursor leftover cleanup still lists
`.claude/skills` as a visible compatibility root; Claude Code is not an active host
([ADR-0039](../decisions/0039-retire-claude-code-host.md)). Hidden uses exact `developer_instructions` in the trusted marker-owned
project config and never replaces existing `AGENTS.md`; returning to Normal removes only that exact
owned key. This is hygiene-effective policy, not host SCM-write enforcement. Installed-wheel cross-interpreter upgrade continuity is automated;
proprietary CLI/IDE/desktop acceptance remains open.

`scripts/accept_codex.py` is the separate opt-in CLI model acceptance runner. Its no-argument mode
prints the external destination, exact temporary fixture/MCP payload class, account-usage effect,
API-key scope, and local isolation guarantees without invoking a model. With explicit
`--run-model` approval and an invocation-scoped `CODEX_API_KEY`, it builds the exact wheel, requires
real Codex JSONL evidence for successful calls to all five Harness tools, a native skill-read
`skill_marker` field proof that Codex selected the matching description from the scan-projected
pack present at session start (not a Task-selected skill), and a negative-control exec whose
prompt must not return the unmatched sibling nonce. That sibling is a Codex acceptance
fairness device (projected so description selection can fail closed); an unmatched prompt
must not select it. The runner then checks
doctor/skills/config/cleanup, verifies that `codex debug prompt-input` includes the exact Harness
bootstrap in model-visible input, and emits a sanitized report. When `--run-model` is used, the
report may include sanitized `search_behavior` metrics from `scripts/eval_search_behavior.py`
(strong vs zero/insufficient Harness hits, targeted vs broad native follow-up). That field is
metrics-only; the standalone CLI JSON includes redacted evidence. The classifier is acceptance
evidence only: it does not add daemon telemetry or MCP fields. The same script classifies
existing `codex exec --json` JSONL without a model when given `--workspace-root`. The key is
passed only to `codex exec`;
the runner uses temporary trusted `CODEX_HOME` state and never reads saved Codex authentication or
writes user trust/config. A local-only stdio preflight passed on 2026-08-28 with `codex-cli 0.147.0`.
ADR-0037 replaced that transport; current automated proof uses the official MCP SDK against the
generated Streamable HTTP endpoint. Real Codex model-selected HTTP MCP, IDE extension, and desktop
acceptance remain in the matrix below. The prompt-input check applies to a fresh CLI process.

When Harness changes a Codex project config, its CLI guidance requires fully quitting and reopening
the client and then creating a new Task. Existing Tasks retain their original instruction snapshot.
The acceptance check for that new Task is behavioral: `project_status` is the first project
action; diagnosis and `project_search` occur only after `task_start` or resume. Compact
`project_status.index` remains a snapshot (`indexed_file_count`,
`content_search_document_count` for code/docs content FTS coverage, and last-known
reconcile provenance) and is not a live
freshness or absence proof. Only tool discovery needed to locate and call Harness is
allowed before status. Harness does
not generate or merge root `AGENTS.md`:
that file is user-owned, and claiming it would be unsafe across existing instructions and linked
worktrees. MCP server instructions carry the same deferred-tool bootstrap in their first 512
characters as a second supported delivery path after server discovery.

### Cursor

- Global MCP config: `~/.cursor/mcp.json`; project config: `.cursor/mcp.json`. Global and project MCP servers are merged. Official notes say a same-name project server has priority; real-host Cursor IDE evidence from 2026-08-27 is that a leftover global entry is a distinct `user-harness` namespace while the project override stays disconnected until it is approved. Harness therefore does not keep an owned global server. The leftover user-level server is profile-scoped (`mcpScope:profile`, `mcp_version=shared_process`).
- Cursor IDE and Cursor CLI use the same MCP configuration. Current CLI commands include `agent mcp list`, `agent mcp list-tools <identifier>`, and `agent mcp enable <identifier>`.
- Cursor's current STDIO schema documents `type: "stdio"` and `command` as required, with optional `args` and `env`.
- Cursor supports interpolation including `${workspaceFolder}`. Official docs describe it as the folder that contains `.cursor/mcp.json`. Real-host Cursor IDE source from 2026-08-27 interpolates user-level MCP with `configurationResolverService.resolveAsync` against the window that spawned the shared profile-scoped process, not `~/.cursor`.
- Real-host Cursor IDE user-level stdio evidence from 2026-08-27: process cwd is the user home, not the Workspace. Cursor injects `WORKSPACE_FOLDER_PATHS` on that shared process; it can name the spawn window rather than the calling window. Using it as Workspace identity attached an Alia chat to mangazeya-backend and wrote a Task into the wrong Project. Harness therefore ignores it. Cursor-profile identity is only interpolated `HARNESS_WORKSPACE_ROOT` from an enabled project override.
- After changing `mcp.json`, current Cursor help explicitly says to save the file and restart Cursor. MCP Logs in the Output panel are the documented troubleshooting surface.
- Skills load from `.agents/skills` and `.cursor/skills`, with user equivalents. Official Cursor
  skill docs describe discovery by name/description and loading `SKILL.md` on activation. Reviewed
  Cursor CLI (`agent mcp list`, `agent mcp list-tools`, `agent mcp enable`) does not expose a
  documented, deterministic "currently selected Skill" query. Do not treat undocumented IDE
  internals, agent transcripts, or a new Harness telemetry channel as that proof.
- Cursor also loads compatibility skill directories including `.claude/skills` and `.codex/skills`. A projection that writes duplicates to several roots is therefore unsafe by default.

### Cursor native skill selection (manual real-host)

Harness does not own Cursor's per-prompt Skill choice. After `harness scan`, the stable project pack
must already exist under `.agents/skills` before a new Cursor session starts. Task lifecycle must
not be used as a Skill selector. Host-version uncertainty remains: Cursor may change how Skills
appear in the UI.

Until Cursor documents a machine-readable selected-Skill API, use this operator procedure on a
mixed FastAPI+Expo (or equivalent backend+mobile) Workspace:

1. Confirm the projected pack is present and Git-ignored (`ls .agents/skills` includes both
   backend/server and mobile surfaces; no Task mutation is required first).
2. Fully quit and reopen Cursor so the session can rediscover project Skills (current Cursor MCP
   help still requires restart after config changes; Skill rediscovery is not proven to hot-reload).
3. Confirm Cursor lists the project Skills from `.agents/skills` (Skills UI / skill picker if the
   current build shows one). Record the Cursor version. Absence of a picker is not a Harness defect;
   the pack on disk is still the Harness contract.
4. In a **new** chat, give a backend-only prompt (for example, Alembic/API validation). The agent
   should follow server/data guidance if it activates a Skill. It must not systematically apply
   mobile-application guidance to that prompt.
5. In another **new** chat, give a mobile-only prompt (for example, Expo navigation). Mobile
   guidance should activate. Backend-only Skills should not systematically dominate.
6. In a third chat, a mixed prompt should not produce systematic confusion (always-mobile on
   backend work, or the reverse) across repeated trials.

Score files-at-session-start and qualitative native choice. Do not score mid-session Task start as
Skill injection. Do not add Cursor-specific path logic to the core resolver from this procedure.

### Implemented Cursor local adapter boundary

The production Linux Cursor adapter edits only Cursor's documented local JSON MCP surfaces. It does not write a global Harness server. Leftover owned `~/.cursor/mcp.json` `mcpServers.harness` is removed; a foreign global `harness` is a fail-closed collision. Every registered Workspace receives a complete `.cursor/mcp.json` override with `HARNESS_WORKSPACE_ROOT=${workspaceFolder}`. Cursor activity is stored in Harness-owned host integration state. After writing a project config, Harness runs `agent mcp enable harness` from that Workspace and verifies `agent mcp list-tools harness` for the exact five tools. Missing `agent` leaves the project config and prints the exact enable command. When the bridge sees the Cursor profile marker, interpolated `HARNESS_WORKSPACE_ROOT` is the only `ROOT` hint. cwd, an uninterpolated `${workspaceFolder}` literal, and `WORKSPACE_FOLDER_PATHS` are never Workspace identity. A Cursor-profile process without that interpolated root lists no tools. If a production process has an interpolated root that is the Harness source checkout's isolated-development overlay, it lists no tools and refuses calls even when `WORKSPACE_FOLDER_PATHS` names a working repository; Cursor-profile overlay refuse does not consult cwd. The checkout overlay is named `harness-dev` and is never enabled as production `harness`.

Config mutation preserves unrelated top-level fields and MCP servers, refuses a foreign same-name `harness`, revalidates bytes before mutation, and uses atomic no-clobber replacement/recovery. Harness never deletes the global config container on uninstall because it cannot prove file ownership across runs. Project config is stricter: tracked config may be accepted only when it already contains the exact current Harness entry, while any required tracked mutation fails closed for manual adoption/removal. If Harness creates an untracked project config, a Workspace-local ownership marker records that fact and Git `info/exclude` keeps the generated config/marker untracked without touching `.gitignore`; linked worktrees are handled against their shared Git common exclude file.

`harness install --host cursor` records Cursor intent, removes leftover global `user-harness`, reconciles all already registered Workspace overrides, and enable/verifies each project MCP. Compatible profiles are installed explicitly; `--host all` installs the Codex+Cursor pair. Claude Code is not a supported host ([ADR-0039](../decisions/0039-retire-claude-code-host.md)). `scan` reconciles one combined active profile set, reusing a single compatible projection where possible. Partial uninstall recalculates skills for the remaining host and leaves the daemon alive. When any Cursor MCP config is actually changed, or when `agent` is missing, the CLI tells the user to fully quit/reopen Cursor and shows the current host-side inspection commands. Doctor reports leftover/foreign/absent global Cursor MCP, on-disk project overrides, Cursor approval/tool catalog, daemon runtime, and Project index separately, including expected/configured Python, the project path, the `${workspaceFolder}` contract, ownership/adoption failures, and an actionable remediation while remaining read-only. Isolated doctor does not inspect user-global host MCP or `~/.harness/skills`.

Automated acceptance now includes a real Harness MCP subprocess switching Cursor → Codex → Cursor, two linked Workspaces with distinct IDs, Project-wide Knowledge/Task retrieval, Workspace-local current Task/index isolation, and installed-wheel multi-host upgrade/uninstall lifecycle. Proprietary Cursor IDE/CLI discovery remains in the real-host matrix. Cursor Cloud Agents are not covered by the local `.cursor/mcp.json` adapter; their personal/team/cloud MCP configuration and API are a separate future profile.

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
| Claude Code local CLI/IDE | leftover Harness-owned `.claude/rules/harness-hidden.md` is removed when applying supported Hidden; do not write `CLAUDE.local.md` | Not a supported Hidden profile | leftover only | Leftover cleanup only; Claude Code is not a supported Hidden profile ([ADR-0039](../decisions/0039-retire-claude-code-host.md)) |
| Codex local CLI/IDE | Trusted project `.codex/config.toml` `developer_instructions`; Harness leaves `AGENTS.md` unchanged | Safe project-local hard SCM denial is not established by docs reviewed here | No host-injected attribution contract established here | Hygiene-effective Hidden (ADR-0028/0030): exact owned developer instructions + git-local ignore; not host-enforced SCM denial |
| Cursor IDE/CLI | `.cursor/rules/harness-hidden.mdc` (`alwaysApply: true`) plus Git `info/exclude` | Safe project-local hard SCM denial is not established by docs reviewed here | Cursor changelog documents per-user/admin attribution control | Hygiene-effective Hidden (ADR-0028): instructions + git-local ignore; not host-enforced SCM denial |
| Cursor Cloud/background | Cloud profile is separate from local project execution | Local project settings cannot be assumed to control server-side branch/commit/PR behavior | Official docs reviewed do not establish a complete cloud suppression contract; Cursor staff support has separately reported cloud `Co-authored-by: Cursor` attribution as independent of the local setting | Unsupported until current vendor behavior and acceptance prove suppression |
| Antigravity IDE | `.agents/rules/`; project-scoped settings/permissions are documented | Official Deny > Ask > Allow permission engine and project settings are candidate controls | No automatic durable attribution behavior established by docs reviewed here | Candidate; real-host acceptance required |
| Antigravity CLI | `.agents/rules/` plus CLI permission settings | CLI permissions are documented, but scope/escape behavior must be accepted separately from IDE | No automatic durable attribution behavior established here | Candidate; separate acceptance required |

Git-local artifact hiding is common across profiles: Hidden projections resolve `git rev-parse --git-path info/exclude` (logically `$GIT_COMMON_DIR/info/exclude`), never mutate `.gitignore`, and must fail on tracked/user-owned target collisions. Because the common exclude file is shared by linked worktrees, v1 uses one effective visibility mode for Workspaces sharing the same Git common directory.

## Required real-host acceptance matrix

For every supported host/profile and supported OS family where behavior differs, verify the
applicable Normal-mode items below. Verify Hidden items only for profiles that claim Hidden
support; Codex claims only ADR-0028 hygiene-effective support until real-host enforcement is proven.

- [ ] Harness global/user or project-scoped MCP registration is discovered according to the adapter
  contract, including bounded host-environment forwarding required by local stdio servers.
- [ ] `harness mcp` launches successfully.
- [ ] The five tool names and schemas are visible.
- [ ] Current Workspace is resolved to the correct worktree/repository.
- [ ] Two simultaneously registered Workspaces cannot be confused.
- [ ] A Task started in one host is visible/resumable in a fresh process of the same host.
- [ ] A Task can be resumed after switching to another supported host.
- [ ] Relevant generated skill files are present for host-native discovery
  (session start after projection, restart/reopen). Optional host-owned live detection
  may occur; do not score it as Harness current-session delivery. Task lifecycle does not
  rotate the project pack
  ([ADR-0042](../decisions/0042-project-stack-skill-selection.md)).
- [ ] The host chooses among that stable pack by Skill description. Cursor has no documented
  selected-Skill CLI; use the manual procedure in the Cursor section. Codex `--run-model`
  may prove description selection via the isolated nonce; it is optional and not CI-mandatory.
- [ ] Irrelevant generated skills are absent from the projected pack.
- [ ] No duplicate Harness skill appears because of compatibility directory scanning.
- [ ] Removing Harness deletes only Harness-owned integration artifacts.
- [ ] Codex trusted-project config is discovered independently by the current CLI, IDE extension, and ChatGPT desktop local client; untrusted projects remain fail-closed without Harness changing trust.
- [ ] After the required restart/reload, each Codex client includes the exact Harness bootstrap in
  the actual model-visible developer input; an already-running app with a stale config snapshot is
  reported as needing restart rather than accepted.
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
- Codex MCP: https://developers.openai.com/codex/extend/mcp
- Codex config reference: https://learn.chatgpt.com/codex/config-file/config-reference
- Codex AGENTS.md: https://developers.openai.com/codex/agent-configuration/agents-md
- Codex skills: https://developers.openai.com/codex/build-skills
- Codex AGENTS discovery source: https://github.com/openai/codex/blob/main/codex-rs/core/src/agents_md.rs
- Codex managed-permission sandbox source: https://github.com/openai/codex/blob/main/codex-rs/windows-sandbox-rs/src/resolved_permissions.rs
- Cursor MCP: https://cursor.com/docs/mcp
- Cursor MCP help/restart and project-over-global precedence: https://cursor.com/help/customization/mcp
- Cursor CLI MCP inspection: https://cursor.com/docs/cli/mcp
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
