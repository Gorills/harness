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
- [ ] When `harnessd` is unavailable, native editing/shell/Git workflow remains usable.

## Source URLs

- Claude MCP: https://code.claude.com/docs/en/mcp
- Claude skills: https://code.claude.com/docs/en/skills
- Claude project memory: https://code.claude.com/docs/en/memory
- Codex MCP: https://developers.openai.com/codex/mcp/
- Codex skills: https://developers.openai.com/codex/skills/
- Cursor MCP: https://cursor.com/docs/mcp
- Cursor skills: https://cursor.com/docs/skills
- Antigravity MCP: https://antigravity.google/docs/mcp
- Antigravity skills: https://antigravity.google/docs/skills/
- Antigravity CLI migration: https://antigravity.google/docs/cli/gcli-migration/
