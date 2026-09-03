# ADR-0052: Validate bounded Python import targets against current Workspace exports

- **Status:** Accepted
- **Date:** 2026-09-03
- **Deciders:** Repository architecture baseline

## Context

ADR-0051 added positive-only Python import-binding resolution to query-time current-source symbol
navigation. A call such as `tc()` may retain `target=tc` while exposing
`resolved_target=service.target_call` when one unshadowed lexical import binder proves that rewrite.
That fact is intentionally local to the importing source file: it does not prove that a matching
Workspace module exists or that the named export is defined there.

The next Search-v2 structural gap is useful cross-file navigation without jumping to a persistent
resolved graph, recursive import semantics, runtime import execution, or type-aware dispatch. The exact
search path already has a current Structural Index snapshot and may reread a small number of bounded
current source files, so it can add a narrower positive proof: one unique module-shaped Workspace path
contains one direct top-level definition for the imported target.

## Decision

### 1. Cross-file validation remains query-time and positive-only

Only a Python `call` relation that already carries ADR-0051 `resolved_target` import-binding evidence is
eligible. Validation does not replace or strengthen the meaning of that lexical field. When an eligible
call additionally proves one unique current Workspace module and one direct top-level definition, the
model-facing relation may expose:

- `resolved_definition_path`;
- `resolved_definition_line`;
- `resolved_definition_column`;
- `resolved_definition_kind`; and
- `resolution_validation_kind=python_workspace_direct_export`.

These fields are omitted when the proof is unavailable or ambiguous. No negative export claim is
returned.

### 2. Module-path matching is deterministic and fail-closed

For absolute import modules, Harness maps the dotted module spelling to Python module-shaped file
suffixes and requires exactly one indexed candidate across the Workspace:

- `pkg.mod.py` form: `pkg/mod.py` or `pkg/mod.pyi`;
- package form: `pkg/mod/__init__.py` or `pkg/mod/__init__.pyi`.

A leading Workspace source root such as `src/` is tolerated through suffix matching, but two matching
paths make validation ambiguous and suppress the proof. `.py` and `.pyi` are not arbitrarily preferred
over one another.

Relative imports are resolved only as deterministic Workspace-relative path arithmetic from the
importing file's parent directory. One leading dot keeps the current package directory; each additional
dot ascends one directory. Crossing above the Workspace root fails closed. This is path validation, not
a claim about `sys.path`, package installation, namespace-package runtime behavior, or import success.

### 3. Only one direct top-level export is accepted

The validated module is reread with its indexed SHA and parsed with the existing Python code-unit
provider. The imported member must identify exactly one module-level definition. Functions, classes,
and module-level variables already recognized by that provider are eligible.

This slice intentionally does not follow:

- re-export chains such as `from impl import name` inside the target module;
- `__all__` policy;
- dynamic `__getattr__` exports;
- star imports;
- multi-member chains such as `module.client.method`;
- descriptor/member lookup, runtime mutation, receiver typing, overrides, or dispatch.

Duplicate direct definitions of the same name fail closed rather than selecting one by source order.

### 4. Validation has an independent bounded reread budget

Only relations that survive the existing symbol-navigation relation/response bounds are considered.
At most the already bounded relation set can therefore trigger validation. Target parser files remain
subject to the existing 1 MiB Python parser bound, and cross-file validation may reread at most 4 MiB
of indexed target-module source per exact-search inspection. Parsed target definitions are cached per
path inside that inspection.

Changed-since-index, unavailable, non-text, oversized, parse-failed, over-budget, missing, or ambiguous
target modules simply receive no cross-file validation fields. The existing exact-search 12 KiB total
response and 5 KiB symbol-navigation budgets remain authoritative; evidence and relations are trimmed
by the existing budget fitter if the new optional fields consume space.

### 5. Persistent schema v19 remains unresolved

No database column, FTS projection, or migration is added. `indexed_code_relations` remains the
unresolved parser-proven syntax layer from ADR-0050. Cross-file validation is computed only from the
current Workspace during exact symbol navigation.

Persisting validated edges, recursive export resolution, closure binding, and type/dispatch-aware graph
construction remain separate later decisions.

## Consequences

- Common aliased Python calls can now point to a concrete current Workspace definition when that proof
  is unique and direct.
- Missing or ambiguous modules do not erase the useful ADR-0051 lexical `resolved_target`; they merely
  omit the stronger cross-file proof.
- Qualified import-alias queries can expose a target definition location even when the qualified literal
  never appears contiguously in source.
- The feature remains bounded, source-current, explainable, and schema-neutral.
- The result is still not a runtime call graph or proof of Python import execution.

## Verification

Acceptance coverage must prove:

- a `from ... import ... as ...` call receives a current Workspace direct-definition location when the
  module/export match is unique;
- an `import ... as ...; alias.member()` call can receive the same proof for a qualified query with zero
  exact qualified source occurrences;
- relative imports resolve to the deterministic package-relative Workspace module path;
- duplicate module-shaped Workspace candidates suppress validation;
- a re-export-only target module does not receive a direct-export proof;
- missing target modules preserve ADR-0051 lexical resolution while omitting validation fields;
- schema remains v19 and persistent syntactic relations remain unchanged;
- existing Search-v2 currentness, symbol navigation, relation/code-unit persistence, response budgets,
  quality gates, benchmark counters, wheel smoke, exact-head CI, and post-merge CI remain green.
