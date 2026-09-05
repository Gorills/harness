# ADR-0061: Expose search input bounds and retain exact punctuation coverage

- **Status:** Accepted
- **Date:** 2026-09-06

## Context

Release verification reproduced two avoidable search problems. MCP advertised an integer `limit`
without its accepted range, so otherwise plausible calls such as `limit=12` failed. Quoted operators
such as `"->"` produced valid exact locations internally but the subsequent lexical channel rejected
them because punctuation has no FTS tokens.

Profiling also found repeated tokenization in natural-search evidence assembly: each significant
query term independently tokenized the entire source and then each source line. This multiplied
evidence work by query width even though term matching uses the same normalized tokens.
Resolved Python edge rebuilding also repeatedly walked the full module inventory for identical
imports, extending the time that reconciliation holds the daemon scan lock.

## Decision

- Publish the existing MCP `limit` contract as JSON Schema `integer`, minimum 1, maximum 10,
  default 5. Retain strict runtime rejection of booleans, strings, fractions, and out-of-range values.
- Describe the query limit on its schema property: non-empty text, no NUL, and at most 256 UTF-8
  bytes after trimming surrounding whitespace. Keep `minLength=1`. Do not misrepresent that byte
  limit as JSON Schema `maxLength`, which counts characters before the existing normalization.
  Trim that same surrounding whitespace before IPC forwarding and model-visible query echo, so
  padding accepted by the transport cannot exhaust the search response budget. Transport request
  body bounds continue to apply to the original incoming message before tool argument handling.
- Code/docs/all searches with a valid exact needle remain successful when no lexical terms exist.
  They return exact aggregate counts and locations, with an empty ranked-results channel. A missing
  literal returns complete zero coverage when the normal source-completeness conditions hold.
  Knowledge and Task searches retain their requirement for searchable lexical terms.
- Match all query terms against one tokenization per source line when relocating rich evidence.
  Preserve Unicode/camel/snake normalization, prefix matching, window selection, source SHA checks,
  and existing byte/item exposure budgets.
- Cache Python module-candidate uniqueness only within one resolved-edge rebuild. Absolute imports
  share the cache across callers; relative imports retain source identity. Retain the existing
  fail-closed treatment of missing/ambiguous modules and rebuild the cache for every reconciliation,
  including changes only to target modules. No process-global cache or new persistence is introduced.
- Extend `scripts/benchmark_hot_paths.py` with warm natural and exact Project-search latency plus
  Git subprocess counts. These measurements exercise the daemon-domain boundary against the same
  isolated synthetic fixture; they exclude MCP transport and first-search reconciliation. Report
  hardware and fixture size with measurements. Do not impose wall-clock assertions in unit tests.

## Verification

Regression coverage includes punctuation exact matches and misses through daemon/MCP retrieval,
strict limit/schema agreement, UTF-8 byte limits and padded queries, and a deterministic evidence
tokenization-work bound. Existing search-quality and evidence fixtures protect ranking and window
semantics. Search continues to use ADR-0046 currentness and the established 12 KiB response budget.
Repeated-import fixtures verify bounded module walks, distinct relative targets, and invalidation
when another matching module is added or removed between incremental scans.
