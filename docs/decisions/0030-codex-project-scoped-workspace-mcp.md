# ADR-0030: Codex Workspace identity is explicit project-scoped MCP state

- **Status:** Accepted
- **Date:** 2026-08-28
- **Deciders:** Repository architecture baseline
- **Builds on:** [ADR-0002](0002-host-integration-and-workspace-resolution.md), [ADR-0024](0024-linux-cursor-multi-host-lifecycle.md), [ADR-0026](0026-cursor-project-scoped-workspace-mcp.md)

## Context

Codex officially supports local stdio MCP servers and loads them from the user
`~/.codex/config.toml` or a trusted project's `.codex/config.toml`. The ChatGPT desktop app,
Codex CLI, and Codex IDE extension share this configuration. The MCP configuration schema
supports an explicit server `cwd` plus static `env` values.

The current official documentation does not establish a universal per-call active-Workspace
signal for a user-scoped stdio process. Harness cannot infer Workspace identity from MCP
`clientInfo`, protocol sessions, process cwd, or the most recently used Workspace. A global
Harness server without an accepted root contract could therefore read or mutate the wrong
Project, violating ADR-0002 and the explicit Task identity/revision invariants.

Project-scoped Codex configuration is a stronger boundary because Harness can write the exact
canonical Workspace root into both the server `cwd` and `HARNESS_WORKSPACE_ROOT`. Project-local
config is loaded only after the user trusts the project; trust remains a Codex/operator decision,
not a file Harness may silently edit in user configuration.

TOML mutation also has a preservation risk. Harness does not currently depend on a round-tripping
TOML editor, and rewriting an arbitrary user `.codex/config.toml` would risk losing comments,
formatting, or future Codex fields. A smaller ownership contract is safer for the first adapter
slice.

## Decision

1. Production Harness MCP for the Codex local profile is project-scoped. Each registered
   Workspace uses `.codex/config.toml` with `[mcp_servers.harness]`, the installed Python
   executable, `-m harness.mcp_process`, the canonical Workspace root as `cwd`, and exact
   `HARNESS_HOST_PROFILE=codex` plus `HARNESS_WORKSPACE_ROOT=<canonical-root>` environment values.
2. A Codex-profile MCP bridge requires that explicit `HARNESS_WORKSPACE_ROOT` and treats it as an
   exact `ROOT` hint. It must list no tools and refuse calls when the root is absent or invalid.
   Process cwd and self-reported client metadata are not Codex Workspace identity.
3. Harness does not create a user-scoped `~/.codex/config.toml` MCP server until separate evidence
   establishes a safe active-Workspace signal. Codex host activity will be Harness-owned intent
   state, as with project-only Cursor, rather than inferred from a global config entry.
4. Automatic TOML mutation is ownership-aware and deliberately narrow:
   - when `.codex/config.toml` is absent, Harness may create the complete file plus an adjacent
     `.harness-mcp-owner.json` marker;
   - a marker-owned file must contain only the exact Harness MCP table shape plus the exact Hidden
     `developer_instructions` when applicable; stale Python paths and owned visibility policy may
     then be replaced atomically;
   - an existing exact desired entry without the marker is accepted as manual adoption and is not
     removed by Harness;
   - an existing user-owned file without the exact entry, a foreign same-name server, tracked
     state requiring mutation, malformed TOML, symlinks, or unknown content added to an owned
     container fail closed without rewrite.
5. Harness-created Codex config and ownership markers are root-anchored in the Git common
   `info/exclude` file. Harness never changes `.gitignore`. Cleanup removes only exact owned files
   and the exact Harness exclude block, preserving the block while another linked worktree still
   has a valid Codex ownership marker.
6. Codex project trust is not written by Harness. Install/scan/doctor must give bounded actionable
   guidance when a project config exists but is not loaded because the operator has not trusted or
   restarted the Codex client.
7. This ADR defines the local CLI/IDE/ChatGPT-desktop configuration profile only. Codex cloud and
   hosted ChatGPT plugin delivery are separate profiles and cannot inherit its Workspace or Hidden
   guarantees.
8. Codex participates in Harness-owned install/scan/doctor/uninstall intent and skill lifecycle.
   Codex and Cursor can share one `.agents/skills` projection; Codex and Claude can use their two
   native roots. The simultaneous Claude + Codex + Cursor graph is rejected before mutation because
   Cursor observes both required roots and would receive duplicate skills.
9. Codex supports ADR-0028 hygiene-effective Hidden through the trusted project config layer.
   Marker-owned `.codex/config.toml` adds the exact Harness Hidden text as top-level
   `developer_instructions`; returning to Normal removes that key while preserving the MCP/root
   contract. This channel composes with, rather than replaces, user-owned `AGENTS.md`. A tracked or
   user-owned Codex config is never rewritten: Hidden preflight accepts it only when its Harness MCP
   entry and Hidden developer instructions are already exact. Codex still has no proven hard
   SCM-write denial, so operator diagnostics must report the policy as hygiene-effective rather
   than enforced.

## Consequences

- Codex calls cannot silently attach to another registered Workspace through global-process state.
- CLI, IDE, and ChatGPT desktop local clients can share one project-scoped contract after project
  trust and restart/reload.
- Existing arbitrary project TOML requires exact manual adoption instead of lossy automatic merge.
  A future round-tripping TOML mutation layer may amend this constraint with preservation tests.
- Every registered Workspace needs its own ignored project config; install and scan lifecycle work
  reconciles it before Codex is advertised for that Workspace.
- `--host all` cannot install the incompatible three-host skill graph. Operators may use either
  compatible pair; uninstall-all still removes every Harness-owned profile artifact.
- Codex can install and reconcile against registered Hidden Projects without changing user project
  instructions. Project trust/restart remains operator-owned, and hard enforced Hidden remains a
  later acceptance-gated profile capability.
- Real-host acceptance must prove project config discovery, trust/restart behavior, the five-tool
  catalog, correct worktree identity, and cross-client continuity. Core tests cannot prove Codex's
  internal tool-ranking behavior.

## Verification

Automated tests must prove:

- exact Normal/Hidden generated TOML fields, modes, ownership marker, and root-anchored Git exclusions;
- idempotent reconcile and marker-owned installed-Python update;
- exact tracked/manual adoption without mutation or automatic removal;
- refusal of user-owned TOML, foreign same-name entries, tracked mutation, malformed/symlink state,
  and unknown content added to an owned file;
- atomic changed-before/during-mutation handling and preservation of recovery evidence;
- linked-worktree roots receive distinct explicit Workspace values while the shared exclude block
  remains until the last owned project config is removed;
- Codex-profile MCP without explicit `HARNESS_WORKSPACE_ROOT` exposes no tools, while two explicit
  roots resolve to their distinct Workspace IDs;
- installed-wheel upgrade refreshes every owned Workspace config to the new Python executable and
  partial uninstall preserves other active hosts and shared skill projections.
- three-host skill admission fails before intent/config mutation; Codex Hidden installation and
  transitions preserve `AGENTS.md`, reconcile exact developer instructions, and fail before mutation
  on unsafe/manual config collisions.
- the opt-in real-CLI runner requires explicit model-usage acknowledgement, uses only a temporary
  fixture Workspace, trusted `CODEX_HOME`, and installed wheel/state; scopes an explicitly supplied
  `CODEX_API_KEY` to `codex exec`; proves the exact configured command, schemas, Workspace identity,
  and five calls through the official MCP SDK before optionally deriving model-selected five-tool
  proof from Codex JSONL MCP events; verifies cleanup; and detects any byte change to user Codex
  config.

Real-host acceptance must additionally prove:

- trusted project config is discovered by the current Codex CLI, IDE extension, and ChatGPT desktop
  local Codex host after their documented restart/reload boundary;
- the exact five Harness tools and server instructions are visible;
- simultaneous projects and linked worktrees never cross-resolve;
- Task and Knowledge continuity survives fresh Codex processes and switching to another supported
  Harness host;
- relevant skills under `.agents/skills` are visible once, irrelevant skills are absent, and no
  compatibility-root duplicate appears;
- untrusted project behavior is fail-closed and remediation does not mutate Codex trust state.

## Official evidence

- https://developers.openai.com/codex/extend/mcp
- https://learn.chatgpt.com/codex/config-file/config-reference
- https://developers.openai.com/codex/agent-configuration/agents-md
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/config-file/config-advanced#project-instructions-discovery
- https://developers.openai.com/codex/build-skills
