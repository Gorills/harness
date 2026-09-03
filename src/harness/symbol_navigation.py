from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from ast_grep_py import SgNode, SgRoot

MAX_SYMBOL_PARSE_BYTES = 1024 * 1024
MAX_SYMBOL_RELATION_EVIDENCE_LINES = 32
MAX_SYMBOL_DEFINITION_EVIDENCE_BYTES = 2048
MAX_SYMBOL_REFERENCE_EVIDENCE_BYTES = 1024
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
_POLYGLOT_SUFFIX_LANGUAGE = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}
_TEST_DIRECTORY_NAMES = frozenset({"test", "tests", "testing", "spec", "specs"})


@dataclass(frozen=True, slots=True)
class SyntaxRelationEvidence:
    start_line: int
    end_line: int
    snippet: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class SyntaxRelation:
    kind: str
    path: str
    line: int
    column: int
    scope: str | None
    target: str
    symbol_kind: str | None
    in_test: bool
    evidence: SyntaxRelationEvidence
    resolved_target: str | None = None
    resolution_kind: str | None = None
    resolution_module: str | None = None


@dataclass(frozen=True, slots=True)
class SyntaxRelationAnalysis:
    language: str
    status: str
    relations: tuple[SyntaxRelation, ...]


def precise_symbol_language(relative_path: str) -> str | None:
    suffix = Path(relative_path.casefold()).suffix
    if suffix in _PYTHON_SUFFIXES:
        return "python"
    return _POLYGLOT_SUFFIX_LANGUAGE.get(suffix)


def is_precise_symbol_path(relative_path: str) -> bool:
    return precise_symbol_language(relative_path) is not None


def is_test_path(relative_path: str) -> bool:
    lowered = relative_path.casefold()
    parts = lowered.split("/")
    name = parts[-1]
    stem = Path(name).stem
    return (
        any(part in _TEST_DIRECTORY_NAMES for part in parts[:-1])
        or stem.startswith(("test_", "test-"))
        or stem.endswith(("_test", "-test", ".test", "_spec", "-spec", ".spec"))
    )


def analyze_precise_symbol_relations(
    relative_path: str,
    text: str,
    needle: str,
) -> SyntaxRelationAnalysis:
    """Classify current exact occurrences using a precise built-in parser when available."""
    return _analyze_precise_relations(relative_path, text, needle)


def analyze_precise_code_units(relative_path: str, text: str) -> SyntaxRelationAnalysis:
    """Extract every precise named definition from one bounded current source file."""
    return _analyze_precise_relations(relative_path, text, None, collect_references=False)


def analyze_precise_code_structure(relative_path: str, text: str) -> SyntaxRelationAnalysis:
    """Extract every bounded precise definition and supported syntactic reference."""
    return _analyze_precise_relations(relative_path, text, None, collect_references=True)


def _analyze_precise_relations(
    relative_path: str,
    text: str,
    needle: str | None,
    *,
    collect_references: bool = False,
) -> SyntaxRelationAnalysis:
    language = precise_symbol_language(relative_path)
    if language is None:
        return SyntaxRelationAnalysis("unsupported", "unsupported", ())
    if len(text.encode("utf-8")) > MAX_SYMBOL_PARSE_BYTES:
        return SyntaxRelationAnalysis(language, "too_large", ())
    if language == "python":
        return _analyze_python_relations(
            relative_path, text, needle, collect_references=collect_references
        )
    return _analyze_polyglot_relations(
        relative_path, text, needle, language, collect_references=collect_references
    )


def _analyze_python_relations(
    relative_path: str,
    text: str,
    needle: str | None,
    *,
    collect_references: bool,
) -> SyntaxRelationAnalysis:
    try:
        tree = ast.parse(text, filename=relative_path, type_comments=True)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return SyntaxRelationAnalysis("python", "parse_error", ())
    binding_analysis = None if needle is None else _collect_python_import_bindings(tree)
    visitor = _PythonRelationVisitor(
        relative_path,
        text,
        needle,
        collect_references=collect_references,
        binding_analysis=binding_analysis,
    )
    visitor.visit(tree)
    relations = tuple(sorted(visitor.relations, key=_relation_key))
    return SyntaxRelationAnalysis("python", "ok", relations)


def _relation_key(relation: SyntaxRelation) -> tuple[int, int, str, int, int, str, str]:
    priority = {
        "definition": 0,
        "call": 1 if not relation.in_test else 2,
        "inheritance": 3,
        "import": 4,
    }
    return (
        priority.get(relation.kind, 9),
        int(relation.in_test),
        relation.path,
        relation.line,
        relation.column,
        relation.scope or "",
        relation.target,
    )


@dataclass(frozen=True, slots=True)
class _PythonImportBinding:
    target: str
    kind: str
    module: str


@dataclass(frozen=True, slots=True)
class _PythonImportBindingAnalysis:
    imports_by_scope: dict[str, dict[str, tuple[_PythonImportBinding, ...]]]
    bound_names_by_scope: dict[str, frozenset[str]]
    globally_declared_names: frozenset[str]

    def safe_binding(self, scope: str, name: str) -> _PythonImportBinding | None:
        if name in self.bound_names_by_scope.get(scope, frozenset()):
            return None
        bindings = self.imports_by_scope.get(scope, {}).get(name, ())
        if len(bindings) != 1:
            return None
        if scope == "" and name in self.globally_declared_names:
            return None
        return bindings[0]

    def scope_claims_name(self, scope: str, name: str) -> bool:
        return name in self.bound_names_by_scope.get(
            scope, frozenset()
        ) or name in self.imports_by_scope.get(scope, {})


class _PythonImportBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope_stack: list[tuple[str, str]] = [("", "module")]
        self.imports_by_scope: dict[str, dict[str, list[_PythonImportBinding]]] = {"": {}}
        self.bound_names_by_scope: dict[str, set[str]] = {"": set()}
        self.globally_declared_names: set[str] = set()

    def analysis(self) -> _PythonImportBindingAnalysis:
        return _PythonImportBindingAnalysis(
            imports_by_scope={
                scope: {name: tuple(bindings) for name, bindings in imports.items()}
                for scope, imports in self.imports_by_scope.items()
            },
            bound_names_by_scope={
                scope: frozenset(names) for scope, names in self.bound_names_by_scope.items()
            },
            globally_declared_names=frozenset(self.globally_declared_names),
        )

    @property
    def _scope(self) -> str:
        return self.scope_stack[-1][0]

    def _bind(self, name: str | None) -> None:
        if name:
            self.bound_names_by_scope.setdefault(self._scope, set()).add(name)

    def _add_import(self, local_name: str, target: str, kind: str, *, module: str) -> None:
        self.imports_by_scope.setdefault(self._scope, {}).setdefault(local_name, []).append(
            _PythonImportBinding(target=target, kind=kind, module=module)
        )

    def _push_scope(self, name: str, kind: str) -> None:
        parent = self._scope
        qualified = name if not parent else f"{parent}.{name}"
        self.scope_stack.append((qualified, kind))
        self.imports_by_scope.setdefault(qualified, {})
        self.bound_names_by_scope.setdefault(qualified, set())

    def _pop_scope(self) -> None:
        self.scope_stack.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                local = alias.asname
                target = alias.name
            else:
                local = alias.name.split(".", 1)[0]
                target = local
            self._add_import(local, target, "python_import_binding", module=target)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        prefix = "." * node.level
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            target = f"{prefix}{module}.{alias.name}" if module else f"{prefix}{alias.name}"
            self._add_import(
                local,
                target,
                "python_from_import_binding",
                module=f"{prefix}{module}",
            )

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self._bind(name)
            self.globally_declared_names.add(name)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self._bind(name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._bind(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        self._bind(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        self._bind(node.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._bind(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)
        self._push_scope(node.name, "function")
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            self._bind(argument.arg)
        if node.args.vararg is not None:
            self._bind(node.args.vararg.arg)
        if node.args.kwarg is not None:
            self._bind(node.args.kwarg.arg)
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword_node in node.keywords:
            self.visit(keyword_node.value)
        self._push_scope(node.name, "class")
        for statement in node.body:
            self.visit(statement)
        self._pop_scope()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    def _visit_comprehension_scope(self, node: ast.AST) -> None:
        for descendant in ast.walk(node):
            if isinstance(descendant, ast.NamedExpr):
                for name_node in _assignment_names(descendant.target):
                    self._bind(name_node.id)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension_scope(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension_scope(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension_scope(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension_scope(node)


def _collect_python_import_bindings(tree: ast.AST) -> _PythonImportBindingAnalysis:
    collector = _PythonImportBindingCollector()
    collector.visit(tree)
    return collector.analysis()


class _PythonRelationVisitor(ast.NodeVisitor):
    def __init__(
        self,
        relative_path: str,
        text: str,
        needle: str | None,
        *,
        collect_references: bool,
        binding_analysis: _PythonImportBindingAnalysis | None,
    ) -> None:
        self.relative_path = relative_path
        self.lines = text.splitlines()
        self.needle = needle
        self.collect_references = collect_references
        self.binding_analysis = binding_analysis
        self.binding_resolution_suspended = 0
        self.needle_leaf = "" if needle is None else needle.rsplit(".", 1)[-1]
        self.scopes: list[tuple[str, str]] = []
        self.relations: list[SyntaxRelation] = []
        self.in_test = is_test_path(relative_path)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        if self._definition_matches(node.name, qualified):
            self._add_definition(node, node.name, "class", qualified)
        for base in node.bases:
            target = _dotted_expression(base)
            if target is not None and self._target_matches(target):
                self._add_reference("inheritance", base, target)
        self.scopes.append((node.name, "class"))
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = _dotted_expression(node.func)
        if target is not None:
            resolved_target, resolution_kind, resolution_module = self._resolved_import_call_target(
                target
            )
            if self._target_matches(target) or (
                resolved_target is not None and self._target_matches(resolved_target)
            ):
                self._add_reference(
                    "call",
                    node.func,
                    target,
                    resolved_target=resolved_target,
                    resolution_kind=resolution_kind,
                    resolution_module=resolution_module,
                )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            if self._target_matches(alias.name) or self._target_matches(local):
                self._add_reference("import", node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        relative_prefix = "." * node.level
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            if module:
                target = f"{relative_prefix}{module}.{alias.name}"
            else:
                target = f"{relative_prefix}{alias.name}"
            if (
                self._target_matches(target)
                or self._target_matches(alias.name)
                or self._target_matches(local)
            ):
                self._add_reference("import", node, target)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.binding_resolution_suspended += 1
        self.generic_visit(node)
        self.binding_resolution_suspended -= 1

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_suspended_binding_scope(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_suspended_binding_scope(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_suspended_binding_scope(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_suspended_binding_scope(node)

    def _visit_suspended_binding_scope(self, node: ast.AST) -> None:
        self.binding_resolution_suspended += 1
        self.generic_visit(node)
        self.binding_resolution_suspended -= 1

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self._inside_callable():
            for target in node.targets:
                for name_node in _assignment_names(target):
                    qualified = self._qualified(name_node.id)
                    if self._definition_matches(name_node.id, qualified):
                        self._add_definition(name_node, name_node.id, "variable", qualified)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self._inside_callable():
            for name_node in _assignment_names(node.target):
                qualified = self._qualified(name_node.id)
                if self._definition_matches(name_node.id, qualified):
                    self._add_definition(name_node, name_node.id, "variable", qualified)
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qualified = self._qualified(node.name)
        parent_kind = self.scopes[-1][1] if self.scopes else None
        symbol_kind = "method" if parent_kind == "class" else "function"
        if self._definition_matches(node.name, qualified):
            self._add_definition(node, node.name, symbol_kind, qualified)
        self.scopes.append((node.name, symbol_kind))
        self.generic_visit(node)
        self.scopes.pop()

    def _add_definition(
        self,
        node: ast.AST,
        name: str,
        symbol_kind: str,
        qualified: str,
    ) -> None:
        start_line = _line_number(node)
        end_line = _end_line_number(node)
        column = self._identifier_column(
            start_line,
            name,
            max(0, _column_number(node) - 1),
        )
        evidence = _definition_evidence(self.lines, start_line, end_line)
        self.relations.append(
            SyntaxRelation(
                kind="definition",
                path=self.relative_path,
                line=start_line,
                column=column,
                scope=self._parent_scope(),
                target=qualified,
                symbol_kind=symbol_kind,
                in_test=self.in_test,
                evidence=evidence,
            )
        )

    def _add_reference(
        self,
        kind: str,
        node: ast.AST,
        target: str,
        *,
        resolved_target: str | None = None,
        resolution_kind: str | None = None,
        resolution_module: str | None = None,
    ) -> None:
        line = _line_number(node)
        evidence = _reference_evidence(self.lines, line)
        self.relations.append(
            SyntaxRelation(
                kind=kind,
                path=self.relative_path,
                line=line,
                column=self._ast_column(line, max(0, _column_number(node) - 1)),
                scope=self._current_scope(),
                target=target,
                symbol_kind=None,
                in_test=self.in_test,
                evidence=evidence,
                resolved_target=resolved_target,
                resolution_kind=resolution_kind,
                resolution_module=resolution_module,
            )
        )

    def _resolved_import_call_target(
        self, target: str
    ) -> tuple[str | None, str | None, str | None]:
        analysis = self.binding_analysis
        if analysis is None or self.binding_resolution_suspended:
            return None, None, None
        function_scopes = [
            ".".join(scope_name for scope_name, _scope_kind in self.scopes[: index + 1])
            for index, (_scope_name, scope_kind) in enumerate(self.scopes)
            if scope_kind in {"function", "method"}
        ]
        if not function_scopes and any(scope_kind == "class" for _name, scope_kind in self.scopes):
            return None, None, None

        root, separator, remainder = target.partition(".")
        if function_scopes:
            current_scope = function_scopes[-1]
            binding = analysis.safe_binding(current_scope, root)
            if binding is not None:
                return self._apply_import_binding(binding, remainder if separator else "")
            if analysis.scope_claims_name(current_scope, root):
                return None, None, None
            for outer_scope in reversed(function_scopes[:-1]):
                if analysis.scope_claims_name(outer_scope, root):
                    return None, None, None

        binding = analysis.safe_binding("", root)
        if binding is None:
            return None, None, None
        return self._apply_import_binding(binding, remainder if separator else "")

    @staticmethod
    def _apply_import_binding(
        binding: _PythonImportBinding,
        remainder: str,
    ) -> tuple[str | None, str | None, str | None]:
        if binding.kind == "python_from_import_binding":
            if remainder:
                return None, None, None
            return binding.target, binding.kind, binding.module
        resolved = binding.target if not remainder else f"{binding.target}.{remainder}"
        return resolved, binding.kind, binding.module

    def _definition_matches(self, name: str, qualified: str) -> bool:
        if self.needle is None:
            return True
        if "." in self.needle:
            return qualified == self.needle or qualified.endswith(f".{self.needle}")
        return name == self.needle

    def _target_matches(self, target: str) -> bool:
        if self.needle is None:
            return self.collect_references
        if "." in self.needle:
            return target == self.needle or target.endswith(f".{self.needle}")
        return target.rsplit(".", 1)[-1] == self.needle_leaf

    def _qualified(self, name: str) -> str:
        values = [scope_name for scope_name, _kind in self.scopes]
        values.append(name)
        return ".".join(values)

    def _parent_scope(self) -> str | None:
        return self._current_scope()

    def _current_scope(self) -> str | None:
        if not self.scopes:
            return None
        return ".".join(name for name, _kind in self.scopes)

    def _inside_callable(self) -> bool:
        return any(kind in {"function", "method"} for _name, kind in self.scopes)

    def _identifier_column(self, line_number: int, identifier: str, byte_fallback: int) -> int:
        fallback = self._ast_column(line_number, byte_fallback)
        if not 1 <= line_number <= len(self.lines):
            return fallback
        line = self.lines[line_number - 1]
        start = max(0, fallback - 1)
        index = line.find(identifier, start)
        return fallback if index < 0 else index + 1

    def _ast_column(self, line_number: int, byte_column: int) -> int:
        if not 1 <= line_number <= len(self.lines):
            return max(1, byte_column + 1)
        line = self.lines[line_number - 1]
        payload = line.encode("utf-8")
        prefix = payload[: max(0, byte_column)]
        try:
            return len(prefix.decode("utf-8")) + 1
        except UnicodeDecodeError:
            return max(1, byte_column + 1)


_JS_CLASS_KINDS = frozenset({"class_declaration", "interface_declaration"})
_JS_FUNCTION_KINDS = frozenset(
    {"function_declaration", "generator_function_declaration", "function_signature"}
)
_JS_METHOD_KINDS = frozenset({"method_definition", "method_signature", "abstract_method_signature"})
_JS_FUNCTION_VALUE_KINDS = frozenset(
    {"arrow_function", "function_expression", "generator_function"}
)
_JS_NAMED_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_RUST_NAMED_TYPE_KINDS = {
    "struct_item": "struct",
    "enum_item": "enum",
    "trait_item": "interface",
    "type_item": "type",
    "const_item": "variable",
    "static_item": "variable",
}
_JAVA_NAMED_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "interface",
}
_IDENTIFIER_KINDS = frozenset(
    {
        "identifier",
        "property_identifier",
        "private_property_identifier",
        "type_identifier",
        "field_identifier",
        "package_identifier",
    }
)


def _analyze_polyglot_relations(
    relative_path: str,
    text: str,
    needle: str | None,
    language: str,
    *,
    collect_references: bool,
) -> SyntaxRelationAnalysis:
    try:
        root = SgRoot(text, language).root()
    except (RuntimeError, TypeError, ValueError, MemoryError):
        return SyntaxRelationAnalysis(language, "parse_error", ())
    if root.find(kind="ERROR") is not None:
        return SyntaxRelationAnalysis(language, "parse_error", ())
    collector = _PolyglotRelationCollector(
        relative_path, text, needle, language, collect_references=collect_references
    )
    collector.collect(root)
    return SyntaxRelationAnalysis(
        language,
        "ok",
        tuple(sorted(collector.relations, key=_relation_key)),
    )


class _PolyglotRelationCollector:
    def __init__(
        self,
        relative_path: str,
        text: str,
        needle: str | None,
        language: str,
        *,
        collect_references: bool,
    ) -> None:
        self.relative_path = relative_path
        self.lines = text.splitlines()
        self.needle = needle
        self.collect_references = collect_references
        self.needle_leaf = "" if needle is None else _target_leaf(needle)
        self.language = language
        self.in_test = is_test_path(relative_path)
        self.relations: list[SyntaxRelation] = []

    def collect(self, root: SgNode) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            self._definition(node)
            self._call(node)
            self._import(node)
            self._inheritance(node)
            stack.extend(reversed(node.children()))

    def _definition(self, node: SgNode) -> None:
        kind = node.kind()
        symbol_kind: str | None = None
        name_node: SgNode | None = None
        qualified_override: str | None = None

        if self.language in {"javascript", "typescript", "tsx"}:
            if kind in _JS_NAMED_TYPE_KINDS:
                symbol_kind = _JS_NAMED_TYPE_KINDS[kind]
                name_node = node.field("name")
            elif kind in _JS_FUNCTION_KINDS:
                symbol_kind = "function"
                name_node = node.field("name")
            elif kind in _JS_METHOD_KINDS:
                symbol_kind = "method"
                name_node = node.field("name")
            elif kind == "variable_declarator":
                name_node = node.field("name")
                value = node.field("value")
                if name_node is None or name_node.kind() not in _IDENTIFIER_KINDS:
                    return
                if value is not None and value.kind() in _JS_FUNCTION_VALUE_KINDS:
                    symbol_kind = "function"
                elif not self._inside_callable(node):
                    symbol_kind = "variable"
                else:
                    return
        elif self.language == "go":
            if kind == "function_declaration":
                symbol_kind = "function"
                name_node = node.field("name")
            elif kind == "method_declaration":
                symbol_kind = "method"
                name_node = node.field("name")
                receiver = _go_receiver_type(node.field("receiver"))
                if receiver is not None and name_node is not None:
                    qualified_override = f"{receiver}.{name_node.text()}"
            elif kind == "type_spec":
                symbol_kind = "type"
                name_node = node.field("name")
        elif self.language == "rust":
            if kind in _RUST_NAMED_TYPE_KINDS:
                symbol_kind = _RUST_NAMED_TYPE_KINDS[kind]
                name_node = node.field("name")
            elif kind in {"function_item", "function_signature_item"}:
                symbol_kind = (
                    "method"
                    if any(
                        parent.kind() in {"impl_item", "trait_item"} for parent in node.ancestors()
                    )
                    else "function"
                )
                name_node = node.field("name")
        elif self.language == "java":
            if kind in _JAVA_NAMED_TYPE_KINDS:
                symbol_kind = _JAVA_NAMED_TYPE_KINDS[kind]
                name_node = node.field("name")
            elif kind == "method_declaration":
                symbol_kind = "method"
                name_node = node.field("name")
            elif kind == "constructor_declaration":
                symbol_kind = "constructor"
                name_node = node.field("name")
            elif kind == "variable_declarator" and _is_java_field_declarator(node):
                symbol_kind = "variable"
                name_node = node.field("name")

        if symbol_kind is None or name_node is None:
            return
        name = name_node.text()
        scope = self._scope(node)
        if qualified_override is not None and self.language == "go" and "." in qualified_override:
            scope = qualified_override.rsplit(".", 1)[0]
        qualified = qualified_override or (f"{scope}.{name}" if scope else name)
        if not self._definition_matches(name, qualified):
            return
        self._add_definition(name_node, node, symbol_kind, qualified, scope)

    def _call(self, node: SgNode) -> None:
        kind = node.kind()
        target_node: SgNode | None = None
        if self.language in {"javascript", "typescript", "tsx", "go", "rust"}:
            if kind == "call_expression":
                target_node = node.field("function")
            elif self.language in {"javascript", "typescript", "tsx"} and kind == "new_expression":
                target_node = node.field("constructor")
        elif self.language == "java":
            if kind == "method_invocation":
                name = node.field("name")
                obj = node.field("object")
                if name is not None:
                    target = name.text() if obj is None else f"{obj.text()}.{name.text()}"
                    if self._target_matches(target):
                        self._add_reference("call", name if obj is None else obj, target)
                return
            if kind == "object_creation_expression":
                target_node = node.field("type")
        if target_node is None:
            return
        call_target = _call_target_text(target_node, self.language)
        if call_target is not None and self._target_matches(call_target):
            self._add_reference("call", target_node, call_target)

    def _import(self, node: SgNode) -> None:
        kind = node.kind()
        if self.language in {"javascript", "typescript", "tsx"} and kind == "import_statement":
            self._js_import(node)
        elif self.language == "go" and kind == "import_spec":
            path_node = node.field("path")
            if path_node is None:
                return
            module = _strip_string_literal(path_node.text())
            if module is None:
                return
            alias_node = node.field("name")
            alias = None if alias_node is None else alias_node.text()
            if self._target_matches(module) or (alias is not None and self._target_matches(alias)):
                self._add_reference("import", alias_node or path_node, module)
        elif self.language == "rust" and kind == "use_declaration":
            argument = node.field("argument")
            if argument is not None:
                for local_name, target, location in _rust_use_entries(argument):
                    if self._target_matches(target) or self._target_matches(local_name):
                        self._add_reference("import", location, target)
        elif self.language == "java" and kind == "import_declaration":
            candidate = next(
                (
                    child
                    for child in node.children()
                    if child.kind() in {"scoped_identifier", "identifier"}
                ),
                None,
            )
            if candidate is not None and self._target_matches(candidate.text()):
                self._add_reference("import", candidate, candidate.text())

    def _js_import(self, node: SgNode) -> None:
        source = node.field("source")
        module = None if source is None else _strip_string_literal(source.text())
        if module is None:
            return
        clause = next((child for child in node.children() if child.kind() == "import_clause"), None)
        if clause is None:
            return
        for child in clause.children():
            if child.kind() == "identifier":
                if self._target_matches(child.text()):
                    self._add_reference("import", child, module)
            elif child.kind() == "namespace_import":
                namespace_local = next(
                    (item for item in child.children() if item.kind() == "identifier"), None
                )
                if namespace_local is not None and self._target_matches(namespace_local.text()):
                    self._add_reference("import", namespace_local, f"{module}.*")
            elif child.kind() == "named_imports":
                for specifier in child.children():
                    if specifier.kind() != "import_specifier":
                        continue
                    name_node = specifier.field("name")
                    alias_node = specifier.field("alias")
                    if name_node is None:
                        continue
                    imported = name_node.text()
                    local_name = imported if alias_node is None else alias_node.text()
                    target = f"{module}.{imported}"
                    if (
                        self._target_matches(target)
                        or self._target_matches(imported)
                        or self._target_matches(local_name)
                    ):
                        self._add_reference("import", alias_node or name_node, target)

    def _inheritance(self, node: SgNode) -> None:
        candidates: list[SgNode] = []
        kind = node.kind()
        if self.language in {"javascript", "typescript", "tsx"}:
            if kind == "class_heritage":
                clauses = [
                    child
                    for child in node.children()
                    if child.kind() in {"extends_clause", "implements_clause"}
                ]
                if not clauses:
                    candidates = _named_target_children(node)
            elif kind == "extends_clause":
                value = node.field("value")
                candidates = [value] if value is not None else _named_target_children(node)
            elif kind in {"implements_clause", "extends_type_clause"}:
                value = node.field("type")
                candidates = [value] if value is not None else _named_target_children(node)
        elif self.language == "java":
            if kind in {"superclass", "super_interfaces"}:
                candidates = _named_target_descendants(node)
        elif self.language == "rust" and kind == "impl_item":
            implementation_type = node.field("type")
            body = node.field("body")
            candidates = [
                child
                for child in node.children()
                if child.is_named()
                and not _same_sg_node(child, implementation_type)
                and not _same_sg_node(child, body)
                and child.kind() in {"type_identifier", "scoped_type_identifier", "generic_type"}
            ]
        for candidate in candidates:
            target = _simple_target_text(candidate, self.language)
            if target is not None and self._target_matches(target):
                self._add_reference("inheritance", candidate, target)

    def _add_definition(
        self,
        name_node: SgNode,
        definition_node: SgNode,
        symbol_kind: str,
        target: str,
        scope: str | None,
    ) -> None:
        start = definition_node.range().start.line + 1
        end = max(start, definition_node.range().end.line + 1)
        self.relations.append(
            SyntaxRelation(
                kind="definition",
                path=self.relative_path,
                line=name_node.range().start.line + 1,
                column=self._node_column(name_node),
                scope=scope,
                target=target,
                symbol_kind=symbol_kind,
                in_test=self.in_test,
                evidence=_definition_evidence(self.lines, start, end),
            )
        )

    def _add_reference(self, kind: str, node: SgNode, target: str) -> None:
        line = node.range().start.line + 1
        self.relations.append(
            SyntaxRelation(
                kind=kind,
                path=self.relative_path,
                line=line,
                column=self._node_column(node),
                scope=self._scope(node),
                target=target,
                symbol_kind=None,
                in_test=self.in_test,
                evidence=_reference_evidence(self.lines, line),
            )
        )

    def _scope(self, node: SgNode) -> str | None:
        components: list[str] = []
        for ancestor in reversed(node.ancestors()):
            components.extend(_scope_components(ancestor, self.language))
        return ".".join(_dedupe_adjacent(components)) or None

    def _inside_callable(self, node: SgNode) -> bool:
        callable_kinds = {
            "function_declaration",
            "generator_function_declaration",
            "function_expression",
            "arrow_function",
            "method_definition",
            "method_signature",
        }
        return any(parent.kind() in callable_kinds for parent in node.ancestors())

    def _node_column(self, node: SgNode) -> int:
        # ast-grep-py exposes SgRange columns as Unicode character offsets, not
        # raw Tree-sitter byte columns. Model-facing locations are 1-based chars.
        return node.range().start.column + 1

    def _definition_matches(self, name: str, qualified: str) -> bool:
        if self.needle is None:
            return True
        if "." in self.needle:
            return qualified == self.needle or qualified.endswith(f".{self.needle}")
        return name == self.needle

    def _target_matches(self, target: str) -> bool:
        if self.needle is None:
            return self.collect_references
        if "." in self.needle:
            normalized = target.replace("::", ".")
            return normalized == self.needle or normalized.endswith(f".{self.needle}")
        return _target_leaf(target) == self.needle_leaf


def _is_java_field_declarator(node: SgNode) -> bool:
    parent = node.parent()
    return parent is not None and parent.kind() == "field_declaration"


def _rust_use_entries(
    node: SgNode,
    prefix: str | None = None,
) -> list[tuple[str, str, SgNode]]:
    kind = node.kind()
    if kind == "use_as_clause":
        path = node.field("path")
        alias = node.field("alias")
        if path is None or alias is None:
            return []
        target = _join_rust_use_path(prefix, path.text())
        return [(alias.text(), target, alias)]
    if kind == "scoped_use_list":
        path = node.field("path")
        scoped_prefix = prefix
        if path is not None:
            scoped_prefix = _join_rust_use_path(prefix, path.text())
        use_list = next((child for child in node.children() if child.kind() == "use_list"), None)
        if use_list is None:
            return []
        return _rust_use_entries(use_list, scoped_prefix)
    if kind == "use_list":
        entries: list[tuple[str, str, SgNode]] = []
        for child in node.children():
            if child.is_named():
                entries.extend(_rust_use_entries(child, prefix))
        return entries
    if kind in {"identifier", "scoped_identifier", "self", "super", "crate"}:
        target = _join_rust_use_path(prefix, node.text())
        return [(_target_leaf(target), target, node)]
    return []


def _join_rust_use_path(prefix: str | None, value: str) -> str:
    value = value.strip()
    if prefix is None or not prefix:
        return value
    if not value:
        return prefix
    return f"{prefix}::{value}"


def _scope_components(node: SgNode, language: str) -> list[str]:
    kind = node.kind()
    if language in {"javascript", "typescript", "tsx"}:
        if kind in _JS_CLASS_KINDS | _JS_FUNCTION_KINDS | _JS_METHOD_KINDS:
            name = node.field("name")
            return [] if name is None else [name.text()]
        if kind == "variable_declarator":
            value = node.field("value")
            name = node.field("name")
            if value is not None and value.kind() in _JS_FUNCTION_VALUE_KINDS and name is not None:
                return [name.text()]
    elif language == "go":
        if kind == "function_declaration":
            name = node.field("name")
            return [] if name is None else [name.text()]
        if kind == "method_declaration":
            receiver = _go_receiver_type(node.field("receiver"))
            name = node.field("name")
            return [value for value in (receiver, None if name is None else name.text()) if value]
    elif language == "rust":
        if kind == "trait_item":
            name = node.field("name")
            return [] if name is None else [name.text()]
        if kind == "impl_item":
            impl_type = node.field("type")
            return [] if impl_type is None else [impl_type.text()]
        if kind in {"function_item", "function_signature_item"}:
            name = node.field("name")
            return [] if name is None else [name.text()]
    elif language == "java":
        if kind in _JAVA_NAMED_TYPE_KINDS or kind in {
            "method_declaration",
            "constructor_declaration",
        }:
            name = node.field("name")
            return [] if name is None else [name.text()]
    return []


def _same_sg_node(left: SgNode, right: SgNode | None) -> bool:
    if right is None or left.kind() != right.kind():
        return False
    left_range = left.range()
    right_range = right.range()
    return (
        left_range.start.index == right_range.start.index
        and left_range.end.index == right_range.end.index
    )


def _go_receiver_type(node: SgNode | None) -> str | None:
    if node is None:
        return None
    candidate = node.find(kind="type_identifier")
    if candidate is not None:
        return candidate.text()
    pointer = node.find(kind="pointer_type")
    if pointer is not None:
        name = pointer.find(kind="type_identifier")
        if name is not None:
            return name.text()
    return None


def _call_target_text(node: SgNode, language: str) -> str | None:
    if language in {"javascript", "typescript", "tsx"} and node.kind() == "member_expression":
        obj = node.field("object")
        prop = node.field("property")
        if prop is None or prop.kind() not in _IDENTIFIER_KINDS:
            return None
        property_name = prop.text()
        if obj is None:
            return property_name
        if obj.kind() == "new_expression":
            constructor = obj.field("constructor")
            base = None if constructor is None else _simple_target_text(constructor, language)
        else:
            base = _call_target_text(obj, language)
        return property_name if base is None else f"{base}.{property_name}"
    return _simple_target_text(node, language)


def _simple_target_text(node: SgNode, language: str) -> str | None:
    if node.kind() == "generic_type":
        candidate = node.field("type")
        if candidate is None:
            candidate = node.find(kind="type_identifier") or node.find(kind="identifier")
        if candidate is not None:
            return _simple_target_text(candidate, language)
    text = node.text().strip()
    if not text or any(character.isspace() for character in text):
        return None
    if language == "rust":
        pattern = r"[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*"
    else:
        pattern = r"(?:this\.)?[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
    return text if re.fullmatch(pattern, text) is not None else None


def _target_leaf(target: str) -> str:
    return target.replace("::", ".").rsplit(".", 1)[-1]


def _strip_string_literal(value: str) -> str | None:
    value = value.strip()
    if len(value) < 2 or value[0] not in {"'", '"', "`"} or value[-1] != value[0]:
        return None
    return value[1:-1]


def _named_target_children(node: SgNode) -> list[SgNode]:
    return [
        child
        for child in node.children()
        if child.is_named() and child.kind() not in {"type_arguments"}
    ]


def _named_target_descendants(node: SgNode) -> list[SgNode]:
    result: list[SgNode] = []
    stack = list(node.children())
    while stack:
        child = stack.pop()
        if child.kind() in {"type_identifier", "scoped_type_identifier"}:
            result.append(child)
            continue
        stack.extend(child.children())
    return result


def _dedupe_adjacent(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


def _dotted_expression(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_expression(node.value)
        return node.attr if parent is None else f"{parent}.{node.attr}"
    return None


def _assignment_names(node: ast.AST) -> tuple[ast.Name, ...]:
    if isinstance(node, ast.Name):
        return (node,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for element in node.elts for name in _assignment_names(element))
    return ()


def _line_number(node: ast.AST) -> int:
    value = getattr(node, "lineno", 1)
    return value if isinstance(value, int) and value > 0 else 1


def _end_line_number(node: ast.AST) -> int:
    start = _line_number(node)
    value = getattr(node, "end_lineno", start)
    return value if isinstance(value, int) and value >= start else start


def _column_number(node: ast.AST) -> int:
    value = getattr(node, "col_offset", 0)
    return value + 1 if isinstance(value, int) and value >= 0 else 1


def _definition_evidence(
    lines: list[str],
    start_line: int,
    end_line: int,
) -> SyntaxRelationEvidence:
    bounded_end = min(end_line, start_line + MAX_SYMBOL_RELATION_EVIDENCE_LINES - 1)
    return _evidence_from_lines(
        lines,
        start_line,
        bounded_end,
        focus_line=start_line,
        maximum_bytes=MAX_SYMBOL_DEFINITION_EVIDENCE_BYTES,
        originally_truncated=bounded_end < end_line,
    )


def _reference_evidence(lines: list[str], line: int) -> SyntaxRelationEvidence:
    start = max(1, line - 2)
    end = min(len(lines), line + 2)
    return _evidence_from_lines(
        lines,
        start,
        end,
        focus_line=line,
        maximum_bytes=MAX_SYMBOL_REFERENCE_EVIDENCE_BYTES,
        originally_truncated=start > 1 or end < len(lines),
    )


def _evidence_from_lines(
    lines: list[str],
    start_line: int,
    end_line: int,
    *,
    focus_line: int,
    maximum_bytes: int,
    originally_truncated: bool,
) -> SyntaxRelationEvidence:
    bounded_start = start_line
    bounded_end = end_line
    snippet = "\n".join(lines[bounded_start - 1 : bounded_end])
    truncated = originally_truncated
    while bounded_start < bounded_end and len(snippet.encode("utf-8")) > maximum_bytes:
        if bounded_end > focus_line:
            bounded_end -= 1
        elif bounded_start < focus_line:
            bounded_start += 1
        else:
            break
        snippet = "\n".join(lines[bounded_start - 1 : bounded_end])
        truncated = True
    if len(snippet.encode("utf-8")) > maximum_bytes:
        snippet = _truncate_utf8(snippet, maximum_bytes)
        truncated = True
    return SyntaxRelationEvidence(
        start_line=bounded_start,
        end_line=bounded_end,
        snippet=snippet,
        truncated=truncated,
    )


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    payload = value.encode("utf-8")
    if len(payload) <= maximum_bytes:
        return value
    truncated = payload[: max(0, maximum_bytes - 3)]
    while True:
        try:
            return truncated.decode("utf-8") + "..."
        except UnicodeDecodeError:
            truncated = truncated[:-1]
