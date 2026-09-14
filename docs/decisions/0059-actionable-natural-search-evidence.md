# ADR-0059: Prefer structural anchors and dense partial windows for natural-search evidence

- **Status:** Accepted
- **Date:** 2026-09-05
- **Deciders:** Repository architecture baseline

## Context

Current-source evidence was safe but often absent or poorly localized for natural agent queries.
File-level FTS can match all significant terms across a large document, while the evidence builder
required every term present in that source body to fit inside one 48-line window. Relevant large
files therefore returned `current_match_not_relocated` even when several query terms formed a useful
local cluster.

Persistent code-unit and code-relation indexes already carry an exact source line. Retrieval used
that line to rank a structural candidate, then discarded it before building evidence and repeated a
whole-file term relocation. A repeated term near the start of a file could consequently displace the
actual definition or call selected by the structural index. Failed or changed rereads also consumed
the same three-slot counter as successfully returned evidence, preventing later hits from supplying
source context.

## Decision

### Amendment: whole definitions and compact evidence (2026-09-14)

Whole normalized code-unit names or qualified names rank ahead of identifier substrings, while
exact paths, filenames, and filename stems retain precedence. This also applies after ordinary
natural-query normalization: `where project search happens` prefers `project_search` over
`_first_project_search` or `_PROJECT_SEARCH_DESCRIPTION`. Ranking and selection of each file's
best code unit happen in SQL before the bounded candidate cap. A file with many definitions
therefore cannot consume all candidate slots and hide other files' structural matches. This is
lexical/syntax ranking, not a claim of semantic completeness or Russian-to-English translation.

Code-unit candidates carry a private qualified parent scope alongside the existing line hint.
After the current-source SHA check, definition evidence starts at that declaration and extends
forward within 48 lines and 3 KiB. A bounded indexed lookup stops it before the next declaration
in the same or an ancestor scope, including a top-level declaration after a class's last method.
These are indexed syntax boundaries, not inferred runtime ownership or a guarantee of a complete
function body. No new parser pass, persisted schema, or model-visible field is added.

Call/import/inheritance anchors keep three context lines on either side. File-level lexical
evidence keeps its shortest maximum-coverage range plus at most three lines of context per side,
still inside the 48-line bound. It no longer fills every available line with unrelated padding.
Private line/scope hints are consumed before serialization. Existing SHA/containment checks,
three returned evidence slots, 12 KiB response budget, and explicit truncation remain unchanged.

Regression fixtures cover more than 96 misleading definitions, per-file candidate fairness,
natural and exact queries, path precedence, large preceding functions, short definitions followed
by unrelated siblings, ancestor-scope boundaries, and compact call/lexical context.

### Original decision

Code-unit definition, proven call, and syntactic relation candidates carry an internal
`evidence_line` through ranking. After the existing current-file containment, regular-file, UTF-8,
and indexed-SHA checks, evidence is built around that exact line when it still contains at least one
significant query term. The hint is consumed inside daemon retrieval and is never added to IPC or the
model-facing DTO.

For a file-level hit without a usable structural anchor, evidence relocation chooses the shortest
window that contains the maximum number of distinct significant query terms within the existing
48-line bound. A multi-term source must contribute at least two terms to the selected window. Exact
single-term behavior is unchanged. The 3 KiB per-window and 12 KiB response limits remain in force,
and evidence still comes only from a live source reread whose SHA matches the current index.

The three-hit evidence counter now counts snippets actually attached to the response. Safe reread or
relocation failures remain explicit but do not prevent lower-ranked hits from using an available
evidence slot. At most the requested ten files can be examined by one search.

## Consequences

- Natural queries over large files return a useful dense current-source window more often.
- Structural definition/call/relation hits show the source selected by the index instead of an
  earlier lexical mention.
- Later hits can still carry evidence when earlier files changed or could not be safely relocated.
- A dense partial window does not claim that every file-level term occurs inside that snippet; the
  hit-level `match_reason` continues to describe the whole indexed document match.
- This improves answer sufficiency without storing source bodies or weakening live-file validation.

## Verification

Automated coverage must prove that:

- widely separated two-term matches still respect the 48-line cap while a denser multi-term cluster
  produces evidence;
- code-unit and relation hits anchor evidence at their persisted source line despite an earlier
  lexical mention;
- changed/failed earlier hits do not consume evidence slots needed by a later valid hit;
- SHA mismatch, symlink containment, invalid UTF-8, response-budget, and negative-disclosure tests
  remain green.
