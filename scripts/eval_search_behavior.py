"""Classify Codex exec JSONL for Harness search vs native rg/grep follow-up.

Acceptance evidence only. Does not talk to the daemon, add MCP fields, or log
agent commands into Harness state.

Usage (no model)::

    scripts/dev python scripts/eval_search_behavior.py events.jsonl
    scripts/dev python scripts/eval_search_behavior.py --output report.json < events.jsonl

Codex ``exec --json`` mapping (see ``scripts/codex_exec_jsonl.py``):

- ``item.completed`` / ``item.type=mcp_tool_call``: ``server``|``server_name``,
  ``tool``|``name``, ``arguments`` (object or JSON string), ``result.structured_content``
  or JSON text in ``result.content[].text``.
- ``item.type=command_execution``: Codex ``command`` is a **string**. A list argv
  is accepted as a documented synthetic shape for tests. Missing ``command`` is
  ``unknown``, not a guessed class.
- ``item.type=file_change`` is a patch (``changes[].path`` / ``kind``), not a
  file-read. Classified ``unrelated_command``. Codex does not emit a distinct
  file-read item; reads are ``cat``/``head``/``tail``/``sed`` command executions.

Supported command forms: ``rg``, ``grep``, ``sed``, ``cat``, ``head``, ``tail``,
optionally wrapped once in ``bash|sh -c|-lc``. Pipelines and unknown syntax fail
closed to ``unknown``.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from codex_exec_jsonl import (
    discovery_actions_before_task_start,
    iter_completed_items,
    mcp_server_and_tool,
    project_actions_before_harness_status,
)

SCHEMA_VERSION = 1
HARNESS_SERVER = "harness"
SEARCH_TOOLS = frozenset({"rg", "grep"})
READ_TOOLS = frozenset({"cat", "head", "tail", "sed"})
_SHELLS = frozenset({"bash", "sh", "dash", "zsh"})
_META_TOKENS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "$(", "`"})
# MCP hit kind is "doc"; "docs" is search scope (accepted as alias).
_CODE_DOC_KINDS = frozenset({"code", "doc", "docs"})
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "does",
        "for",
        "from",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "where",
        "why",
        "with",
    }
)
_SECRET_RE = re.compile(
    r"(?i)(?:sk-|rk-|xox[baprs]-)[A-Za-z0-9_-]{8,}"
    r"|(?:(?:api[_-]?key|access[_-]?token|secret|password|passwd|bearer"
    r"|authorization)['\"]?\s*[=:]\s*['\"]?)[^\s'\"&]+"
)
_ENV_SECRET_RE = re.compile(r"(?i)^(?:[\w]*?(?:api[_-]?key|token|secret|password|passwd))=.*")
_RG_VALUE_FLAGS = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-d",
        "-e",
        "-E",
        "-f",
        "-g",
        "-j",
        "-m",
        "-r",
        "-T",
        "-t",
        "--after-context",
        "--before-context",
        "--context",
        "--encoding",
        "--file",
        "--glob",
        "--ignore-file",
        "--max-count",
        "--max-depth",
        "--max-filesize",
        "--pre",
        "--pre-glob",
        "--regex",
        "--regexp",
        "--replace",
        "--sort",
        "--sortr",
        "--threads",
        "--type",
        "--type-not",
    }
)
_GREP_VALUE_FLAGS = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-D",
        "-e",
        "-f",
        "-m",
        "--context",
        "--exclude",
        "--exclude-dir",
        "--file",
        "--include",
        "--max-count",
        "--regexp",
    }
)
_READ_VALUE_FLAGS = frozenset(
    {
        "-A",
        "-B",
        "-C",
        "-c",
        "-e",
        "-f",
        "-n",
        "--bytes",
        "--expression",
        "--file",
        "--lines",
    }
)
SANITIZED_METRIC_KEYS = (
    "search_hit_quality",
    "native_followup",
    "duplicate_broad_search",
    "status_first",
    "task_before_diagnosis",
    "search_before_broad_native",
    "good_hit_to_targeted_read",
    "good_hit_to_duplicate_broad_search",
    "zero_hit_to_native_fallback",
)


class SearchHitQuality(StrEnum):
    """Quality of the first Harness project_search result set."""

    STRONG = "strong"
    ZERO = "zero"
    INSUFFICIENT = "insufficient"


class CommandClass(StrEnum):
    """Native follow-up class. Fail closed to unknown rather than guess."""

    TARGETED_READ = "targeted_read"
    TARGETED_SEARCH = "targeted_search"
    BROAD_SEARCH = "broad_search"
    UNRELATED_COMMAND = "unrelated_command"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NativeCommandEvidence:
    command_class: CommandClass
    argv: tuple[str, ...]
    search_pattern: str | None


@dataclass(frozen=True, slots=True)
class SearchBehaviorReport:
    search_hit_quality: SearchHitQuality
    native_followup: CommandClass
    duplicate_broad_search: bool
    status_first: bool
    task_before_diagnosis: bool
    search_before_broad_native: bool
    good_hit_to_targeted_read: bool
    good_hit_to_duplicate_broad_search: bool
    zero_hit_to_native_fallback: bool
    project_search_query: str | None
    candidate_paths: tuple[str, ...]
    native_commands: tuple[NativeCommandEvidence, ...]

    def to_json_dict(self) -> dict[str, Any]:
        """Sanitized JSON object: classifier fields, metrics, path-only evidence."""
        return {
            "schema_version": SCHEMA_VERSION,
            "search_hit_quality": self.search_hit_quality.value,
            "native_followup": self.native_followup.value,
            "duplicate_broad_search": self.duplicate_broad_search,
            "metrics": {
                "status_first": self.status_first,
                "task_before_diagnosis": self.task_before_diagnosis,
                "search_before_broad_native": self.search_before_broad_native,
                "good_hit_to_targeted_read": self.good_hit_to_targeted_read,
                "good_hit_to_duplicate_broad_search": self.good_hit_to_duplicate_broad_search,
                "zero_hit_to_native_fallback": self.zero_hit_to_native_fallback,
            },
            "evidence": {
                "project_search_query": _redact_text(self.project_search_query),
                "candidate_paths": [_redact_text(path) or path for path in self.candidate_paths],
                "native_commands": [
                    {
                        "command_class": command.command_class.value,
                        "argv": [_redact_text(part) or "" for part in command.argv],
                    }
                    for command in self.native_commands
                ],
            },
        }


class SearchBehaviorEvalError(ValueError):
    """Raised when JSONL cannot be parsed into Codex exec events."""


def sanitized_search_behavior_metrics(
    events: Sequence[Mapping[str, Any]],
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Classifier plus metrics only; omit evidence paths and argv (accept_codex report)."""
    report = evaluate_search_behavior(events, workspace_root=workspace_root)
    payload = report.to_json_dict()
    metrics = payload["metrics"]
    if not isinstance(metrics, dict):
        raise SearchBehaviorEvalError("search behavior metrics missing")
    summary = {
        "search_hit_quality": payload["search_hit_quality"],
        "native_followup": payload["native_followup"],
        "duplicate_broad_search": payload["duplicate_broad_search"],
        **metrics,
    }
    return {key: summary[key] for key in SANITIZED_METRIC_KEYS}


def evaluate_search_behavior(
    events: Sequence[Mapping[str, Any]],
    *,
    workspace_root: str | Path | None = None,
) -> SearchBehaviorReport:
    """Deterministically classify completed Codex JSONL items."""
    root = _workspace_root_text(workspace_root)
    status_first = not project_actions_before_harness_status(events)
    task_before_diagnosis = not discovery_actions_before_task_start(events)
    search_item, search_index = _first_project_search(events)
    query, quality, candidates = _search_outcome(search_item)
    native_commands: list[NativeCommandEvidence] = []
    first_broad_before_search = False
    for index, item in enumerate(iter_completed_items(events)):
        evidence = _classify_native_item(item, candidates=candidates, workspace_root=root)
        if evidence is None:
            continue
        if search_index is None or index < search_index:
            if evidence.command_class is CommandClass.BROAD_SEARCH:
                first_broad_before_search = True
            continue
        native_commands.append(evidence)
    followup = (
        native_commands[0].command_class if native_commands else CommandClass.UNRELATED_COMMAND
    )
    duplicate = _duplicate_broad_search(quality, query, native_commands)
    targeted_read = any(
        command.command_class is CommandClass.TARGETED_READ for command in native_commands
    )
    broad_after = any(
        command.command_class is CommandClass.BROAD_SEARCH for command in native_commands
    )
    return SearchBehaviorReport(
        search_hit_quality=quality,
        native_followup=followup,
        duplicate_broad_search=duplicate,
        status_first=status_first,
        task_before_diagnosis=task_before_diagnosis,
        search_before_broad_native=not first_broad_before_search,
        good_hit_to_targeted_read=quality is SearchHitQuality.STRONG and targeted_read,
        good_hit_to_duplicate_broad_search=duplicate,
        zero_hit_to_native_fallback=quality
        in {SearchHitQuality.ZERO, SearchHitQuality.INSUFFICIENT}
        and broad_after,
        project_search_query=query,
        candidate_paths=candidates,
        native_commands=tuple(native_commands),
    )


def parse_jsonl_events(text: str) -> list[dict[str, Any]]:
    """Parse Codex exec JSONL. Empty input is a valid empty event list."""
    events: list[dict[str, Any]] = []
    for position, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SearchBehaviorEvalError(f"invalid JSONL at line {position}") from exc
        if not isinstance(event, dict):
            raise SearchBehaviorEvalError(f"non-object JSONL event at line {position}")
        events.append(event)
    return events


def _first_project_search(
    events: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, int | None]:
    for index, item in enumerate(iter_completed_items(events)):
        server, tool = mcp_server_and_tool(item)
        if (
            item.get("type") == "mcp_tool_call"
            and server == HARNESS_SERVER
            and tool == "project_search"
        ):
            return item, index
    return None, None


def _search_outcome(
    item: Mapping[str, Any] | None,
) -> tuple[str | None, SearchHitQuality, tuple[str, ...]]:
    if item is None:
        return None, SearchHitQuality.INSUFFICIENT, ()
    status = item.get("status")
    error = item.get("error")
    if status not in {None, "completed"} or error not in {None, ""}:
        return _search_query(item), SearchHitQuality.INSUFFICIENT, ()
    query = _search_query(item)
    payload = _search_result_payload(item)
    if payload is None:
        return query, SearchHitQuality.INSUFFICIENT, ()
    results = payload.get("results")
    if not isinstance(results, list):
        return query, SearchHitQuality.INSUFFICIENT, ()
    if not results:
        return query, SearchHitQuality.ZERO, ()
    paths = tuple(_candidate_paths(results))
    if not paths:
        return query, SearchHitQuality.INSUFFICIENT, ()

    production_hits = tuple(
        hit
        for hit in results
        if isinstance(hit, Mapping)
        and hit.get("kind") in _CODE_DOC_KINDS
        and isinstance(hit.get("path"), str)
        and "evidence" in hit
    )
    if production_hits:
        if any(_has_current_source_evidence(hit) for hit in production_hits):
            return query, SearchHitQuality.STRONG, paths
        return query, SearchHitQuality.INSUFFICIENT, paths

    # Older/synthetic JSONL fixtures predate the production evidence fields. Keep them
    # classifiable without weakening the current MCP contract, whose code/doc hits always
    # include explicit evidence/evidence_reason fields.
    return query, SearchHitQuality.STRONG, paths


def _has_current_source_evidence(hit: Mapping[str, Any]) -> bool:
    evidence = hit.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    snippet = evidence.get("snippet")
    return isinstance(snippet, str) and bool(snippet.strip())


def _search_query(item: Mapping[str, Any]) -> str | None:
    arguments = _as_mapping(item.get("arguments")) or {}
    query = arguments.get("query")
    if isinstance(query, str) and query.strip():
        return query
    payload = _search_result_payload(item)
    if payload is not None:
        echoed = payload.get("query")
        if isinstance(echoed, str) and echoed.strip():
            return echoed
    return None


def _search_result_payload(item: Mapping[str, Any]) -> dict[str, Any] | None:
    result = item.get("result")
    if isinstance(result, str):
        return _as_mapping(result)
    if not isinstance(result, Mapping):
        return None
    for key in ("structured_content", "structuredContent"):
        mapped = _as_mapping(result.get(key))
        if mapped is not None:
            return mapped
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        mapped = _as_mapping(block.get("text"))
        if mapped is not None:
            return mapped
    return None


def _candidate_paths(results: Sequence[Any]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()
    for hit in results:
        if not isinstance(hit, Mapping):
            continue
        kind = hit.get("kind")
        path = hit.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        if kind is not None and kind not in _CODE_DOC_KINDS:
            continue
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def _classify_native_item(
    item: Mapping[str, Any],
    *,
    candidates: tuple[str, ...],
    workspace_root: str | None,
) -> NativeCommandEvidence | None:
    item_type = item.get("type")
    if item_type == "file_change":
        return NativeCommandEvidence(CommandClass.UNRELATED_COMMAND, (), None)
    if item_type != "command_execution":
        return None
    argv = _command_argv(item)
    if argv is None:
        return NativeCommandEvidence(CommandClass.UNKNOWN, (), None)
    if any(token in _META_TOKENS or token.startswith("`") for token in argv):
        return NativeCommandEvidence(CommandClass.UNKNOWN, _redact_argv(argv), None)
    tool = Path(argv[0]).name
    if tool in SEARCH_TOOLS:
        command_class, pattern = _classify_search(argv, workspace_root=workspace_root)
        return NativeCommandEvidence(command_class, _redact_argv(argv), pattern)
    if tool in READ_TOOLS:
        command_class = _classify_read(argv, candidates=candidates, workspace_root=workspace_root)
        return NativeCommandEvidence(command_class, _redact_argv(argv), None)
    return NativeCommandEvidence(CommandClass.UNRELATED_COMMAND, _redact_argv(argv), None)


def _command_argv(item: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = item.get("command")
    argv: tuple[str, ...] | None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            argv = tuple(shlex.split(raw))
        except ValueError:
            return None
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        parts = tuple(raw)
        if not parts or not all(isinstance(part, str) for part in parts):
            return None
        argv = tuple(str(part) for part in parts)
    else:
        return None
    if not argv:
        return None
    return _unwrap_shell(argv)


def _unwrap_shell(argv: tuple[str, ...]) -> tuple[str, ...]:
    if len(argv) < 3:
        return argv
    if Path(argv[0]).name not in _SHELLS:
        return argv
    flag = argv[1]
    if flag not in {"-c", "-lc"} and not (
        flag.startswith("-") and "c" in flag[1:] and len(flag) <= 4
    ):
        return argv
    try:
        inner = tuple(shlex.split(argv[2]))
    except ValueError:
        return argv
    return inner or argv


def _classify_search(
    argv: tuple[str, ...],
    *,
    workspace_root: str | None,
) -> tuple[CommandClass, str | None]:
    parsed = _search_operands(argv)
    if parsed is None:
        return CommandClass.UNKNOWN, None
    pattern, paths, globs = parsed
    if _is_repo_broad(paths, globs, workspace_root):
        return CommandClass.BROAD_SEARCH, pattern
    return CommandClass.TARGETED_SEARCH, pattern


def _search_operands(
    argv: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]] | None:
    tool = Path(argv[0]).name
    value_flags = _RG_VALUE_FLAGS if tool == "rg" else _GREP_VALUE_FLAGS
    patterns: list[str] = []
    globs: list[str] = []
    positional: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            positional.extend(argv[index + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            name, eq, value = arg.partition("=")
            if eq:
                if name in {"-g", "--glob", "--include"}:
                    globs.append(value)
                elif name in {"-e", "--regexp", "--regex"}:
                    patterns.append(value)
                index += 1
                continue
            if arg in value_flags:
                if index + 1 >= len(argv):
                    return None
                value = argv[index + 1]
                if arg in {"-g", "--glob", "--include"}:
                    globs.append(value)
                elif arg in {"-e", "--regexp", "--regex"}:
                    patterns.append(value)
                index += 2
                continue
            index += 1
            continue
        positional.append(arg)
        index += 1
    if patterns:
        return patterns[0], tuple(positional), tuple(globs)
    if not positional:
        return None, (), tuple(globs)
    return positional[0], tuple(positional[1:]), tuple(globs)


def _is_repo_broad(
    paths: tuple[str, ...],
    globs: tuple[str, ...],
    workspace_root: str | None,
) -> bool:
    if any(_path_is_repo_root(path, workspace_root) for path in paths):
        return True
    if paths:
        return False
    if globs and all(not _glob_is_repo_wide(glob) for glob in globs):
        return False
    return True


def _classify_read(
    argv: tuple[str, ...],
    *,
    candidates: tuple[str, ...],
    workspace_root: str | None,
) -> CommandClass:
    paths = _read_paths(argv)
    if paths is None:
        return CommandClass.UNKNOWN
    if not paths or any(_path_is_repo_root(path, workspace_root) for path in paths):
        return CommandClass.UNKNOWN
    candidate_set = {_normalize_path(path, workspace_root) for path in candidates}
    if any(_is_candidate_or_localized_dir(path, candidate_set, workspace_root) for path in paths):
        return CommandClass.TARGETED_READ
    return CommandClass.UNRELATED_COMMAND


def _read_paths(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    paths: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            paths.extend(argv[index + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            name, eq, _value = arg.partition("=")
            if eq:
                index += 1
                continue
            if arg in _READ_VALUE_FLAGS:
                if index + 1 >= len(argv):
                    return None
                index += 2
                continue
            if name in _READ_VALUE_FLAGS:
                index += 1
                continue
            index += 1
            continue
        paths.append(arg)
        index += 1
    return tuple(paths)


def _duplicate_broad_search(
    quality: SearchHitQuality,
    query: str | None,
    native_commands: Sequence[NativeCommandEvidence],
) -> bool:
    if quality is not SearchHitQuality.STRONG or not query:
        return False
    return any(
        command.command_class is CommandClass.BROAD_SEARCH
        and command.search_pattern is not None
        and _substantially_repeats(command.search_pattern, query)
        for command in native_commands
    )


def _substantially_repeats(pattern: str, query: str) -> bool:
    stripped = pattern.strip().strip("'\"")
    if len(stripped) >= 4 and (
        stripped.lower() in query.lower() or query.lower() in stripped.lower()
    ):
        return True
    pattern_tokens = _tokens(stripped)
    query_tokens = _tokens(query)
    if not pattern_tokens or not query_tokens:
        return False
    overlap = pattern_tokens & query_tokens
    if overlap == pattern_tokens or overlap == query_tokens:
        return True
    return len(overlap) / len(pattern_tokens | query_tokens) >= 0.5


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= 2 and token not in _STOPWORDS
    )


def _path_is_repo_root(path: str, workspace_root: str | None) -> bool:
    return _normalize_path(path, workspace_root) == "."


def _glob_is_repo_wide(glob: str) -> bool:
    text = glob.strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    first = text.split("/", 1)[0]
    return not first or "*" in first or first == "**"


def _is_candidate_or_localized_dir(
    path: str,
    candidates: set[str],
    workspace_root: str | None,
) -> bool:
    norm = _normalize_path(path, workspace_root)
    if norm == ".":
        return False
    if norm in candidates:
        return True
    prefix = norm.rstrip("/") + "/"
    return any(candidate == norm or candidate.startswith(prefix) for candidate in candidates)


def _normalize_path(path: str, workspace_root: str | None) -> str:
    text = path.replace("\\", "/").strip()
    if text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/") or "."
    if workspace_root:
        root = workspace_root.replace("\\", "/").rstrip("/")
        if text == root:
            return "."
        if text.startswith(root + "/"):
            relative = text[len(root) + 1 :]
            return relative or "."
    return text or "."


def _workspace_root_text(workspace_root: str | Path | None) -> str | None:
    if workspace_root is None:
        return None
    return str(workspace_root).replace("\\", "/").rstrip("/")


def _as_mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_redact_text(part) or "" for part in argv)


def _redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _ENV_SECRET_RE.match(value):
        key, _sep, _rest = value.partition("=")
        return f"{key}=<redacted>"
    return _SECRET_RE.sub("<redacted>", value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Classify Codex exec JSONL for Harness project_search vs native rg/grep. "
            "Does not invoke a model or contact harnessd. Real Codex --run-model is optional "
            "and is wired from scripts/accept_codex.py using sanitized metrics only."
        )
    )
    parser.add_argument(
        "jsonl",
        nargs="?",
        type=Path,
        help="Codex exec --json JSONL file (default: stdin)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write sanitized JSON report to this path in addition to stdout",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="workspace root used to treat absolute paths as repo-relative",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.jsonl is None and sys.stdin.isatty():
        _parser().print_help()
        return 2
    try:
        raw = args.jsonl.read_text(encoding="utf-8") if args.jsonl is not None else sys.stdin.read()
        events = parse_jsonl_events(raw)
        report = evaluate_search_behavior(events, workspace_root=args.workspace_root)
    except SearchBehaviorEvalError as exc:
        print(f"eval_search_behavior: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"eval_search_behavior: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report.to_json_dict(), indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
