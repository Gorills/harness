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


def _seed_project_intelligence(tmp_path: Path) -> tuple[Path, Path, str, str, str, str]:
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
            knowledge=(
                _knowledge(
                    "Refresh rotation current invariant",
                    "Current replacement invalidates the previous token transactionally.",
                ),
            ),
        )
        current_checkpoint_id = current.checkpoint.checkpoint_id

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
            knowledge=(
                _knowledge(
                    "Refresh rotation invariant SECRET_OTHER_PROJECT",
                    "SECRET_OTHER_PROJECT must never cross Project retrieval boundaries.",
                ),
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
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_real_mcp_searches_and_expands_project_knowledge_and_task_history(
    tmp_path: Path,
) -> None:
    (
        root,
        database,
        task_id,
        legacy_knowledge_id,
        current_checkpoint_id,
        other_knowledge_id,
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
            knowledge = await client.call_tool(
                "project_search",
                {"query": "refresh rotation invariant", "scope": "knowledge", "limit": 5},
            )
            assert knowledge.is_error is False
            assert knowledge.structured_content is not None
            knowledge_results = knowledge.structured_content["results"]
            assert [item["kind"] for item in knowledge_results] == ["knowledge", "knowledge"]
            assert knowledge_results[0]["freshness"] == "fresh"
            assert knowledge_results[1]["freshness"] == "needs_revalidation"
            assert {item["ref"] for item in knowledge_results} >= {
                f"knowledge:{legacy_knowledge_id}"
            }
            assert "SECRET_OTHER_PROJECT" not in json.dumps(
                knowledge.structured_content, sort_keys=True
            )

            tasks = await client.call_tool(
                "project_search",
                {
                    "query": "transactional replacement enforced",
                    "scope": "tasks",
                    "limit": 3,
                },
            )
            assert tasks.is_error is False
            assert tasks.structured_content is not None
            task_results = tasks.structured_content["results"]
            assert task_results[0]["ref"] == (f"task:{task_id}#checkpoint:{current_checkpoint_id}")
            assert task_results[0]["kind"] == "task"
            assert "SECRET_OTHER_PROJECT" not in json.dumps(
                tasks.structured_content, sort_keys=True
            )

            code = await client.call_tool(
                "project_search",
                {
                    "query": "where previous credential invalidation happens",
                    "scope": "code",
                    "limit": 3,
                },
            )
            assert code.is_error is False
            assert code.structured_content is not None
            assert code.structured_content["results"][0]["ref"] == ("code:src/token_service.py")
            assert code.structured_content["results"][0]["short_summary"] is None
            assert "replace_and_invalidate" not in json.dumps(
                code.structured_content, sort_keys=True
            )

            docs = await client.call_tool(
                "project_search", {"query": "rotation", "scope": "docs", "limit": 2}
            )
            assert docs.is_error is False
            assert docs.structured_content is not None
            assert docs.structured_content["results"][0]["ref"] == "doc:docs/rotation.md"
            assert docs.structured_content["results"][0]["kind"] == "doc"

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
