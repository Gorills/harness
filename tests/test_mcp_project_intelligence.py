from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from harness.daemon import serve_daemon
from harness.index import scan_workspace
from harness.knowledge import KnowledgeAnchorDraft, KnowledgeDraft, KnowledgeKind
from harness.registry import create_project, register_workspace
from harness.storage import connect_database, initialize_database
from harness.task_workflow import task_checkpoint, task_start
from harness.tasks import TaskState

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX MCP/IPC slice")


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "token_service.py").write_text(
        "VERSION = 1\n\ndef rotateRefreshToken():\n    return 'previous credential'\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir()
    (root / "docs" / "rotation.md").write_text("Repository rotation notes\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=h@example.invalid",
        "commit",
        "-m",
        "init",
    )


def _start_daemon(
    database: Path, socket_path: Path
) -> tuple[Event, ThreadPoolExecutor, Future[None]]:
    stop = Event()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(serve_daemon, database, socket_path, stop_event=stop)
    deadline = time.monotonic() + 3
    while not socket_path.exists():
        if future.done():
            future.result()
        if time.monotonic() >= deadline:
            raise AssertionError("daemon did not start")
        time.sleep(0.01)
    return stop, executor, future


def _knowledge(title: str, body: str) -> KnowledgeDraft:
    return KnowledgeDraft(
        kind=KnowledgeKind.INVARIANT,
        title=title,
        body=body,
        anchors=(KnowledgeAnchorDraft(path="src/token_service.py"),),
    )


def _seed_project_intelligence(tmp_path: Path) -> tuple[Path, Path, str, str, str, str, str, str]:
    active_root = tmp_path / "active"
    other_root = tmp_path / "other"
    _init_repo(active_root)
    _init_repo(other_root)
    database = tmp_path / "harness.db"
    initialize_database(database)
    connection = connect_database(database)
    try:
        active_project = create_project(connection)
        active_workspace = register_workspace(
            connection, project_id=active_project.project_id, path=active_root
        )
        scan_workspace(connection, active_workspace.workspace_id)

        task = task_start(connection, active_workspace.workspace_id, "Token hardening")
        legacy = task_checkpoint(
            connection,
            active_workspace.workspace_id,
            task.task_id,
            expected_revision=task.revision,
            state=TaskState.WORKING,
            summary="Mapped the legacy rotation path",
            knowledge=(
                _knowledge(
                    "Refresh rotation legacy invariant",
                    "Legacy rotation invalidates the previous token after replacement.",
                ),
            ),
        )
        legacy_knowledge_id = legacy.knowledge_cards[0].knowledge_id

        (active_root / "src" / "token_service.py").write_text(
            """
VERSION = 2

def rotateRefreshToken(repository, previous_credential):
    return repository.replace_and_invalidate(previous_credential)
""".lstrip(),
            encoding="utf-8",
        )
        scan_workspace(connection, active_workspace.workspace_id)

        current = task_checkpoint(
            connection,
            active_workspace.workspace_id,
            task.task_id,
            expected_revision=legacy.task.revision,
            state=TaskState.WORKING,
            summary="Transactional replacement now enforced",
            next_step="Проверить управление задачами.",
            knowledge=(
                _knowledge(
                    "Refresh rotation current invariant",
                    "Current replacement invalidates the previous token transactionally.",
                ),
                _knowledge(
                    "Управление задачами",
                    "Состояние работы сохраняется между сессиями.",
                ),
            ),
        )
        current_checkpoint_id = current.checkpoint.checkpoint_id
        russian_knowledge_id = current.knowledge_cards[1].knowledge_id

        other_project = create_project(connection)
        other_workspace = register_workspace(
            connection, project_id=other_project.project_id, path=other_root
        )
        scan_workspace(connection, other_workspace.workspace_id)
        other_task = task_start(connection, other_workspace.workspace_id, "Other project secret")
        other = task_checkpoint(
            connection,
            other_workspace.workspace_id,
            other_task.task_id,
            expected_revision=other_task.revision,
            state=TaskState.WORKING,
            summary="Transactional replacement SECRET_OTHER_PROJECT",
            next_step="Проверить управление задачами SECRET_OTHER_PROJECT.",
            knowledge=(
                _knowledge(
                    "Refresh rotation invariant SECRET_OTHER_PROJECT",
                    "SECRET_OTHER_PROJECT must never cross Project retrieval boundaries.",
                ),
                _knowledge("Управление задачами", "SECRET_OTHER_PROJECT не должен раскрываться."),
            ),
        )
        other_knowledge_id = other.knowledge_cards[0].knowledge_id
        return (
            active_root,
            database,
            task.task_id,
            legacy_knowledge_id,
            current_checkpoint_id,
            other_knowledge_id,
            other_task.task_id,
            russian_knowledge_id,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_real_mcp_expands_selected_project_knowledge_and_task_history(
    tmp_path: Path,
) -> None:
    (
        root,
        database,
        task_id,
        legacy_knowledge_id,
        current_checkpoint_id,
        other_knowledge_id,
        other_task_id,
        russian_knowledge_id,
    ) = _seed_project_intelligence(tmp_path)
    runtime = tmp_path / "runtime"
    socket_path = runtime / "harness" / "harness.sock"
    stop, executor, future = _start_daemon(database, socket_path)
    env = dict(os.environ)
    env.update(
        {
            "XDG_RUNTIME_DIR": str(runtime),
            "HARNESS_WORKSPACE_ROOT": str(root),
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_process"],
        env=env,
        cwd=str(root),
    )
    try:
        async with Client(stdio_client(params)) as client:
            unavailable = await client.call_tool(
                "project_context", {"refs": [f"knowledge:{legacy_knowledge_id}"]}
            )
            assert unavailable.is_error is True
            assert "Legacy rotation invalidates" not in str(unavailable.content)

            selected = await client.call_tool(
                "project_context",
                {
                    "refs": [
                        f"knowledge:{russian_knowledge_id}",
                        f"task:{task_id}#checkpoint:{current_checkpoint_id}",
                        "code:src/token_service.py",
                    ]
                },
            )
            assert selected.is_error is False
            assert selected.structured_content is not None
            assert [item["ref"] for item in selected.structured_content["items"]] == [
                f"knowledge:{russian_knowledge_id}",
                f"task:{task_id}#checkpoint:{current_checkpoint_id}",
                "code:src/token_service.py",
            ]
            assert "SECRET_OTHER_PROJECT" not in json.dumps(
                selected.structured_content, sort_keys=True
            )
            for ref in (f"knowledge:{other_knowledge_id}", f"task:{other_task_id}"):
                rejected = await client.call_tool("project_context", {"refs": [ref]})
                assert rejected.is_error is True
                assert "SECRET_OTHER_PROJECT" not in str(rejected.content)

            recalled = await client.call_tool(
                "project_recall", {"query": "refresh rotation", "kind": "knowledge"}
            )
            assert recalled.is_error is False
            assert recalled.structured_content is not None
            assert set(recalled.structured_content) == {
                "query",
                "kind",
                "results_truncated",
                "results",
            }
            assert recalled.structured_content["kind"] == "knowledge"
            knowledge_hits = recalled.structured_content["results"]
            assert len(knowledge_hits) == 1
            assert knowledge_hits[0]["title"] == "Refresh rotation current invariant"
            assert knowledge_hits[0]["ref"] != f"knowledge:{legacy_knowledge_id}"
            assert all(
                set(hit) == {"ref", "title", "short_summary", "freshness"} for hit in knowledge_hits
            )
            assert "Legacy rotation invalidates" not in json.dumps(recalled.structured_content)
            assert "SECRET_OTHER_PROJECT" not in json.dumps(recalled.structured_content)
            assert "src/token_service.py" not in json.dumps(recalled.structured_content)

            # The highest-ranked title match is hidden on this Git snapshot. A lower
            # body match must still fill limit=1 after applicability filtering.
            connection = connect_database(database)
            try:
                connection.execute(
                    """UPDATE knowledge_cards
                    SET title = 'Current invariant', body = 'refresh rotation still applies'
                    WHERE source_checkpoint_id = ?
                      AND title = 'Refresh rotation current invariant'""",
                    (current_checkpoint_id,),
                )
            finally:
                connection.close()
            visible_after_filter = await client.call_tool(
                "project_recall",
                {"query": "refresh rotation", "kind": "knowledge", "limit": 1},
            )
            assert visible_after_filter.is_error is False
            assert visible_after_filter.structured_content is not None
            assert [hit["title"] for hit in visible_after_filter.structured_content["results"]] == [
                "Current invariant"
            ]

            task_recall = await client.call_tool(
                "project_recall", {"query": "Token hardening", "kind": "task"}
            )
            assert task_recall.is_error is False
            assert task_recall.structured_content is not None
            assert len(task_recall.structured_content["results"]) == 1
            assert task_recall.structured_content["results"][0]["ref"].startswith(f"task:{task_id}")
            assert "SECRET_OTHER_PROJECT" not in json.dumps(task_recall.structured_content)

            other_task_recall = await client.call_tool(
                "project_recall", {"query": "SECRET_OTHER_PROJECT", "kind": "task"}
            )
            assert other_task_recall.is_error is False
            assert other_task_recall.structured_content is not None
            assert other_task_recall.structured_content["results"] == []
            assert "Other project secret" not in json.dumps(other_task_recall.structured_content)

            unavailable_recall = await client.call_tool(
                "project_recall", {"query": "SECRET_OTHER_PROJECT", "kind": "knowledge"}
            )
            assert unavailable_recall.is_error is False
            assert unavailable_recall.structured_content == {
                "query": "SECRET_OTHER_PROJECT",
                "kind": "knowledge",
                "results_truncated": False,
                "results": [],
            }

            # Matching source makes the old card applicable again, while its durable
            # needs_revalidation label remains unchanged rather than being auto-refreshed.
            _git(root, "restore", "--", "src/token_service.py")
            context = await client.call_tool(
                "project_context",
                {
                    "refs": [
                        f"knowledge:{legacy_knowledge_id}",
                        f"task:{task_id}#checkpoint:{current_checkpoint_id}",
                        "doc:docs/rotation.md",
                    ]
                },
            )
            assert context.is_error is False
            assert context.structured_content is not None
            items = context.structured_content["items"]
            assert items[0]["historical_clue"] is True
            assert items[0]["freshness"] == "needs_revalidation"
            assert items[1]["selected_checkpoint"]["summary"] == (
                "Transactional replacement now enforced"
            )
            assert items[2] == {
                "ref": "doc:docs/rotation.md",
                "kind": "doc",
                "title": "rotation.md",
                "location": "docs/rotation.md",
                "path": "docs/rotation.md",
                "entry_kind": "file",
                "size_bytes": (root / "docs" / "rotation.md").stat().st_size,
                "freshness": "indexed_snapshot",
            }
            assert "SECRET_OTHER_PROJECT" not in json.dumps(
                context.structured_content, sort_keys=True
            )

            rejected = await client.call_tool(
                "project_context", {"refs": [f"knowledge:{other_knowledge_id}"]}
            )
            assert rejected.is_error is True
            assert "SECRET_OTHER_PROJECT" not in str(rejected.content)
    finally:
        stop.set()
        executor.shutdown(wait=True)
        future.result()
