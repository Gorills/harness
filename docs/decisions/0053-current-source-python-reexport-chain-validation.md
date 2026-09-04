# ADR-0053: Follow bounded explicit Python re-export chains in current-source navigation

- **Status:** Accepted
- **Date:** 2026-09-04
- **Deciders:** Repository architecture baseline

## Context

ADR-0051 added positive-only lexical Python import-binding resolution for query-time current-source
symbol navigation. ADR-0052 then allowed one stronger cross-file proof: when that lexical target maps to
one unique current Workspace Python module and one direct top-level definition, the call relation may
expose the concrete definition location.

A common Python package surface intentionally re-exports names through one or more modules, for
example:

```python
# package/api.py
from .impl import target_call
```

Under ADR-0052 that module is deliberately not treated as a direct definition, so a caller importing
`package.api.target_call` keeps the useful lexical `resolved_target` but receives no concrete Workspace
definition location. Following arbitrary Python import behavior would be too large a semantic jump: it
would require runtime path resolution, package initialization, dynamic exports, and execution-order
reasoning. A smaller syntactic extension can cover explicit re-export chains while preserving the same
positive-only, current-source, fail-closed model.

## Decision

### 1. Re-export validation extends ADR-0052; it does not change lexical resolution

Only a Python `call` relation that already carries ADR-0051 import-binding evidence and maps to one
unique ADR-0052 Workspace module candidate is eligible. `target`, `resolved_target`,
`resolution_kind`, and the internal `resolution_module` keep their existing meanings.

If the first module has one direct top-level definition for the requested export, ADR-0052 behavior is
unchanged and `resolution_validation_kind=python_workspace_direct_export` remains authoritative.

If there is no matching direct definition, Harness may follow a bounded explicit re-export chain. When
that chain reaches one direct top-level definition, the existing model-facing definition fields are
attached and `resolution_validation_kind=python_workspace_reexport_chain` identifies the stronger
proof. No re-export trace or intermediate module path is exposed.

### 2. Each followed hop must be one safe explicit top-level `from` import

A re-export hop is eligible only for a direct module-body statement of the form:

```python
from module import imported_name
from module import imported_name as exported_name
from .module import imported_name
from ..module import imported_name as exported_name
```

The local exported spelling must equal the name currently being resolved. The module-scope binding
analysis must also prove that this local name has exactly one import binding and no competing
module-scope binder. Conditional/nested imports, duplicate imports, rebinding, deletion, and other
module-scope claims therefore fail closed.

The following are intentionally not followed in this slice:

- `import module as alias`;
- star imports;
- pure-relative `from . import name` / `from .. import name` forms, because the imported object may be
  a package attribute or submodule rather than the requested symbol;
- assignment-based aliases;
- `__all__` declarations;
- module-level `__getattr__` or any other dynamic export mechanism.

This is syntactic re-export validation, not Python runtime import execution.

### 3. Every module hop reuses ADR-0052 deterministic Workspace matching

Absolute module spellings use the existing unique module-shaped suffix matching for `.py`, `.pyi`, and
package `__init__.py[i]`. Relative module spellings use the existing deterministic Workspace-relative
path arithmetic from the re-exporting file.

Every hop must resolve to exactly one current indexed Python module-shaped file. Missing or ambiguous
module candidates stop validation without erasing the original lexical `resolved_target`.

### 4. Chains are depth-bounded and cycle-safe

At most four re-export edges may be followed for one call relation. A direct definition reached after
zero through four followed edges is eligible. A fifth re-export edge is not followed.

The resolver records `(module path, exported name)` states. Revisiting a state stops the chain, so
cycles cannot consume the reread budget or claim a definition.

### 5. Currentness and byte bounds remain shared with ADR-0052

Target modules are reread against their indexed content SHA. Changed-since-index, unavailable,
non-text, oversized, or parse-failed modules fail closed.

The existing 1 MiB Python parser-file limit still applies to every target module. Direct and re-export
validation share the existing 4 MiB cross-file reread budget for the whole exact-search inspection, and
parsed export analyses are cached per path. The existing 16-relation, 5 KiB symbol-navigation, and
12 KiB `project_search` response limits remain authoritative.

### 6. Persistence and runtime semantics remain unchanged

Schema v19 is unchanged. No resolved edge, re-export edge, intermediate path, or validation result is
persisted. Scan-time `indexed_code_relations` remains an unresolved syntactic relation layer.

This decision does not prove:

- `sys.path`, environment, editable-install, or package installation behavior;
- Python import execution order or side effects;
- namespace-package runtime semantics;
- final runtime rebinding after the syntactic evidence;
- `__all__`, dynamic exports, descriptors, receiver types, overrides, dynamic dispatch, or reachability.

Persistent resolved edges, closure binding, type/dispatch-aware graph construction, and semantic
reranking remain separate later Search-v2 decisions.

## Consequences

- Explicit package facade modules can now lead exact symbol navigation to the unique current Workspace
  definition they re-export.
- Aliased and relative `from` re-export chains are supported without interpreting arbitrary Python
  runtime import behavior.
- Ambiguous, conditional, rebound, cyclic, too-deep, changed, or unsupported chains retain lexical
  resolution but omit the concrete definition proof.
- The feature remains query-time, source-current, bounded, explainable, and schema-neutral.

## Verification

Acceptance coverage must prove:

- one explicit re-export hop resolves to the final direct definition;
- relative and aliased multi-hop re-exports preserve the final definition path/kind;
- duplicate re-export bindings fail closed;
- cycles fail closed;
- exactly four re-export edges may reach a direct definition, while a fifth edge is not followed;
- a direct top-level re-export with a competing conditional module-scope import of the same name fails closed;
- pure-relative `from . import name` re-exports are not interpreted;
- direct ADR-0052 validation remains unchanged;
- schema stays v19 and persistent syntactic relations remain unresolved;
- response budgets, currentness, Search-v2 regression suites, benchmark counters, wheel smoke,
  exact-head CI, and post-merge CI remain green.
