from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_search_behavior import SearchHitQuality, _search_outcome
from harness.retrieval import _relocate_search_evidence


def _project_search_item(evidence: object) -> dict[str, Any]:
    query = "authenticate user"
    return {
        "type": "mcp_tool_call",
        "server": "harness",
        "tool": "project_search",
        "status": "completed",
        "arguments": {"query": query, "scope": "code"},
        "result": {
            "structured_content": {
                "query": query,
                "scope": "code",
                "results": [
                    {
                        "ref": "code:src/auth.py",
                        "kind": "code",
                        "path": "src/auth.py",
                        "title": "auth.py",
                        "evidence": evidence,
                        "evidence_reason": None if evidence is not None else "response_budget",
                    }
                ],
            }
        },
    }


def test_small_current_source_match_returns_the_useful_file_context() -> None:
    text = (
        "from __future__ import annotations\n"
        "\n"
        "class AuthService:\n"
        "    def authenticate_user(self, token: str) -> bool:\n"
        "        if not token:\n"
        "            return False\n"
        "        return self.verify(token)\n"
    )

    evidence = _relocate_search_evidence(text, ("authenticate", "user"))

    assert evidence is not None
    assert evidence.start_line == 1
    assert evidence.end_line == len(text.splitlines())
    assert evidence.snippet == text.rstrip("\n")
    assert evidence.truncated is False


def test_production_hit_without_current_source_evidence_is_not_strong() -> None:
    query, quality, paths = _search_outcome(_project_search_item(None))

    assert query == "authenticate user"
    assert quality is SearchHitQuality.INSUFFICIENT
    assert paths == ("src/auth.py",)


def test_production_hit_with_current_source_evidence_is_strong() -> None:
    evidence = {
        "start_line": 1,
        "end_line": 7,
        "snippet": "class AuthService:\n    def authenticate_user(self, token: str) -> bool: ...",
        "truncated": False,
    }

    query, quality, paths = _search_outcome(_project_search_item(evidence))

    assert query == "authenticate user"
    assert quality is SearchHitQuality.STRONG
    assert paths == ("src/auth.py",)
