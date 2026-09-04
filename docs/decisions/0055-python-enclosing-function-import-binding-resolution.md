# ADR-0055: Resolve safe Python imports from enclosing function scopes

- **Status:** Accepted
- **Date:** 2026-09-04
- **Deciders:** Repository architecture baseline

## Context

ADR-0051 deliberately limited Python import-binding resolution to the current function or module and
failed closed when an intervening enclosing function claimed the same root name. ADR-0054 later
persisted the same caller-local lexical provenance and used it to rebuild bounded Workspace resolved
call edges.

Python nested functions may legally reference a name imported in a lexically enclosing function. The
existing conservative collector already records imports and all competing binders per qualified
function scope, so Harness can support the common closure case without introducing runtime execution,
bytecode analysis, type inference, or a schema change.

## Decision

### 1. Resolution may walk enclosing function scopes from nearest to farthest

For an eligible Python call, Harness first applies the existing ADR-0051 rule in the current callable
scope. If that scope does not claim the root name, the resolver examines lexically enclosing function
or method scopes from nearest to farthest.

At each enclosing callable scope:

1. if exactly one safe import binding exists for the root and no competing binder exists, use that
   import binding;
2. otherwise, if the scope claims the root name in any way, fail closed and do not continue to a more
   distant scope or module scope;
3. otherwise continue outward.

If no enclosing callable scope claims the name, the existing module-scope import-binding rule remains
the final fallback.

The imported target spelling, `resolution_kind`, and `resolution_module` keep their existing meanings.
No new public wire field is added to identify whether the winning binding came from the current,
enclosing, or module scope.

### 2. Python lexical shadowing remains conservative

Any current or intervening callable-scope claim blocks an outer import proof unless that same scope has
one safe import binding. Existing binders remain authoritative blockers, including parameters,
assignments/deletions, definitions/classes, exception/match bindings, duplicate imports, and other
collector-visible local claims.

A `global` or `nonlocal` declaration remains fail-closed in this slice. Harness does not interpret the
redirected binding semantics of those statements.

This correctly preserves Python's static local-name behavior: an assignment anywhere in a function can
block an enclosing import even when the textual assignment occurs after the call.

### 3. Class namespaces are not treated as closure bindings

The resolver walks callable scopes only. A class-body import is not exposed as an unqualified binding
to a method. Existing module fallback behavior remains unchanged.

A callable nested beneath a class may still reach a lexically enclosing function outside that class
when the existing qualified callable-scope model proves such a function scope and no nearer callable
scope claims the name. The class namespace itself is never used as an import-binding provider.

### 4. Lambda and comprehension capture remain unsupported

ADR-0051 already suspends import-binding resolution while traversing lambdas and comprehensions. That
rule is unchanged. This slice covers named `def` / `async def` callable scopes only.

### 5. Schema v20 persists the stronger caller-local proof without migration

`analyze_precise_code_structure` uses the same Python resolver as current-source symbol navigation.
Therefore a safe enclosing-function import may populate the existing schema-v20 nullable
`resolved_target`, `resolution_kind`, and `resolution_module` fields on a persisted `call` relation.

No schema-v21 migration is required. The existing Workspace resolved-edge rebuild then validates the
persisted target against the same direct-export / bounded re-export machinery from ADR-0054.
Target-only invalidation, edge-limit behavior, contentless FTS, and foreign-key cascades are unchanged.

### 6. Runtime, receiver, and dispatch semantics remain out of scope

An enclosing-function import proof remains a lexical AST proof only. It does not prove:

- execution order or whether the enclosing function executed the import before the nested call;
- mutation of a captured cell after function creation;
- `global` / `nonlocal` redirected binding semantics;
- lambda or comprehension closure behavior;
- receiver/member type, descriptor lookup, overrides, dynamic dispatch, or reachability;
- installed-package, `sys.path`, namespace-package, or other runtime import behavior.

Receiver/type resolution, override/dynamic-dispatch selection, and semantic embedding/reranking remain
later independent Search-v2 slices.

## Consequences

- Exact Python symbol navigation can resolve calls through a unique safe import in a named enclosing
  function.
- The same proof can be persisted in schema v20 and participate in the existing rebuildable resolved
  call-edge/search projection.
- A nearer local, duplicate/ambiguous import, rebinding, `global`, or `nonlocal` claim prevents a more
  distant import or module fallback from being asserted.
- The implementation remains source-current, deterministic, bounded by existing parser/index/search
  limits, and schema-neutral.

## Verification

Acceptance coverage must prove:

- one nested function resolves an import from its enclosing function;
- multiple named nested functions may resolve the nearest safe outer import;
- intermediate local shadowing blocks a farther enclosing import;
- `nonlocal` remains fail closed;
- scan-time schema-v20 persistence stores the enclosing-function lexical provenance and creates the
  existing direct/re-export resolved edge;
- persisted resolution remains absent when an intermediate callable shadows the name;
- existing module/current-function import binding, re-export, target-only invalidation, schema-v20,
  current exact navigation, IPC/MCP, benchmark, wheel smoke, exact-head CI, and post-merge CI remain
  green.
