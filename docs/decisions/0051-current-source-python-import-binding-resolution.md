# ADR-0051: Resolve bounded Python import bindings in current-source symbol navigation

- **Status:** Accepted
- **Date:** 2026-09-03
- **Deciders:** Repository architecture baseline

## Context

ADR-0050 intentionally persisted only unresolved syntactic `call`, `import`, and `inheritance`
relations. Equal target spellings are useful structural evidence, but they do not connect an aliased
Python call such as `tc()` back to `from service import target_call as tc`, and a module alias such as
`svc.target_call()` does not match an exact qualified query for `service.target_call`.

Jumping directly from that limitation to a persisted cross-file/type-aware graph would combine several
independent correctness problems: Python lexical binding, import execution, module/export resolution,
receiver typing, override selection, and dynamic dispatch. The current exact-search path already reads
and parses bounded current source, so it can prove a smaller positive binding fact without changing the
durable schema.

## Decision

### 1. Exact Python symbol navigation may expose a resolved import target

For query-time Python AST analysis only, a `call` relation may additionally carry:

- `resolved_target`: the import target proven by a unique lexical import binding; and
- `resolution_kind`: `python_import_binding` or `python_from_import_binding`.

The existing `target` remains the source spelling. For example:

- `from service import target_call as tc; tc()` keeps `target=tc` and may expose
  `resolved_target=service.target_call`;
- `import service as svc; svc.target_call()` keeps `target=svc.target_call` and may expose
  `resolved_target=service.target_call`.

These optional wire fields are emitted only when resolution succeeds. Existing unresolved relation
payloads therefore retain their previous shape.

### 2. Resolution is positive-only and fail-closed

Harness resolves a Python import binding only when the root name has one import binder in the relevant
module/function scope and no competing static binder in that scope. Parameters, assignments,
definitions, deletion, exception/match bindings, `global`/`nonlocal`, duplicate imports, and other
explicit binders suppress the resolution rather than guessing.

A function-local import may resolve calls in that same function. A module import may resolve calls in a
function only when the function and intervening function scopes do not bind the same root name.
Potential closure bindings are not followed in this slice. Calls inside lambda/comprehension scopes and
class-body execution are intentionally left unresolved.

The result is evidence about a unique lexical import binder, not a proof that the import executed, that
the module/export exists, or that the runtime object was not mutated.

### 3. `from` imports do not imply member/type resolution

For `from module import value as alias`, only a direct call of the imported local name may receive the
resolved import target. Harness does not turn `alias.member()` into `module.value.member`, because that
would cross from import binding into runtime member/type resolution.

For `import module as alias`, a dotted member spelling may be rewritten under the proven module binding,
but that remains import-binding evidence only. It does not resolve dynamic module attributes or runtime
dispatch.

### 4. Persistent schema v19 is unchanged

`analyze_precise_code_structure(...)` remains the scan-time unresolved syntax provider used by schema
v19. Import-binding analysis runs only for query-targeted current-source Python symbol navigation.
No `resolved_target` is persisted, no new FTS projection is added, and no schema migration is required.

This keeps the persistent relation layer conservative until the current-source behavior has independent
acceptance coverage.

### 5. Exact candidate and response bounds remain unchanged

The existing exact current-source scan, 1 MiB parser-file bound, 64 MiB query scan bound, 16-relation
symbol-navigation bound, 5 KiB symbol-navigation payload, and overall 12 KiB search response remain
unchanged. Resolution reuses the already parsed Python AST and does not add filesystem reads or parser
passes to schema-v19 indexing.

## Consequences

- Exact Python symbol navigation can connect common import aliases to the imported target without
  claiming a general program graph.
- Qualified exact queries can classify a module-aliased call even when the qualified literal never
  appears contiguously in source.
- Explicit rebinding/shadowing produces no resolved target instead of a speculative edge.
- Persisted schema-v19 relations remain unresolved and rebuildable.
- Cross-file module/export validation, closure import resolution, persistent resolved edges, receiver
  types, overrides, dynamic dispatch, and semantic reranking remain later bounded slices.

## Verification

Acceptance coverage must prove:

- a Python `from ... import ... as ...` call exposes the source target plus the imported
  `resolved_target`;
- an `import ... as ...` member call can satisfy an exact qualified imported-target query even when the
  qualified literal has zero exact source occurrences;
- parameter/module rebinding suppresses resolution and does not add an imported-target call;
- existing Python/polyglot syntax navigation, persistent schema-v19 relations, current exact coverage,
  stale-source behavior, response budgets, quality gates, benchmark counters, wheel smoke, exact-head
  CI, and post-merge CI remain green.
