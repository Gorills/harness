# ADR-0071: Retire project code and document search

- **Status:** Accepted for implementation
- **Date:** 2026-09-23
- **Deciders:** Operator request
- **Supersedes:** ADR-0010, ADR-0021 and later code/document search extensions where they specify an active search product surface

## Context

The operator finds Harness project search unhelpful and explicitly asked to remove the feature
and the instruction that agents search through Harness. A search-first rule adds a tool call even
when an agent can use its native repository tools directly. The old project search spans MCP,
CLI, IPC and dashboard Workspace code/path results, along with derived retrieval projections.
Those surfaces must describe one consistent product behavior.

## Decision

Remove the agent-facing `project_search` MCP tool, the `harness search` CLI command, and the
dashboard Workspace code/document/path search. Remove the requirement to call Harness before
native repository exploration. Agents use their host's native file and search tools to locate
code and documentation. Harness still provides `project_status`, explicit `project_context` for
durable references, and Task lifecycle operations.

Human Task lookup and list filtering remain part of the dashboard. They operate on durable Task
records and do not imply a repository code/document search service. Knowledge/Task context may
still be opened by an explicit reference. Existing explicit code/document refs remain
metadata-only compatibility inputs to `project_context`; Harness no longer generates them
through a search tool. Structural Index data may remain where status,
Knowledge freshness, skill relevance, or another independently retained function needs it; the
retired search feature is not a reason to keep a redundant derived projection. Schema v24
drops project-search-only code/document, symbol, Knowledge-search and currentness projections;
the Task-search projection remains for human Task lookup.

Historical search ADRs remain as records of the previous design. They no longer define the
active agent workflow or product surface. The original specification is preserved; this ADR
amends its search-first and five-tool expectations.

## Consequences and compatibility

The MCP advertised tool list and generated agent instructions change together. Clients that
call the removed tool or CLI command receive an ordinary unknown-tool/command error; no search
compatibility alias is kept. Dashboard Task lookup remains available. Existing Task and
Knowledge records keep their durable identities and content. Derived search state is dropped through schema v24; authoritative records are not deleted as a side effect. Updating checkout source alone does not refresh a running host's MCP
instruction snapshot; host configuration is reconciled and the host is restarted separately.

## Verification boundary

Check the exact advertised MCP tools, generated bootstrap text, CLI help, and dashboard routes
for the absence of project code/document search. Check that Task lookup and explicit context
continue to work and that schema migration preserves authoritative Task/Knowledge data. Use
native repository search in agent acceptance scenarios. Do not claim a new real-host behavior
from unit tests alone.

## 2026-09-23 amendment: retain agent access to durable memory

Codex integration audit found that removing `project_search` also made older Knowledge and Task
records undiscoverable to an agent in a fresh conversation. `project_context` requires an exact
reference, while `project_status` names only the current or relevant waiting Task. The human
Task archive does not make Knowledge IDs available to the agent. This breaks the product's
cross-session memory use case even when those records remain correctly stored.

Add `project_recall(query, kind, limit)` as an optional MCP lookup limited to durable
`knowledge` and `task` records. It returns bounded references and brief descriptions, not
record bodies; a selected result is expanded through `project_context`. Previews may
be omitted to fit the model-visible byte budget, and the response reports dropped hits.
Both kinds apply the active Workspace's Git applicability before disclosure. Task lookup may reuse the retained
Task search projection; Knowledge lookup uses authoritative cards without restoring the
removed Knowledge FTS projection. Knowledge cards are streamed under a finite deadline;
exhaustion returns an explicit error rather than a partial search result. There is no
code/document/path search and no instruction to call this tool before native repository work. An unknown prior ID can now be found by
topic in a fresh session. The resulting five-tool catalog is a different contract from
the historical five-tool catalog that included `project_search`.

Verification must cover discovery without a prior ID, cross-Project and cross-branch
non-disclosure before result limits, response budgets, source-text exclusion, exact
advertised schemas, and the Codex acceptance catalog.
