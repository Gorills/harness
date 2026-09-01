"""Lock ADR-0041 Option A: next-session-only task skill delivery.

Filesystem/watcher projection after task_start is not live MCP skill-body delivery.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from queue import Empty, SimpleQueue

import pytest

from harness.daemon import mutate_task_start
from harness.index import scan_workspace
from harness.ipc import TaskStartRequestData, TaskStartResult, WorkspaceTaskSummary
from harness.mcp_bridge import (
    _PROJECT_CONTEXT_DESCRIPTION,
    _PROJECT_SEARCH_DESCRIPTION,
    _SERVER_INSTRUCTIONS,
    _TASK_START_DESCRIPTION,
    _TOOL_ARGUMENTS,
    _status_task_payload,
    _task_start_payload,
)
from harness.registry import create_project, register_workspace
from harness.retrieval import (
    ProjectRetrievalRefError,
    ProjectSearchKind,
    ProjectSearchScope,
    read_project_context,
)
from harness.skills import (
    SKILL_METADATA_FILE_NAME,
    ResolvedSkill,
    load_skill_registry,
    resolve_workspace_skills,
)
from harness.storage import connect_database, initialize_database
from harness.tasks import SkillRelevanceKey, TaskState, get_task, skill_relevance_key
from harness.workspace_resolution import WorkspaceHint

REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_0041 = REPO_ROOT / "docs" / "decisions" / "0041-task-skill-session-delivery.md"
FORBIDDEN_SKILL_DELIVERY_FIELDS = (
    "recommended_skills",
    "skill_body",
    "skill_bodies",
    "skill_refs",
    "selected_skills",
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
SYNTHETIC_GATE_EXPECTED_RESULT = "next-session-only"
_CURRENT_SESSION_GUIDANCE_CLAIM = "guidance X is available in the current session"
_SKILL_X_ID = "session-skill-x"
_SKILL_X_HINT = "expo"
_SKILL_X_BODY_MARKER = "UNIQUE_SKILL_X_GUIDANCE_BODY_7f3a"


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text))


def _section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    end_at = text.index(end, start_at + len(start))
    return text[start_at:end_at]


def _expected_result_token(section: str) -> str:
    match = re.search(r"Expected result:\s*(.+)", section)
    assert match is not None
    tokens = _tokens(match.group(1))
    assert tokens
    return tokens[0]


def _folded(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _question_answer(text: str, heading: str, *, next_heading: str) -> str:
    start = text.index(heading)
    question_end = text.index("?", start) + 1
    answer = text[question_end : text.index(next_heading, question_end)]
    return re.sub(r"^[\s*]+", "", answer)


def _mcp_session_snapshot(
    *,
    task_start: dict[str, object],
    current_task: dict[str, object],
) -> str:
    return json.dumps(
        {
            "tools": list(_TOOL_ARGUMENTS),
            "task_start": task_start,
            "current_task": current_task,
            "instructions": _SERVER_INSTRUCTIONS,
            "task_start_description": _TASK_START_DESCRIPTION,
            "project_search": _PROJECT_SEARCH_DESCRIPTION,
            "project_context": _PROJECT_CONTEXT_DESCRIPTION,
        },
        sort_keys=True,
    )


def _assert_no_skill_delivery(serialized: str) -> None:
    for name in FORBIDDEN_SKILL_DELIVERY_FIELDS:
        assert name not in serialized
    assert _SKILL_X_BODY_MARKER not in serialized


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _drain(queue: SimpleQueue[str]) -> list[str]:
    items: list[str] = []
    while True:
        try:
            items.append(queue.get_nowait())
        except Empty:
            return items


def _ids(resolved: tuple[ResolvedSkill, ...]) -> tuple[str, ...]:
    return tuple(item.definition.skill_id for item in resolved)


def _write_skill_x(registry: Path) -> None:
    directory = registry / _SKILL_X_ID
    directory.mkdir(parents=True)
    registry.chmod(0o700)
    (directory / "SKILL.md").write_text(
        f"---\nname: {_SKILL_X_ID}\ndescription: Portable {_SKILL_X_ID} instructions.\n"
        f"---\n\n# {_SKILL_X_ID}\n\n{_SKILL_X_BODY_MARKER}\n",
        encoding="utf-8",
    )
    (directory / SKILL_METADATA_FILE_NAME).write_text(
        f"id: {_SKILL_X_ID}\ntask_hints:\n  - {_SKILL_X_HINT}\n",
        encoding="utf-8",
    )


def _registered_workspace(tmp_path: Path) -> tuple[Path, sqlite3.Connection, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("project\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        "init",
    )
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    project = create_project(connection)
    workspace = register_workspace(connection, project_id=project.project_id, path=root)
    scan_workspace(connection, workspace.workspace_id)
    return root, connection, workspace.workspace_id


def test_adr_0041_locks_option_a_next_session_only() -> None:
    text = ADR_0041.read_text(encoding="utf-8")
    decision = _section(text, "## Decision", "### Nine questions")
    q1 = _question_answer(
        text,
        "Must Task hints affect the current live session?",
        next_heading="What is proof of delivery?",
    )
    gate = _section(text, "### Synthetic acceptance gate", "### Nine questions")
    q6 = _question_answer(
        text,
        "How is duplicate or conflicting guidance prevented?",
        next_heading="What remains host-native?",
    )
    q9 = _question_answer(
        text,
        "How does acceptance verify this without undocumented host assumptions?",
        next_heading="## Consequences",
    )

    assert "Option A" in decision
    assert "Not implemented here" in decision
    assert "does not claim" in decision
    assert "current session" in decision

    assert q1.lstrip().startswith("No.")
    assert "does not claim" in q1
    assert "current-session" in q1
    assert "do not mutate" in q1 or "does not mutate" in q1
    assert "model instruction context" in q1

    result = _expected_result_token(gate)
    assert result == SYNTHETIC_GATE_EXPECTED_RESULT
    assert result != _CURRENT_SESSION_GUIDANCE_CLAIM
    claim_tokens = _tokens(_CURRENT_SESSION_GUIDANCE_CLAIM)
    gate_tokens = _tokens(gate)
    expected_index = gate_tokens.index(SYNTHETIC_GATE_EXPECTED_RESULT)
    assert gate_tokens[expected_index : expected_index + len(claim_tokens)] != claim_tokens

    assert "stale previously projected skills" in q6
    assert "not a Harness bug" in q6
    assert "Option A" in q6

    assert "task_start" in q9
    assert "live skill injection" in q9
    assert "relevant generated skill is visible" in _folded(q9)
    assert "current-session delivery" in q9
    assert "next discovery boundary" in q9
    assert SYNTHETIC_GATE_EXPECTED_RESULT in _tokens(q9)

    for question in (
        "Must Task hints affect the current live session?",
        "What is proof of delivery?",
        "What happens on a host without hot reload?",
        "What happens on skill budget overflow?",
        "How are nested `references/` disclosed?",
        "How is duplicate or conflicting guidance prevented?",
        "What remains host-native?",
        "What is daemon-owned?",
        "How does acceptance verify this without undocumented host assumptions?",
    ):
        assert question in text


def test_mcp_surface_does_not_deliver_skill_bodies() -> None:
    assert tuple(_TOOL_ARGUMENTS) == (
        "project_status",
        "project_search",
        "project_context",
        "task_start",
        "task_checkpoint",
    )
    payload = _task_start_payload(
        TaskStartResult(
            schema_version=14,
            workspace_id="ws",
            task_id="task",
            state=TaskState.WORKING,
            wait_reason=None,
            revision=1,
        )
    )
    assert set(payload) == {
        "workspace_id",
        "task_id",
        "state",
        "wait_reason",
        "revision",
    }
    status = _status_task_payload(
        WorkspaceTaskSummary(
            task_id="task",
            title="t",
            state=TaskState.WORKING,
            wait_reason=None,
            revision=1,
        )
    )
    assert set(status) == {"task_id", "title", "state", "wait_reason", "revision"}
    serialized = _mcp_session_snapshot(task_start=payload, current_task=status)
    _assert_no_skill_delivery(serialized)
    assert "next host discovery boundary" in _TASK_START_DESCRIPTION
    assert "not current-session MCP skill delivery" in _TASK_START_DESCRIPTION
    assert "live skill injection" in _TASK_START_DESCRIPTION
    assert "stack_hints drive" in _TASK_START_DESCRIPTION
    assert len(_SERVER_INSTRUCTIONS.encode("utf-8")) < 1024
    for text in (
        _PROJECT_SEARCH_DESCRIPTION,
        _PROJECT_CONTEXT_DESCRIPTION,
    ):
        assert "hot reload" not in text
        assert "recommended_skills" not in text


def test_project_context_rejects_skill_ref(tmp_path: Path) -> None:
    assert {kind.value for kind in ProjectSearchKind} == {"code", "doc", "knowledge", "task"}
    assert {scope.value for scope in ProjectSearchScope} == {
        "all",
        "code",
        "docs",
        "knowledge",
        "tasks",
    }
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "README.md").write_text("repo\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        project = create_project(connection)
        workspace = register_workspace(connection, project_id=project.project_id, path=root)
        with pytest.raises(ProjectRetrievalRefError, match="kind is unsupported"):
            read_project_context(
                connection,
                workspace.workspace_id,
                ("skill:language-engineering",),
            )
    finally:
        connection.close()


def test_instruction_surfaces_state_next_discovery_boundary() -> None:
    adr_0032 = (
        REPO_ROOT / "docs" / "decisions" / "0032-continuous-project-skill-reconciliation.md"
    ).read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    spec = (REPO_ROOT / "docs" / "specification.md").read_text(encoding="utf-8")
    host = (REPO_ROOT / "docs" / "host-compatibility.md").read_text(encoding="utf-8")
    accept = (REPO_ROOT / "scripts" / "accept_codex.py").read_text(encoding="utf-8")
    adr_0041 = ADR_0041.read_text(encoding="utf-8")

    assert "next host discovery boundary" in adr_0032
    assert "ADR-0041" in adr_0032
    assert "Skill hot reload is an optimization" in architecture
    assert "ADR-0041" in architecture
    assert "Harness MCP does not deliver" in architecture
    assert "does not deliver skill bodies into the current session" in _folded(architecture)
    assert "MCP delivers skill" not in architecture
    assert "MCP does deliver" not in architecture
    assert "Correctness не зависит от live detection" in spec
    accept_expected = re.search(
        r'^TASK_SKILL_SESSION_DELIVERY_EXPECTED_RESULT = "([^"]+)"$',
        accept,
        re.MULTILINE,
    )
    assert accept_expected is not None
    assert accept_expected.group(1) == SYNTHETIC_GATE_EXPECTED_RESULT
    assert SYNTHETIC_GATE_EXPECTED_RESULT in _tokens(host)
    assert SYNTHETIC_GATE_EXPECTED_RESULT in _tokens(accept)
    assert (
        _expected_result_token(
            _section(adr_0041, "### Synthetic acceptance gate", "### Nine questions")
        )
        == SYNTHETIC_GATE_EXPECTED_RESULT
    )
    assert "do not score it as Harness current-session delivery" in host
    assert "files at the next discovery boundary" in _folded(host)
    assert "next host discovery boundary" in _TASK_START_DESCRIPTION
    assert "not current-session MCP skill delivery" in _TASK_START_DESCRIPTION
    assert "live skill injection" in _TASK_START_DESCRIPTION
    for text in (architecture, host, adr_0032, adr_0041, _TASK_START_DESCRIPTION):
        assert "live reload is a correctness requirement" not in text.casefold()
        assert "MCP delivers skill" not in text


def test_projection_after_task_start_tests_are_filesystem_not_mcp_delivery() -> None:
    source = (REPO_ROOT / "tests" / "test_skill_relevance_reconciliation.py").read_text(
        encoding="utf-8"
    )
    assert "watcher_invalidations" in source
    assert "skill_relevance_key" in source
    assert "ADR-0041" in source
    assert "recommended_skills" not in source
    assert "mcp_bridge" not in source
    assert "list_tools" not in source


def test_synthetic_gate_task_start_does_not_deliver_skill_x_through_mcp(
    tmp_path: Path,
) -> None:
    """Session already on the five-tool MCP surface; selecting X is next-session-only."""
    root, connection, workspace_id = _registered_workspace(tmp_path)
    registry = tmp_path / "skills"
    _write_skill_x(registry)
    definitions = load_skill_registry(registry)
    invalidations: SimpleQueue[str] = SimpleQueue()
    session_tools = tuple(_TOOL_ARGUMENTS)
    empty_start = _task_start_payload(
        TaskStartResult(
            schema_version=14,
            workspace_id=workspace_id,
            task_id="pre-session",
            state=TaskState.WORKING,
            wait_reason=None,
            revision=1,
        )
    )
    empty_status = _status_task_payload(
        WorkspaceTaskSummary(
            task_id="pre-session",
            title="pre",
            state=TaskState.WORKING,
            wait_reason=None,
            revision=1,
        )
    )
    try:
        assert session_tools == (
            "project_status",
            "project_search",
            "project_context",
            "task_start",
            "task_checkpoint",
        )
        _assert_no_skill_delivery(
            _mcp_session_snapshot(task_start=empty_start, current_task=empty_status)
        )
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(None, ())
        assert _ids(resolve_workspace_skills(connection, workspace_id, definitions)) != (
            _SKILL_X_ID,
        )

        started = mutate_task_start(
            connection,
            TaskStartRequestData(
                workspace_hints=(WorkspaceHint(root, "explicit-root"),),
                title="Select skill X",
                stack_hints=(_SKILL_X_HINT,),
                task_id=None,
                expected_revision=None,
            ),
            watcher_invalidations=invalidations,
        )
        task = get_task(connection, started.task_id)
        payload = _task_start_payload(started)
        status = _status_task_payload(
            WorkspaceTaskSummary(
                task_id=task.task_id,
                title=task.title,
                state=task.state,
                wait_reason=task.wait_reason,
                revision=task.revision,
            )
        )
        after = _mcp_session_snapshot(task_start=payload, current_task=status)
        _assert_no_skill_delivery(after)
        assert tuple(_TOOL_ARGUMENTS) == session_tools
        assert set(payload) == {
            "workspace_id",
            "task_id",
            "state",
            "wait_reason",
            "revision",
        }
        assert _SKILL_X_ID not in payload.values()
        assert _SKILL_X_BODY_MARKER not in json.dumps(payload)
        with pytest.raises(ProjectRetrievalRefError, match="kind is unsupported"):
            read_project_context(
                connection,
                workspace_id,
                (f"skill:{_SKILL_X_ID}",),
            )
        assert _drain(invalidations) == [workspace_id]
        assert skill_relevance_key(connection, workspace_id) == SkillRelevanceKey(
            started.task_id, (_SKILL_X_HINT,)
        )
        assert _ids(resolve_workspace_skills(connection, workspace_id, definitions)) == (
            _SKILL_X_ID,
        )
        assert SYNTHETIC_GATE_EXPECTED_RESULT == "next-session-only"
    finally:
        connection.close()
