# ADR-0054: Persist bounded proven Python resolved call edges as rebuildable Search-v2 data

- **Status:** Accepted
- **Date:** 2026-09-04
- **Deciders:** Repository architecture baseline

## Context

Schema v19 persists bounded syntax-proven `call`, `import`, and `inheritance` relations, but those rows
intentionally keep source spellings unresolved. ADR-0051 through ADR-0053 then added query-time Python
proofs in progressively stronger layers: caller-local import-binding resolution, one unique current
Workspace direct export, and bounded explicit re-export chains.

That query-time model is useful for exact symbol navigation, but natural relation-intent search still
cannot use a proven aliased target such as `service.target_call` when the persisted call spelling is
only `tc`. The next structural slice is therefore persistence of the already-bounded Python proof model,
without jumping to closure capture, receiver typing, override selection, dynamic dispatch, or runtime
Python import semantics.

A naive nullable target path on the existing relation row would be unsafe. A caller can remain unchanged
while its target module changes or disappears. Incremental scanning only the target path must invalidate
or rebuild the old proof; otherwise a persisted resolved edge becomes stale even though the source
relation row is still current. The persisted design therefore has to separate caller-local provenance
from Workspace-wide edge proof and rebuild the latter after structural reconciliation.

## Decision

### 1. Schema v20 persists caller-local Python lexical provenance separately from proven edges

`indexed_code_relations` remains the authoritative unresolved syntax relation table. Schema v20 adds
nullable fields used only for Python `call` rows whose caller-local AST analysis proves ADR-0051 import
binding resolution:

- `resolved_target`;
- `resolution_kind`;
- `resolution_module`.

The original `target`, relation kind, lexical scope, location, and test-path flag remain unchanged.
Nullable provenance is all-or-nothing. Non-Python relations remain unresolved in this slice.

`indexed_code_unit_files` gains `resolution_status` with the rebuildable states:

- `unindexed`;
- `ok`;
- `resolution_limit`.

A schema-v19 database migrates with `resolution_status=unindexed`, so the next scan reparses current
Python files before any persisted resolved proof is created. Existing durable Project/Workspace/Task/
Knowledge state remains untouched.

### 2. Safe explicit top-level Python re-exports are persisted as syntax provenance

Schema v20 adds `indexed_python_reexports`. Rows contain only:

- Workspace/path and bounded position;
- local exported name;
- imported name;
- syntactic module spelling.

They are produced from the same CPython AST and conservative module-scope binding analysis used by
ADR-0053. Conditional, duplicate, rebound, deleted, star, assignment-based, dynamic, and pure-relative
`from . import name` forms remain unsupported or fail closed. At most 4096 re-export rows are accepted
per file. Exceeding resolution metadata bounds sets `resolution_status=resolution_limit`, suppresses
re-export rows and caller-local resolution provenance for that file, and does not discard otherwise
valid unresolved relations or definitions.

### 3. Proven resolved calls live in a separate rebuildable Workspace projection

Schema v20 adds `indexed_resolved_code_relations`. Each row links exactly:

- one persisted source `indexed_code_relations.id`;
- one persisted target `indexed_code_units.id`;
- one validation kind:
  - `python_workspace_direct_export`, or
  - `python_workspace_reexport_chain`.

Foreign-key cascades remove an edge when either its source relation or target code unit is replaced.
The edge table does not duplicate source bodies, snippets, runtime objects, or an intermediate re-export
trace.

A Workspace manifest, `indexed_resolved_relation_workspaces`, records `ok` or `edge_limit` plus the
current edge count. At most 32768 caller relations with lexical resolution provenance are considered
per Workspace rebuild. Crossing that bound fails closed: the Workspace manifest becomes `edge_limit`
and the resolved edge/search projection is empty.

### 4. The resolved projection is rebuilt after every successful structural reconciliation

After `indexed_files`, code-unit manifests, definitions, unresolved relations, lexical provenance, and
re-export provenance have been reconciled inside the scan transaction, Harness deletes and rebuilds the
resolved-edge projection for that Workspace before committing the new index revision.

This rule applies to both full and incremental scans. Therefore changing only a target module still
re-evaluates every persisted caller-local candidate against the new current Structural Index snapshot.
No caller-file modification is required to invalidate an old proof.

The rebuild uses only current same-Workspace structural rows:

1. derive the requested export name from the persisted lexical `resolved_target` and
   `resolution_module`;
2. map the module spelling with the same deterministic Workspace module-path rules as ADR-0052;
3. require exactly one module-shaped candidate, including parse-failed/oversized candidates when
   deciding ambiguity;
4. accept exactly one direct top-level code unit when present;
5. otherwise follow at most four safe persisted explicit re-export edges with cycle detection;
6. fail closed on missing/ambiguous modules, duplicate definitions/re-exports, parse/unavailable
   structural state, unsupported re-export form, cycle, or a fifth re-export edge.

Direct definitions remain preferred over re-export traversal, matching current-source symbol navigation.

### 5. Resolved relation search is contentless and proof-gated

Schema v20 adds `indexed_resolved_code_relation_search`, a contentless FTS5 projection keyed by the
source relation id. It contains only:

- `resolved_target`;
- derived identifier tokens;
- lexical caller scope;
- relation-intent terms.

Natural code relation search may use this channel only when the Workspace resolved manifest is `ok` and
both source and target structural manifests still match their authoritative indexed SHA rows. A proven
aliased caller can therefore answer a query such as `who calls service target call` even when the source
literal is only `tc()`.

The existing unresolved relation projection remains available and unchanged for source-spelling search.
No source body is persisted in either FTS table.

### 6. Runtime and type semantics remain explicitly out of scope

A persisted resolved edge means only that the current Structural Index snapshot contains the bounded
syntactic proof described above. It does not prove:

- Python `sys.path`, environment, editable-install, namespace-package, or package installation behavior;
- import execution order, side effects, or final runtime rebinding;
- closure capture or nonlocal binding;
- assignment aliases, `__all__`, module `__getattr__`, or dynamic exports;
- receiver type, descriptor/member lookup, overrides, dynamic dispatch, or reachability.

Closure binding, receiver/type resolution, override/dynamic-dispatch selection, and semantic embedding/
reranking remain later independent Search-v2 slices.

## Consequences

- Natural Search-v2 relation queries can use proven Python alias/import targets without storing source
  bodies.
- A target-only incremental change cannot leave a stale persisted proof because the whole resolved
  Workspace projection is rebuilt transactionally.
- Schema v20 cleanly distinguishes unresolved syntax, caller-local lexical provenance, and stronger
  cross-file edge proof.
- Existing v19 unresolved relation behavior remains available as a fallback when stronger proof is
  unavailable.
- Incremental scans do extra bounded SQLite work to rebuild the resolved Workspace projection; the
  existing scan deadline remains authoritative and aborts the transaction rather than committing a
  partial projection.

## Verification

Acceptance coverage must prove:

- schema-v19 databases migrate to v20 without losing durable state;
- a Python `from ... import ... as ...` call persists lexical provenance and a direct target-unit edge;
- resolved relation-intent search finds the caller by the proven target spelling;
- a safe explicit re-export chain persists an edge to the final direct definition;
- changing only the target file removes an obsolete resolved edge while leaving the unchanged caller
  relation current;
- ambiguous module candidates and caller shadowing fail closed;
- Workspace edge-limit state clears the edge/search projection rather than returning a partial graph;
- source bodies remain absent from relation, re-export, resolved-edge, and resolved-FTS persistence;
- current-source exact/symbol navigation, v18/v19 structural projections, IPC/MCP schemas, scan
  currentness, benchmark counters, wheel smoke, exact-head CI, and post-merge CI remain green.
