# ADR-0057: Resolve bounded same-file Python inheritance receiver calls in current-source navigation

- **Status:** Accepted
- **Date:** 2026-09-05
- **Deciders:** Repository architecture baseline

## Context

ADR-0056 added a narrow current-source proof for exact `self.method()` / `cls.method()` calls when the
caller receiver and a direct method in the same syntactic class have safe descriptor shapes. It
intentionally failed closed for inheritance.

A common next case is still structurally simple:

```python
class Base:
    def target_call(self): ...


class Worker(Base):
    def invoke(self):
        return self.target_call()
```

The source file contains enough syntax to identify a nearest declared method along a small single-base
chain without implementing Python's general MRO or runtime dispatch. The proof must remain distinct from
runtime target identity: `Worker.invoke()` may later run on a subclass instance that overrides
`target_call`.

## Decision

### 1. Extend only the ADR-0056 query-time receiver layer

For an already proven ADR-0056 `self` or `cls` receiver whose current class has no safe direct definition
of the requested member, Harness may walk a bounded same-file declared base chain.

A successful inherited proof attaches:

- `resolved_target=<declaring top-level class>.<method>`; and
- `resolution_kind=python_self_inherited_method_binding` or
  `python_cls_inherited_method_binding`.

The original source `target` remains unchanged. `resolution_module` and `resolved_definition_*` remain
absent. Schema v20 is unchanged and scan-time structural extraction does not persist receiver-derived
provenance or resolved edges.

### 2. Only unique top-level classes and one simple-name base are eligible

Inheritance walking is enabled only for a direct top-level `class` statement that:

- is the unique module-scope binder of its class name;
- has no class decorators;
- has no class keywords such as an explicit metaclass;
- has zero or one base expression; and
- when a base exists, that base expression is one plain `ast.Name`.

The referenced base must itself be one unique eligible top-level class in the same current source file.
Imports, qualified bases, subscripted generic bases, conditional/nested class definitions, duplicate or
rebound class names, multiple inheritance, and cross-file bases fail closed.

This is deliberately stricter than Python runtime class construction.

### 3. Nearest safe direct method wins; any nearer class-namespace binder blocks the walk

At each class in the chain, Harness first looks for one safe direct method using the ADR-0056 descriptor
rules: undecorated, exactly `@classmethod`, or exactly `@staticmethod`.

A direct method is safe only when its name is the only class-namespace binding of that spelling. A later
assignment/import/delete/second definition or any other conservative class-scope binder therefore makes
that method unsafe.

If a class has any class-namespace binder for the requested name but no safe direct method, inheritance
resolution stops. Harness never skips an unsafe/property/custom-decorated override or class attribute to
reach a farther base definition.

When no binder exists, the resolver may continue to that class's one eligible base.

### 4. The chain is bounded and cycle-safe

At most four declared base edges may be followed. A definition reached after one through four edges is
eligible; a fifth edge is not followed.

Visited class names are tracked. Re-entering a class name stops the proof, so syntactic cycles fail
closed even though such source would not construct normally at runtime.

### 5. The proof is a nearest syntactic definition, not runtime dynamic dispatch

`resolved_target=Base.target_call` means only that `Base.target_call` is the nearest safe direct method
found along the bounded declared class chain in this current file.

It does **not** prove that a runtime call will dispatch to that implementation. In particular this slice
does not model:

- invocation on a subclass instance that overrides the method;
- Python MRO or multiple inheritance;
- `super()`;
- cross-file or imported bases;
- constructor/local variable type flow;
- abstract/protocol/interface semantics;
- descriptors beyond the narrow ADR-0056 syntactic shapes;
- metaclasses, monkey patching, `__getattribute__`, `__getattr__`, or runtime class mutation;
- reachability or execution order.

A future runtime-dispatch slice should represent a bounded candidate set rather than force one target
when more than one implementation is semantically possible.

## Consequences

- Exact current-source navigation can connect simple same-file inherited `self` / `cls` calls to the
  nearest declared safe method without introducing general type inference.
- Direct ADR-0056 resolution remains higher priority than inherited resolution.
- Class-namespace shadowing is now explicitly counted so `def name(...); name = ...` cannot be mistaken
  for a safe direct method and inherited walking cannot skip a nearer binder.
- The additional binding-count bookkeeping is internal AST analysis metadata only; schema v20 remains
  unchanged.
- Persisted resolved edges remain import-derived only.

## Verification

Acceptance coverage must prove:

- one same-file single-base `self` call resolves to the base method;
- inherited `cls` calls resolve under the same bounded rule;
- the nearest safe override wins;
- exactly four base edges may be followed while a fifth is rejected;
- multiple inheritance and cross-file bases fail closed;
- class-attribute and unsafe/property override shadowing stop the walk;
- a direct method followed by another class-namespace binding is not treated as safe;
- a rebound/ambiguous top-level base class name fails closed;
- syntactic inheritance cycles fail closed;
- scan-time schema-v20 relations remain unresolved for inherited receiver calls;
- existing import/closure/re-export persistence, exact currentness, IPC/MCP, benchmark counters, wheel
  smoke, exact-head CI, and post-merge CI remain green.
