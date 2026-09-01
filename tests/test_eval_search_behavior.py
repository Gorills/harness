from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_search_behavior import (
    SANITIZED_METRIC_KEYS,
    CommandClass,
    SearchHitQuality,
    evaluate_search_behavior,
    main,
    parse_jsonl_events,
    sanitized_search_behavior_metrics,
)

SOURCE_BODY = "def leaked_source_body():\n    return 'secret-file-body'\n"
API_SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz012345"
QUERY = "authenticate user"
CANDIDATE = "src/auth.py"


def _completed(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "item.completed", "item": item}


def _mcp(tool: str, **fields: Any) -> dict[str, Any]:
    item = {
        "type": "mcp_tool_call",
        "server": "harness",
        "tool": tool,
        "status": "completed",
        **fields,
    }
    return _completed(item)


def _status() -> dict[str, Any]:
    return _mcp("project_status")


def _task_start() -> dict[str, Any]:
    return _mcp("task_start")


def _search(
    query: str,
    results: Sequence[Mapping[str, Any]] | None,
    *,
    via: str = "structured_content",
    scope: str = "code",
) -> dict[str, Any]:
    payload = {"query": query, "scope": scope, "results": list(results or ())}
    if via == "structured_content":
        result: dict[str, Any] = {"structured_content": payload, "content": []}
    else:
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    return _mcp("project_search", arguments={"query": query, "scope": scope}, result=result)


def _command(command: str | Sequence[str], *, output: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "command_execution", "command": command, "status": "completed"}
    if output is not None:
        item["aggregated_output"] = output
    return _completed(item)


def _canonical(
    *native: dict[str, Any], results: Sequence[Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    hits = (
        list(results)
        if results is not None
        else [{"kind": "code", "path": CANDIDATE, "title": "auth"}]
    )
    return [_status(), _task_start(), _search(QUERY, hits), *native]


def test_search_hit_quality_enum_is_locked() -> None:
    assert tuple(SearchHitQuality) == (
        SearchHitQuality.STRONG,
        SearchHitQuality.ZERO,
        SearchHitQuality.INSUFFICIENT,
    )
    assert tuple(member.value for member in SearchHitQuality) == ("strong", "zero", "insufficient")
    assert tuple(member.value for member in CommandClass) == (
        "targeted_read",
        "targeted_search",
        "broad_search",
        "unrelated_command",
        "unknown",
    )


def test_strong_hit_targeted_read_is_not_duplicate() -> None:
    report = evaluate_search_behavior(_canonical(_command(f"cat {CANDIDATE}")))

    assert report.search_hit_quality is SearchHitQuality.STRONG
    assert report.native_followup is CommandClass.TARGETED_READ
    assert report.duplicate_broad_search is False
    assert report.good_hit_to_targeted_read is True
    assert report.good_hit_to_duplicate_broad_search is False
    assert report.zero_hit_to_native_fallback is False
    assert report.status_first is True
    assert report.task_before_diagnosis is True
    assert report.search_before_broad_native is True


def test_strong_hit_repo_root_rg_of_same_query_is_duplicate() -> None:
    report = evaluate_search_behavior(
        _canonical(_command("bash -lc 'rg authenticate user .'")),
    )

    assert report.search_hit_quality is SearchHitQuality.STRONG
    assert report.native_followup is CommandClass.BROAD_SEARCH
    assert report.duplicate_broad_search is True
    assert report.good_hit_to_duplicate_broad_search is True
    assert report.good_hit_to_targeted_read is False


def test_zero_hits_broad_rg_is_justified_fallback() -> None:
    events = [
        _status(),
        _task_start(),
        _search(QUERY, []),
        _command(["rg", QUERY, "."]),
    ]
    report = evaluate_search_behavior(events)

    assert report.search_hit_quality is SearchHitQuality.ZERO
    assert report.native_followup is CommandClass.BROAD_SEARCH
    assert report.duplicate_broad_search is False
    assert report.zero_hit_to_native_fallback is True
    assert report.good_hit_to_duplicate_broad_search is False


def test_insufficient_task_hits_allow_broad_native_fallback() -> None:
    events = [
        _status(),
        _task_start(),
        _search(QUERY, [{"kind": "task", "title": "Old task", "location": "task:abc"}]),
        _command("rg authenticate ."),
    ]
    report = evaluate_search_behavior(events)

    assert report.search_hit_quality is SearchHitQuality.INSUFFICIENT
    assert report.native_followup is CommandClass.BROAD_SEARCH
    assert report.duplicate_broad_search is False
    assert report.zero_hit_to_native_fallback is True


def test_targeted_rg_after_hit_is_allowed() -> None:
    report = evaluate_search_behavior(_canonical(_command(f"rg 'def authenticate' {CANDIDATE}")))

    assert report.search_hit_quality is SearchHitQuality.STRONG
    assert report.native_followup is CommandClass.TARGETED_SEARCH
    assert report.duplicate_broad_search is False
    assert report.good_hit_to_targeted_read is False


def test_subdirectory_rg_after_localization_is_targeted() -> None:
    report = evaluate_search_behavior(_canonical(_command("rg authenticate src")))

    assert report.native_followup is CommandClass.TARGETED_SEARCH
    assert report.duplicate_broad_search is False


def test_head_and_sed_of_candidate_are_targeted_reads() -> None:
    head_report = evaluate_search_behavior(_canonical(_command(f"head -n 40 {CANDIDATE}")))
    sed_report = evaluate_search_behavior(_canonical(_command(f"sed -n '1,40p' {CANDIDATE}")))

    assert head_report.native_followup is CommandClass.TARGETED_READ
    assert sed_report.native_followup is CommandClass.TARGETED_READ
    assert head_report.duplicate_broad_search is False
    assert sed_report.duplicate_broad_search is False


def test_missing_command_string_is_unknown_not_broad() -> None:
    events = _canonical(_completed({"type": "command_execution", "status": "completed"}))
    report = evaluate_search_behavior(events)

    assert report.native_followup is CommandClass.UNKNOWN
    assert report.duplicate_broad_search is False


def test_file_change_is_unrelated_not_a_file_read() -> None:
    events = _canonical(
        _completed(
            {
                "type": "file_change",
                "status": "completed",
                "changes": [{"path": CANDIDATE, "kind": "update"}],
            }
        )
    )
    report = evaluate_search_behavior(events)

    assert report.native_followup is CommandClass.UNRELATED_COMMAND


def test_repo_wide_glob_without_path_is_broad() -> None:
    report = evaluate_search_behavior(_canonical(_command("rg --glob '*.py' authenticate")))

    assert report.native_followup is CommandClass.BROAD_SEARCH
    assert report.duplicate_broad_search is True


def test_content_text_result_payload_is_accepted() -> None:
    events = [
        _status(),
        _task_start(),
        _search(QUERY, [{"kind": "code", "path": CANDIDATE}], via="content"),
        _command(f"cat {CANDIDATE}"),
    ]
    report = evaluate_search_behavior(events)

    assert report.search_hit_quality is SearchHitQuality.STRONG
    assert report.candidate_paths == (CANDIDATE,)
    assert report.native_followup is CommandClass.TARGETED_READ


def test_mcp_wire_doc_hit_plus_cat_is_strong_targeted_read() -> None:
    """Production MCP hits use kind=doc (scope remains docs), plus path and ref."""
    events = [
        _status(),
        _task_start(),
        _search(
            QUERY,
            [
                {
                    "ref": "doc:docs/auth.md",
                    "kind": "doc",
                    "title": "auth.md",
                    "location": "docs/auth.md",
                    "short_summary": None,
                    "match_reason": "lexical content (all terms)",
                    "freshness": "indexed_snapshot",
                    "path": "docs/auth.md",
                }
            ],
            scope="docs",
        ),
        _command("cat docs/auth.md"),
    ]
    report = evaluate_search_behavior(events)

    assert report.search_hit_quality is SearchHitQuality.STRONG
    assert report.candidate_paths == ("docs/auth.md",)
    assert report.native_followup is CommandClass.TARGETED_READ
    assert report.good_hit_to_targeted_read is True
    assert report.duplicate_broad_search is False


def test_abs_root_rg_is_duplicate_only_with_workspace_root() -> None:
    events = _canonical(_command("rg authenticate /tmp/ws"))
    without_root = evaluate_search_behavior(events)
    with_root = evaluate_search_behavior(events, workspace_root="/tmp/ws")

    assert without_root.search_hit_quality is SearchHitQuality.STRONG
    assert without_root.native_followup is CommandClass.TARGETED_SEARCH
    assert without_root.duplicate_broad_search is False
    assert with_root.native_followup is CommandClass.BROAD_SEARCH
    assert with_root.duplicate_broad_search is True


def test_server_name_alias_and_absolute_workspace_root() -> None:
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server_name": "harness",
                "name": "project_status",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server_name": "harness",
                "name": "task_start",
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server_name": "harness",
                "name": "project_search",
                "status": "completed",
                "arguments": json.dumps({"query": QUERY}),
                "result": {
                    "structuredContent": {"results": [{"kind": "doc", "path": "/tmp/ws/README.md"}]}
                },
            },
        },
        _command("cat /tmp/ws/README.md"),
    ]
    report = evaluate_search_behavior(events, workspace_root="/tmp/ws")

    assert report.search_hit_quality is SearchHitQuality.STRONG
    assert report.candidate_paths == ("/tmp/ws/README.md",)
    assert report.native_followup is CommandClass.TARGETED_READ


def test_broad_rg_before_project_search_fails_search_before_broad_native() -> None:
    events = [
        _status(),
        _task_start(),
        _command("rg authenticate ."),
        _search(QUERY, [{"kind": "code", "path": CANDIDATE}]),
        _command(f"cat {CANDIDATE}"),
    ]
    report = evaluate_search_behavior(events)

    assert report.search_before_broad_native is False
    assert report.native_followup is CommandClass.TARGETED_READ
    assert report.duplicate_broad_search is False


def test_sanitization_omits_source_bodies_and_secrets() -> None:
    events = _canonical(
        _command(
            f"cat {CANDIDATE}",
            output=SOURCE_BODY + f"CODEX_API_KEY={API_SECRET}\n",
        )
    )
    events[2]["item"]["result"]["content"] = [
        {"type": "text", "text": SOURCE_BODY + f"token={API_SECRET}"}
    ]
    leaked_command = _command(f"echo CODEX_API_KEY={API_SECRET}")
    events.append(leaked_command)
    payload = json.dumps(evaluate_search_behavior(events).to_json_dict())
    metrics = json.dumps(sanitized_search_behavior_metrics(events))

    for blob in (payload, metrics):
        assert SOURCE_BODY.split("(", 1)[0] not in blob
        assert "leaked_source_body" not in blob
        assert "secret-file-body" not in blob
        assert API_SECRET not in blob
        assert "aggregated_output" not in blob
    assert "<redacted>" in payload
    assert "evidence" not in metrics
    assert tuple(sanitized_search_behavior_metrics(events)) == SANITIZED_METRIC_KEYS


def test_sanitized_metrics_are_the_accept_codex_report_contract() -> None:
    events = _canonical(_command(f"cat {CANDIDATE}"))
    summary = sanitized_search_behavior_metrics(events)
    assert summary["search_hit_quality"] == "strong"
    assert summary["native_followup"] == "targeted_read"
    assert summary["duplicate_broad_search"] is False
    assert "candidate_paths" not in summary


def test_preflight_parser_reads_jsonl_without_a_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _canonical(_command(f"cat {CANDIDATE}"))
    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    output = tmp_path / "report.json"

    assert parse_jsonl_events(jsonl.read_text(encoding="utf-8")) == events
    assert main([str(jsonl), "--output", str(output)]) == 0
    stdout = capsys.readouterr().out
    report = json.loads(output.read_text(encoding="utf-8"))

    assert json.loads(stdout) == report
    assert report["search_hit_quality"] == "strong"
    assert report["native_followup"] == "targeted_read"
    assert report["duplicate_broad_search"] is False
    assert "leaked_source_body" not in stdout


def test_invalid_jsonl_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    jsonl = tmp_path / "bad.jsonl"
    jsonl.write_text("{not json}\n", encoding="utf-8")

    assert main([str(jsonl)]) == 2
    assert "invalid JSONL" in capsys.readouterr().err
