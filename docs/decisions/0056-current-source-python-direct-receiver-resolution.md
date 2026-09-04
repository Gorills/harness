# ADR-0056: Resolve bounded direct Python `self` / `cls` receiver calls in current-source navigation

- **Status:** Accepted
- **Date:** 2026-09-04
- **Deciders:** Repository architecture baseline

## Context

Search v2 can now persist and query bounded Python call edges proven through lexical import bindings,
current Workspace direct exports, explicit re-export chains, and safe named enclosing-function import
bindings. Those proofs still do not help a common local call shape such as:

```python
class Worker:
    def target_call(self): ...

    def invoke(self):
        return self.target_call()
```

The source spelling `self.target_call` does not identify a globally importable symbol, but in a tightly
bounded class body Harness can prove more than unresolved syntax without implementing general Python
member typing or dynamic dispatch. The next slice should add that proof only to current-source symbol
navigation and should not silently widen schema-v20 persistence.

## Decision

### 1. Receiver resolution is query-time current-source evidence only

Query-targeted Python AST analysis may attach:

- `resolved_target=<qualified class>.<method>`; and
- `resolution_kind=python_self_method_binding` or `python_cls_method_binding`

to a `call` relation whose source target is exactly `self.<method>` or `cls.<method>`.

The original source `target` remains unchanged. `resolution_module` remains absent because this is not
an import proof. Schema v20 is unchanged, and scan-time `analyze_precise_code_structure` does not emit
receiver-resolution provenance or a persisted resolved edge for these calls.

### 2. `self` is recognized only for one direct undecorated method shape

A `self.<method>()` call is eligible only when all of the following hold:

- the caller is a direct `def` / `async def` statement in the current class body;
- the caller has no decorators;
- its first positional parameter (positional-only or normal positional) is literally named `self`;
- `self` is not rebound, deleted, imported over, declared `global` / `nonlocal`, or otherwise claimed
  by the conservative function-scope binding analysis.

A nested function inside that method does not inherit this receiver proof. Lambdas and comprehension
binding scopes remain suspended as before.

### 3. `cls` is recognized only for one direct syntactic classmethod shape

A `cls.<method>()` call is eligible only when the direct caller has exactly one decorator whose dotted
syntax is `classmethod` and its first positional parameter is literally named `cls`. Additional/custom
decorators, `staticmethod`, a different first parameter, and receiver rebinding fail closed.

This is a syntactic descriptor rule, not proof that the runtime `classmethod` builtin was not rebound.

### 4. The target must be one unique safe direct method of the same class

Harness resolves only one-component member calls (`self.name()` / `cls.name()`). `self.member.name()`
and other chains are not followed.

The target name must correspond to exactly one direct `def` / `async def` in the same class body. That
target method is eligible only when its descriptor shape is one of:

- undecorated direct method;
- exactly `@classmethod`;
- exactly `@staticmethod`.

Duplicate definitions and custom/property/other decorated targets fail closed. Inherited methods are not
considered. The resolved spelling uses the current syntactic qualified class name, including enclosing
named scopes where present.

### 5. This is not general type or dispatch resolution

ADR-0056 does not prove:

- inheritance, MRO, overrides, `super()`, or dynamic dispatch;
- constructor flow such as `x = Worker(); x.target_call()`;
- imported/annotated variable types;
- descriptor replacement, metaclass behavior, monkey patching, or runtime class mutation;
- `__getattribute__`, `__getattr__`, or dynamic member synthesis;
- runtime rebinding of the `classmethod` / `staticmethod` names;
- reachability or execution order.

Override/dynamic-dispatch reasoning remains a separate later slice. Constructor/local type-flow may be
considered independently if it can retain the same positive-only bounded model. Semantic embedding and
reranking also remain separate.

## Consequences

- Qualified exact navigation can connect a direct `self` / `cls` call with a same-class direct method
  without pretending to implement Python runtime dispatch.
- Simple identifier searches keep their existing syntactic call coverage; the stronger target spelling
  is additional positive evidence.
- Unsafe receiver or descriptor shapes retain the original unresolved source relation.
- Schema v20 and its persisted import-derived resolved-edge projection remain unchanged.

## Verification

Acceptance coverage must prove:

- direct undecorated `self` method calls resolve to the same-class qualified target;
- direct syntactic `@classmethod` `cls` calls resolve;
- safe `@staticmethod` targets may be reached through a proven receiver;
- receiver rebinding fails closed;
- the receiver must be the first positional parameter;
- a custom-decorated caller fails closed;
- custom/property target descriptors fail closed;
- inherited methods and multi-component member chains are not inferred;
- scan-time schema-v20 relations do not persist receiver-resolution provenance or resolved edges;
- existing import/closure/re-export resolution, currentness, schema-v20 persistence, IPC/MCP,
  benchmark counters, wheel smoke, exact-head CI, and post-merge CI remain green.
