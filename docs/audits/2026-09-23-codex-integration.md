# Codex integration audit — 2026-09-23

## Scope and evidence

This audit covers the current checkout's local Codex project adapter, model-facing MCP
contract, and synthetic Codex CLI acceptance. It also compares the checkout with the MCP
running in this Codex conversation. Production configuration and the user-global Harness package were changed only
after separate operator approvals, as recorded below. The external Codex documentation could not be opened in this
session because automatic browser approval denied origin access; host behavior below is
supported by temporary-profile Codex CLI reproduction and repository tests, not by a new
vendor documentation claim.

## Confirmed problems

| Severity | Problem | Evidence | Correction |
| --- | --- | --- | --- |
| P1 | A project MCP entry with `required=true` was reported `CURRENT` even when a user-level Codex config disabled the same server. The tracked development overlay had the same issue when `enabled` was absent or false. | A temporary trusted Codex profile with user-level `enabled=false` returned `enabled:false` from `codex mcp get`, while Harness diagnosed the project config as current. | Generated project config now writes `enabled=true`; owned older HTTP configs migrate. The development overlay requires explicit `enabled=true` and `required=true`. Manual/tracked configs that lack these fields require manual adoption. |
| P1 | Removing `project_search` left older Knowledge and Task records undiscoverable in a fresh Codex conversation unless the agent already knew an exact ID. | `project_context` accepts explicit refs only; `project_status` identifies only the current or relevant waiting Task; MCP has no Knowledge listing tool. | `project_recall` searches only durable Knowledge and Task records, returns brief applicable refs, and leaves code/document discovery to native tools. |
| P2 | Codex acceptance compared the inspected Authorization bearer with the bearer reported by that same inspection. A stale but well-formed token could pass the inspection stage. | The old `_verify_codex_inspection` constructed its expected header from `payload.transport.http_headers`. | Acceptance compares the CLI-reported headers with the exact generated project config and rejects a mismatch without printing either token. |
| P1 | Malformed Unicode accepted by several IPC text/path validators could raise `UnicodeEncodeError` outside the daemon protocol-error path. | A JSON escaped lone surrogate passed a direct `project_recall` or `project_context` validator; a surrogate absolute path passed scan validation but failed during `Path.resolve()`. The worker treated the exception as a fatal daemon failure. | Reject invalid UTF-8 at the IPC boundary and verify a later valid request still succeeds. |
| P2 | The local Codex CLI preflight could not reach its wire checks because the runner used `harness scan` on an unregistered synthetic Workspace. | `--preflight-only` failed with `registry_error: workspace is not registered; run harness init` after temporary install; cleanup completed. | Register temporary Workspaces with `harness init` before any subsequent `scan`, and cover the sequence in runner tests. |

## Verification

- `./scripts/dev python scripts/quality.py` passed: lock check, Ruff format/lint, mypy,
  1112 pytest cases, hot-path counter benchmark, and installed-wheel smoke test.
- `./scripts/dev python scripts/accept_codex.py --preflight-only` passed in temporary
  profiles: all five exact HTTP MCP calls (including `project_recall`), zero doctor
  failures, distinct Workspace identities, unchanged user Codex config, and owned
  cleanup. This did not run a model.
- A synthetic trusted project with a user-level `enabled=false` reproduced the Codex
  merge defect; Codex CLI `mcp get` reported `enabled=true` after generated project
  config added the explicit field. Regression tests cover older owned config migration
  and the tracked development overlay.
- Updating the MCP catalog also required the exact Cursor CLI/doctor and wheel-smoke
  catalogs to include `project_recall`. Focused Cursor tests and the full wheel smoke
  now pass with the same five-tool contract.

## Live installation and remaining host proof

After separate operator approvals, `make accept-global-codex` passed against the
tool-installed package with temporary state. It verified all five HTTP MCP calls,
two isolated Workspaces, Codex prompt bootstrap, zero doctor failures, unchanged
user Codex config, and owned cleanup. It did not run a model.

`make install-global HOST=codex` then replaced the live daemon and reconciled 19
Codex project configs. The install and the separately run `make doctor-global` both
exited successfully. Global doctor reported 67 OK, 7 WARN, 0 FAIL. Sixteen
Workspace configurations were individually checked as current; three were skipped
by the shared doctor time budget. The Hidden SCM enforcement warning is a known
host limitation, not a failed Codex registration.

The reconciled real Workspace configurations were:

- Doctor checked: `/home/gorills/projects/dom-sporta`,
  `/home/gorills/projects/MultiAgent`, `/home/gorills/projects/reka-landing-backend`,
  `/home/gorills/projects/food`, `/home/gorills/projects/mangazeya-backend`,
  `/home/gorills/projects/StoryWriter`, `/home/gorills/projects/linux-tools`,
  `/home/gorills/projects/server-monitoring`, `/home/gorills/projects/skupka`,
  `/home/gorills/projects/games/factorio3`, `/home/gorills/projects/portfolio`,
  `/home/gorills/projects/thebook.moscow`, `/home/gorills/projects/skyhouse.moscow`,
  `/home/gorills/projects/games/hardcore-rpg`, `/home/gorills/projects/trener`,
  `/home/gorills/projects/golovolomka`.
- Install reconciled but doctor budget did not inspect:
  `/home/gorills/projects/harness`, `/home/gorills/projects/food-go-new`,
  `/home/gorills/projects/alia`.

This existing Codex conversation still has the earlier MCP instruction snapshot.
The client must be fully quit and reopened into a fresh conversation; resume the
same unfinished Harness Task by ID. A model-selected desktop MCP call after that
restart remains unverified.
