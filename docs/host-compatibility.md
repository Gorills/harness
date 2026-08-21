# Host compatibility baseline

**Checked:** 2026-08-21 against current official host documentation.

This file is evidence for adapter design, not a promise that undocumented host internals remain stable. Re-check official docs when adapter behavior changes.

## Matrix

| Host | Global/user MCP | Project MCP | Project root signal | Project skills | Global/user skills | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code | `~/.claude.json` user scope | `.mcp.json` | `CLAUDE_PROJECT_DIR` documented for stdio server | `.claude/skills/` | `~/.claude/skills/` | Reads `CLAUDE.md`, not `AGENTS.md`; use `CLAUDE.md` importing `@AGENTS.md`. |
| Codex CLI / IDE / ChatGPT desktop local config | `~/.codex/config.toml` | `.codex/config.toml` in trusted project | No universal active-root guarantee established by docs reviewed here; requires acceptance | `.agents/skills/` from CWD through repo root | `~/.agents/skills/`; admin `/etc/codex/skills` | MCP server instructions are used; first 512 chars should be self-contained. |
| Cursor | `~/.cursor/mcp.json` | `.cursor/mcp.json` | `${workspaceFolder}` documented for project config; global active-root semantics require acceptance | `.agents/skills/`, `.cursor/skills/`, plus compatibility roots | `~/.agents/skills/`, `~/.cursor/skills/`, plus compatibility roots | Cursor also scans Claude/Codex skill directories: duplication risk. |
| Antigravity | `~/.gemini/config/mcp_config.json` | `.agents/mcp_config.json` | Needs real-host acceptance for global stdio current-root behavior | `.agents/skills/` | IDE docs: `~/.gemini/config/skills/`; Antigravity CLI migration docs: `~/.gemini/antigravity-cli/skills/` | Model skills follow progressive disclosure. Keep IDE/CLI skill profile differences explicit. |

## Official evidence notes

### Claude Code

- User-scoped MCP servers are stored in `~/.claude.json` and available across projects.
- For local stdio MCP servers Claude Code sets `CLAUDE_PROJECT_DIR` to the project root.
- Project/personal skills use `.claude/skills` and `~/.claude/skills`.
- Claude Code reads `CLAUDE.md`, not `AGENTS.md`, and explicitly documents importing `@AGENTS.md` to share instructions.

### Codex

- MCP config defaults to `~/.codex/config.toml`; trusted projects may use `.codex/config.toml`.
- The desktop app, Codex CLI, and IDE extension share that MCP configuration.
- Codex reads MCP server instructions as server-wide guidance; current docs recommend the first 512 characters be self-contained.
- Repository skills are discovered from `.agents/skills` from current directory through repo root; user skills live at `~/.agents/skills`.
- Skills use progressive disclosure; current Codex docs cap the initial skills list to at most 2% of context or 8,000 characters when context size is unknown.

### Cursor

- Global MCP config: `~/.cursor/mcp.json`; project config: `.cursor/mcp.json`.
- Cursor supports interpolation including `${workspaceFolder}` for project configuration.
- Skills load from `.agents/skills` and `.cursor/skills`, with user equivalents.
- Cursor also loads compatibility skill directories including `.claude/skills` and `.codex/skills`. A projection that writes duplicates to several roots is therefore unsafe by default.

### Antigravity

- Official MCP docs list `~/.gemini/config/mcp_config.json` globally and `.agents/mcp_config.json` per workspace.
- Workspace skills use `.agents/skills`.
- Antigravity IDE docs list global skills under `~/.gemini/config/skills`.
- Antigravity CLI migration docs list global CLI skills under `~/.gemini/antigravity-cli/skills` while keeping workspace skills at `.agents/skills`.
- Skills are discovered by name/description first and full `SKILL.md` is loaded on activation.

## Hidden-mode capability baseline

Hidden mode is stricter than ordinary project instructions. Prompt/rule loading is necessary but not sufficient: the supported profile must deny agent-originated durable SCM mutations, protect its enforcement from agent tampering, and suppress host-injected durable attribution where applicable. Admission is based on Harness-owned adapter/profile metadata rather than self-reported `clientInfo`; unknown capability fails closed.

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
