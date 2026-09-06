# ADR-0060: Rank dense high-coverage natural matches ahead of dispersed full-file matches

- **Status:** Accepted
- **Date:** 2026-09-05
- **Deciders:** Repository architecture baseline

## Context

Code/document retrieval required every significant natural-query term to match somewhere in one
indexed file and then used whole-file BM25 within the all-terms quality tier. Generic location-intent
words such as “implemented” or “verifies” could remove the correct file entirely. Conversely, a large
file with every term scattered across unrelated sections outranked a compact implementation or test
that omitted one word. Better evidence windows made this mismatch visible but did not correct result
order.

The content FTS index is intentionally contentless, but FTS5 still retains token positions needed by
its bounded `NEAR` operator. This can provide a deterministic local proximity signal without storing
or disclosing source bodies, introducing embeddings, or reading a large candidate set merely to rank
it.

## Decision

For natural code/docs queries, retrieval keeps the existing all-terms candidate query and adds one
bounded high-coverage query when there are three through eight significant terms. The additional
expression is the OR of every leave-one-term-out conjunction, so it admits only `N-1/N` matches. It
does not degrade into arbitrary OR retrieval, and queries outside that term bound keep all-terms-only
candidate generation.

The same full and leave-one-out term sets form one FTS5 `NEAR` expression with a 128-token distance.
Candidates matched by that expression receive a `dense lexical content` quality tier between
title/identifier phrases and dispersed all-terms content. Inside the dense tier, greater term coverage
still wins before BM25. Match reasons disclose whether the dense result covers all terms or `N-1/N`.

Natural query analysis always treats `before` as filler and conditionally treats `enforced`,
`implemented`, and `verifies` as conversational location intent when a where/how/test marker is
present; those terms remain searchable outside that shape. The conservative English inflection list
handles `-ation` before `-ion`, allowing `installation` to match `install` identifiers. Exact query text,
exact coverage, and symbol navigation are unchanged.

## Consequences

- One extra descriptive term no longer removes an otherwise concentrated implementation or test.
- A dense `N-1/N` result can outrank a large unrelated file with all terms scattered across it.
- Search remains deterministic, local, explainable, and contentless at rest.
- At most one additional candidate SQL query and one proximity SQL query run per code/docs channel;
  query construction is bounded to eight significant terms and the existing 96-row candidate cap.
- This is lexical proximity, not semantic equivalence; embeddings or a reranker may remain useful for
  cross-vocabulary questions.

## Verification

Automated coverage must prove that:

- a dense five-of-six test result beats a dispersed six-of-six source file;
- match reasons report dense coverage explicitly and evidence comes from the dense target;
- natural location-intent words do not overconstrain the query;
- `installation` finds an `install` identifier;
- exact path, identifier, Knowledge, Task, current-source, response-budget, and negative-disclosure
  contracts remain green.
