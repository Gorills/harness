from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

MAX_SYMBOL_PARSE_BYTES = 1024 * 1024
MAX_SYMBOL_RELATION_EVIDENCE_LINES = 32
MAX_SYMBOL_DEFINITION_EVIDENCE_BYTES = 2048
MAX_SYMBOL_REFERENCE_EVIDENCE_BYTES = 1024
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
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


@dataclass(frozen=True, slots=True)
class SyntaxRelationAnalysis:
    language: str
    status: str
    relations: tuple[SyntaxRelation, ...]


def is_precise_symbol_path(relative_path: str) -> bool:
    return Path(relative_path.casefold()).suffix in _PYTHON_SUFFIXES


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
    if not is_precise_symbol_path(relative_path):
        return SyntaxRelationAnalysis("unsupported", "unsupported", ())
    if len(text.encode("utf-8")) > MAX_SYMBOL_PARSE_BYTES:
        return SyntaxRelationAnalysis("python", "too_large", ())
    try:
        tree = ast.parse(text, filename=relative_path, type_comments=True)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return SyntaxRelationAnalysis("python", "parse_error", ())
    visitor = _PythonRelationVisitor(relative_path, text, needle)
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


class _PythonRelationVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str, text: str, needle: str) -> None:
        self.relative_path = relative_path
        self.lines = text.splitlines()
        self.needle = needle
        self.needle_leaf = needle.rsplit(".", 1)[-1]
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
        if target is not None and self._target_matches(target):
            self._add_reference("call", node.func, target)
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

    def _add_reference(self, kind: str, node: ast.AST, target: str) -> None:
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
            )
        )

    def _definition_matches(self, name: str, qualified: str) -> bool:
        if "." in self.needle:
            return qualified == self.needle or qualified.endswith(f".{self.needle}")
        return name == self.needle

    def _target_matches(self, target: str) -> bool:
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
