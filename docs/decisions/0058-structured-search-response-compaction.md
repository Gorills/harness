# ADR-0058: Fit search responses once in structured MCP content

- **Status:** Accepted
- **Date:** 2026-09-05
- **Deciders:** Repository architecture baseline

## Context

The official MCP SDK converts a dictionary returned by a tool into two equivalent representations:
a JSON `TextContent` block for backwards compatibility and `structuredContent` for the current
structured-output protocol. Harness then measured both copies plus wire overhead against the same
12 KiB `project_search` exposure limit. A useful domain payload of roughly 6 KiB therefore became a
model-facing error even though either representation fit independently. Common searches with three
results and requested searches with ten results could lose the complete answer.

The retrieval layer already removed lower-priority current-source evidence when its approximation of
the response budget was full. It did not account for the SDK duplication or remove result metadata,
so the MCP boundary could only replace an otherwise valid result with a generic budget error.

## Decision

Successful `project_search` calls use the MCP 2026 structured-output representation as the single
authoritative model-facing payload. The required `content` array remains present but empty;
`structuredContent` retains the documented search object. Other tools and error results keep their
existing content behavior.

The MCP boundary fits search disclosure against a serialized structured-only `CallToolResult` plus
the existing wire-overhead reserve. Compaction is deterministic and preserves retrieval priority:

1. remove already-built evidence from the lowest-priority hit and label it
   `evidence_reason=response_budget`;
2. if metadata still does not fit, remove lowest-priority hits from the tail;
3. expose `results_truncated=true` when a requested hit was removed for the response budget.

Bounded exact coverage and symbol navigation remain ahead of file-level evidence and ranked hit
metadata. Their own aggregate counts and truncation flags continue to describe their disclosures.
The outer 12 KiB MCP response and stdio frame limits do not increase.

## Consequences

- Search no longer spends approximately half of its exposure budget on duplicate JSON.
- A large but useful result degrades to fewer explicitly marked hits instead of a generic tool error.
- Current MCP clients must consume `structuredContent`; legacy clients that only render non-empty
  `content` do not receive successful search data.
- This improves search discovery and evidence delivery but does not change the product rule that a
  targeted native source read can still be necessary when bounded evidence is absent or insufficient.

## Verification

Automated coverage must prove that:

- real stdio MCP search with limits three and ten succeeds within the 12 KiB frame;
- successful search has one structured representation and an empty `content` array;
- evidence is removed before lower-priority results;
- metadata overflow returns `results_truncated=true` rather than a budget error;
- exact coverage, symbol navigation, ranking order, and negative-disclosure contracts remain intact.
