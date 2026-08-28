from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from harness.index import get_indexed_file
from harness.knowledge import (
    KnowledgeCardRecord,
    KnowledgeError,
    KnowledgeFreshness,
    get_knowledge_card,
)
from harness.registry import get_project, get_workspace
from harness.search import (
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_QUERY_BYTES,
    IndexedPathSearchScope,
    SearchError,
    is_document_path,
    search_indexed_paths,
)
from harness.task_checkpoints import TaskCheckpointError, get_task_checkpoint, list_task_events
from harness.tasks import TaskNotFoundError, get_relevant_task, get_task, get_task_stack_hints
from harness.verification import list_checkpoint_verification

_DEFAULT_CANDIDATE_LIMIT = 96
_MAX_CANDIDATE_LIMIT = 384
_SUMMARY_MAX_BYTES = 384
_CONTEXT_HISTORY_LIMIT = 4
_CONTEXT_TEXT_MAX_BYTES = 1024
_CONTEXT_VERIFICATION_LIMIT = 4
_CONTEXT_VERIFICATION_EVIDENCE_MAX_BYTES = 512
_CONTEXT_KNOWLEDGE_ANCHOR_LIMIT = 4
_CONTEXT_KNOWLEDGE_ANCHOR_BYTES = 2048
_CONTEXT_STACK_HINT_LIMIT = 8
_CONTEXT_CHANGED_PATH_LIMIT = 16
_CONTEXT_CHANGED_PATH_BYTES = 2048
MAX_PROJECT_CONTEXT_REF_BYTES = 4096 + len("code:")
_QUERY_TOKEN_LIMIT = 24
_DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}


class ProjectRetrievalError(RuntimeError):
    """Base class for bounded Project Intelligence retrieval failures."""


class ProjectRetrievalRefError(ProjectRetrievalError):
    """Raised when a selected context ref is invalid or outside the active Project."""


class ProjectSearchScope(StrEnum):
    ALL = "all"
    CODE = "code"
    DOCS = "docs"
    KNOWLEDGE = "knowledge"
    TASKS = "tasks"


class ProjectSearchKind(StrEnum):
    CODE = "code"
    DOC = "doc"
    KNOWLEDGE = "knowledge"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class ProjectSearchHit:
    ref: str
    kind: ProjectSearchKind
    title: str
    location: str
    short_summary: str | None
    match_reason: str
    freshness: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectContextItem:
    ref: str
    kind: ProjectSearchKind
    data: dict[str, object]


def search_project(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    *,
    scope: ProjectSearchScope = ProjectSearchScope.ALL,
    limit: int,
) -> tuple[ProjectSearchHit, ...]:
    """Search bounded Project Intelligence while keeping filesystem search Workspace-local."""
    workspace = get_workspace(connection, workspace_id)
    project = get_project(connection, workspace.project_id)
    normalized = _normalize_query(query)
    _validate_limit(limit)
    if not isinstance(scope, ProjectSearchScope):
        raise SearchError("project search scope is unsupported")

    if scope is ProjectSearchScope.CODE:
        return _path_hits(connection, workspace_id, normalized, IndexedPathSearchScope.CODE, limit)
    if scope is ProjectSearchScope.DOCS:
        return _path_hits(connection, workspace_id, normalized, IndexedPathSearchScope.DOCS, limit)
    if scope is ProjectSearchScope.KNOWLEDGE:
        return _knowledge_hits(connection, project.project_id, normalized, limit)
    if scope is ProjectSearchScope.TASKS:
        return _task_hits(connection, project.project_id, workspace_id, normalized, limit)

    channels = (
        _path_hits(connection, workspace_id, normalized, IndexedPathSearchScope.CODE, limit),
        _path_hits(connection, workspace_id, normalized, IndexedPathSearchScope.DOCS, limit),
        _knowledge_hits(connection, project.project_id, normalized, limit),
        _task_hits(connection, project.project_id, workspace_id, normalized, limit),
    )
    fused: list[ProjectSearchHit] = []
    for rank in range(limit):
        for channel in channels:
            if rank < len(channel):
                fused.append(channel[rank])
                if len(fused) == limit:
                    return tuple(fused)
    return tuple(fused)


def read_project_context(
    connection: sqlite3.Connection,
    workspace_id: str,
    refs: tuple[str, ...],
) -> tuple[ProjectContextItem, ...]:
    """Expand only explicitly selected refs and fail closed on cross-Project identities."""
    workspace = get_workspace(connection, workspace_id)
    project = get_project(connection, workspace.project_id)
    items: list[ProjectContextItem] = []
    for ref in refs:
        _validate_ref(ref)
        if ref.startswith("code:") or ref.startswith("doc:"):
            kind_text, _, relative_path = ref.partition(":")
            entry = get_indexed_file(connection, workspace_id, relative_path)
            if entry is None:
                raise ProjectRetrievalRefError("selected Structural Index ref is not current")
            is_doc = is_document_path(entry.relative_path)
            if (kind_text == "doc") != is_doc:
                raise ProjectRetrievalRefError(
                    "selected Structural Index ref kind does not match path"
                )
            kind = ProjectSearchKind.DOC if is_doc else ProjectSearchKind.CODE
            items.append(
                ProjectContextItem(
                    ref=ref,
                    kind=kind,
                    data={
                        "path": entry.relative_path,
                        "entry_kind": entry.kind.value,
                        "size_bytes": entry.size_bytes,
                    },
                )
            )
            continue
        if ref.startswith("knowledge:"):
            knowledge_id = ref.removeprefix("knowledge:")
            try:
                card = get_knowledge_card(connection, knowledge_id)
            except KnowledgeError as exc:
                raise ProjectRetrievalRefError("selected Knowledge ref does not exist") from exc
            if card.project_id != project.project_id:
                raise ProjectRetrievalRefError("selected Knowledge ref belongs to another Project")
            items.append(
                ProjectContextItem(
                    ref=ref,
                    kind=ProjectSearchKind.KNOWLEDGE,
                    data=_knowledge_context_data(card),
                )
            )
            continue
        if ref.startswith("task:"):
            items.append(_task_context(connection, project.project_id, ref))
            continue
        raise ProjectRetrievalRefError("selected Project context ref kind is unsupported")
    return tuple(items)


def _path_hits(
    connection: sqlite3.Connection,
    workspace_id: str,
    query: str,
    scope: IndexedPathSearchScope,
    limit: int,
) -> tuple[ProjectSearchHit, ...]:
    results = search_indexed_paths(connection, workspace_id, query, limit=limit, scope=scope)
    hits: list[ProjectSearchHit] = []
    for result in results:
        is_doc = is_document_path(result.relative_path)
        kind = ProjectSearchKind.DOC if is_doc else ProjectSearchKind.CODE
        prefix = "doc" if is_doc else "code"
        hits.append(
            ProjectSearchHit(
                ref=f"{prefix}:{result.relative_path}",
                kind=kind,
                title=Path(result.relative_path).name,
                location=result.relative_path,
                short_summary=None,
                match_reason=result.match_kind.value,
                freshness="indexed_snapshot",
                path=result.relative_path,
            )
        )
    return tuple(hits)


def _knowledge_hits(
    connection: sqlite3.Connection,
    project_id: str,
    query: str,
    limit: int,
) -> tuple[ProjectSearchHit, ...]:
    match_query = _fts_query(query)
    candidate_limit = _candidate_limit(limit)
    rows = connection.execute(
        """
        SELECT knowledge_id, bm25(knowledge_search, 0.0, 0.0, 5.0, 1.0) AS score
        FROM knowledge_search
        WHERE knowledge_search MATCH ? AND project_id = ?
        ORDER BY score, knowledge_id
        LIMIT ?
        """,
        (match_query, project_id, candidate_limit),
    ).fetchall()
    ranked: list[tuple[int, float, str, ProjectSearchHit]] = []
    seen: set[str] = set()
    for knowledge_id, raw_score in rows:
        if not isinstance(knowledge_id, str) or not isinstance(raw_score, (int, float)):
            raise ProjectRetrievalError("Knowledge search index returned invalid persisted types")
        if knowledge_id in seen:
            continue
        seen.add(knowledge_id)
        card = get_knowledge_card(connection, knowledge_id)
        if card.project_id != project_id:
            raise ProjectRetrievalError("Knowledge search index crossed Project ownership")
        stale = card.freshness is KnowledgeFreshness.NEEDS_REVALIDATION
        location = card.anchors[0].relative_path if card.anchors else f"project:{project_id[:12]}"
        ranked.append(
            (
                1 if stale else 0,
                float(raw_score),
                card.knowledge_id,
                ProjectSearchHit(
                    ref=f"knowledge:{card.knowledge_id}",
                    kind=ProjectSearchKind.KNOWLEDGE,
                    title=card.title,
                    location=location,
                    short_summary=_truncate_utf8(card.body, _SUMMARY_MAX_BYTES),
                    match_reason="semantic Knowledge title/body",
                    freshness=card.freshness.value,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in ranked[:limit])


def _task_hits(
    connection: sqlite3.Connection,
    project_id: str,
    active_workspace_id: str,
    query: str,
    limit: int,
) -> tuple[ProjectSearchHit, ...]:
    match_query = _fts_query(query)
    rows = connection.execute(
        """
        SELECT
            fragment_ref,
            task_id,
            workspace_id,
            bm25(task_search, 0.0, 0.0, 0.0, 0.0, 5.0, 1.0) AS score
        FROM task_search
        WHERE task_search MATCH ? AND project_id = ?
        ORDER BY score, rowid
        LIMIT ?
        """,
        (match_query, project_id, _candidate_limit(limit)),
    ).fetchall()
    current = get_relevant_task(connection, active_workspace_id)
    best: dict[str, tuple[str, str, float]] = {}
    for fragment_ref, task_id, workspace_id, raw_score in rows:
        if (
            not isinstance(fragment_ref, str)
            or not isinstance(task_id, str)
            or not isinstance(workspace_id, str)
            or not isinstance(raw_score, (int, float))
        ):
            raise ProjectRetrievalError("Task search index returned invalid persisted types")
        best.setdefault(task_id, (fragment_ref, workspace_id, float(raw_score)))

    ranked: list[tuple[int, float, str, ProjectSearchHit]] = []
    for task_id, (fragment_ref, workspace_id, score) in best.items():
        task = get_task(connection, task_id)
        owner = get_workspace(connection, task.workspace_id)
        if owner.project_id != project_id or workspace_id != task.workspace_id:
            raise ProjectRetrievalError("Task search index crossed Project ownership")
        result_ref, reason, summary, location = _task_fragment_projection(
            connection, task_id, fragment_ref
        )
        ranked.append(
            (
                0 if current is not None and current.task_id == task_id else 1,
                score,
                task_id,
                ProjectSearchHit(
                    ref=result_ref,
                    kind=ProjectSearchKind.TASK,
                    title=task.title,
                    location=location,
                    short_summary=summary,
                    match_reason=reason,
                    freshness="durable_history",
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple(item[3] for item in ranked[:limit])


def _task_fragment_projection(
    connection: sqlite3.Connection,
    task_id: str,
    fragment_ref: str,
) -> tuple[str, str, str | None, str]:
    if fragment_ref == f"task:{task_id}":
        row = connection.execute(
            """
            SELECT summary
            FROM task_checkpoints
            WHERE task_id = ?
            ORDER BY task_revision DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        summary = None if row is None else _require_text(row[0], "Task checkpoint summary")
        return (
            f"task:{task_id}",
            "Task title",
            None if summary is None else _truncate_utf8(summary, _SUMMARY_MAX_BYTES),
            f"task:{task_id}",
        )
    if fragment_ref == f"meta:{task_id}":
        task = get_task(connection, task_id)
        values = [value for value in (task.jira_url, task.operator_status) if value is not None]
        summary = " · ".join(str(value) for value in values) or None
        return (
            f"task:{task_id}",
            "Task Jira link/operator status",
            summary,
            task.jira_url or f"task:{task_id}",
        )
    if fragment_ref == f"baseline:{task_id}":
        row = connection.execute(
            "SELECT branch FROM task_baselines WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None or (row[0] is not None and not isinstance(row[0], str)):
            raise ProjectRetrievalError("Task search baseline ownership mismatch")
        branch = row[0]
        return (
            f"task:{task_id}",
            "Task Git branch",
            branch,
            f"branch:{branch}" if branch is not None else f"task:{task_id}",
        )
    if fragment_ref.startswith("checkpoint:"):
        checkpoint_id = fragment_ref.removeprefix("checkpoint:")
        checkpoint = get_task_checkpoint(connection, checkpoint_id)
        if checkpoint.task_id != task_id:
            raise ProjectRetrievalError("Task search checkpoint ownership mismatch")
        return (
            f"task:{task_id}#checkpoint:{checkpoint_id}",
            "Task checkpoint summary/next step/branch",
            _truncate_utf8(checkpoint.summary, _SUMMARY_MAX_BYTES),
            f"task:{task_id} · revision {checkpoint.task_revision}",
        )
    if fragment_ref.startswith("event:"):
        event_id = _parse_positive_int(fragment_ref.removeprefix("event:"), "Task event id")
        row = connection.execute(
            """
            SELECT task_id, task_revision, event_type, operator_feedback,
                   operator_comment, jira_url, operator_status
            FROM task_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if (
            row is None
            or row[0] != task_id
            or row[2]
            not in {
                "operator_feedback",
                "operator_comment",
                "jira_link_updated",
                "operator_status_updated",
            }
        ):
            raise ProjectRetrievalError("Task search event ownership mismatch")
        event_type = _require_text(row[2], "Task event type")
        raw_summary = next((value for value in row[3:] if isinstance(value, str)), None)
        if raw_summary is None:
            raise ProjectRetrievalError("Task search event has no searchable payload")
        revision = row[1]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ProjectRetrievalError("Task search event revision is invalid")
        return (
            f"task:{task_id}#event:{event_id}",
            event_type.replace("_", " "),
            _truncate_utf8(raw_summary, _SUMMARY_MAX_BYTES),
            f"task:{task_id} · revision {revision}",
        )
    raise ProjectRetrievalError("Task search fragment ref is invalid")


def _task_context(
    connection: sqlite3.Connection,
    project_id: str,
    ref: str,
) -> ProjectContextItem:
    task_part, separator, fragment = ref.partition("#")
    task_id = task_part.removeprefix("task:")
    try:
        task = get_task(connection, task_id)
    except TaskNotFoundError as exc:
        raise ProjectRetrievalRefError("selected Task ref does not exist") from exc
    workspace = get_workspace(connection, task.workspace_id)
    if workspace.project_id != project_id:
        raise ProjectRetrievalRefError("selected Task ref belongs to another Project")

    stack_hints = get_task_stack_hints(connection, task.task_id)
    data: dict[str, object] = {
        "task_id": task.task_id,
        "workspace_id": task.workspace_id,
        "title": task.title,
        "state": task.state.value,
        "wait_reason": None if task.wait_reason is None else task.wait_reason.value,
        "jira_url": task.jira_url,
        "operator_status": (None if task.operator_status is None else task.operator_status.value),
        "revision": task.revision,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "stack_hints": list(stack_hints[:_CONTEXT_STACK_HINT_LIMIT]),
        "stack_hint_count": len(stack_hints),
        "stack_hints_truncated": len(stack_hints) > _CONTEXT_STACK_HINT_LIMIT,
    }
    if not separator:
        history: list[dict[str, object]] = []
        for event in list_task_events(connection, task.task_id, limit=_CONTEXT_HISTORY_LIMIT):
            item: dict[str, object] = {
                "event_type": event.event_type.value,
                "task_revision": event.task_revision,
                "created_at": event.created_at,
            }
            if event.checkpoint_id is not None:
                try:
                    checkpoint = get_task_checkpoint(connection, event.checkpoint_id)
                except TaskCheckpointError as exc:
                    raise ProjectRetrievalRefError(
                        "selected Task history checkpoint does not exist"
                    ) from exc
                item["summary"] = _truncate_utf8(checkpoint.summary, _CONTEXT_TEXT_MAX_BYTES)
                item["next_step"] = (
                    None
                    if checkpoint.next_step is None
                    else _truncate_utf8(checkpoint.next_step, _CONTEXT_TEXT_MAX_BYTES)
                )
                verification = list_checkpoint_verification(connection, checkpoint.checkpoint_id)
                item["verification"] = [
                    {"name": record.name, "status": record.status.value}
                    for record in verification[:_CONTEXT_VERIFICATION_LIMIT]
                ]
                item["verification_count"] = len(verification)
                item["verification_truncated"] = len(verification) > _CONTEXT_VERIFICATION_LIMIT
            if event.operator_feedback is not None:
                item["operator_feedback"] = _truncate_utf8(
                    event.operator_feedback, _CONTEXT_TEXT_MAX_BYTES
                )
            if event.operator_comment is not None:
                item["operator_comment"] = _truncate_utf8(
                    event.operator_comment, _CONTEXT_TEXT_MAX_BYTES
                )
            if event.jira_url is not None:
                item["jira_url"] = _truncate_utf8(event.jira_url, _CONTEXT_TEXT_MAX_BYTES)
            if event.operator_status is not None:
                item["operator_status"] = event.operator_status.value
            history.append(item)
        data["recent_history"] = history
    elif fragment.startswith("checkpoint:"):
        checkpoint_id = fragment.removeprefix("checkpoint:")
        try:
            checkpoint = get_task_checkpoint(connection, checkpoint_id)
        except TaskCheckpointError as exc:
            raise ProjectRetrievalRefError("selected Task checkpoint ref does not exist") from exc
        if checkpoint.task_id != task.task_id:
            raise ProjectRetrievalRefError("selected Task checkpoint ref does not belong to Task")
        changed_paths = _bounded_paths(checkpoint.changed_paths)
        summary = _truncate_utf8(checkpoint.summary, _CONTEXT_TEXT_MAX_BYTES)
        next_step = (
            None
            if checkpoint.next_step is None
            else _truncate_utf8(checkpoint.next_step, _CONTEXT_TEXT_MAX_BYTES)
        )
        verification_records = list_checkpoint_verification(connection, checkpoint.checkpoint_id)
        data["selected_checkpoint"] = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "task_revision": checkpoint.task_revision,
            "state": checkpoint.state.value,
            "wait_reason": None if checkpoint.wait_reason is None else checkpoint.wait_reason.value,
            "summary": summary,
            "summary_truncated": summary != checkpoint.summary,
            "next_step": next_step,
            "next_step_truncated": next_step != checkpoint.next_step,
            "created_at": checkpoint.created_at,
            "changed_paths": list(changed_paths),
            "changed_path_count": len(checkpoint.changed_paths),
            "changed_paths_truncated": len(changed_paths) < len(checkpoint.changed_paths),
            "verification": [
                {
                    "name": record.name,
                    "status": record.status.value,
                    "evidence": _truncate_utf8(
                        record.evidence, _CONTEXT_VERIFICATION_EVIDENCE_MAX_BYTES
                    ),
                    "evidence_truncated": (
                        len(record.evidence.encode("utf-8"))
                        > _CONTEXT_VERIFICATION_EVIDENCE_MAX_BYTES
                    ),
                    "source": record.source.value,
                }
                for record in verification_records[:_CONTEXT_VERIFICATION_LIMIT]
            ],
            "verification_count": len(verification_records),
            "verification_truncated": len(verification_records) > _CONTEXT_VERIFICATION_LIMIT,
        }
    elif fragment.startswith("event:"):
        event_id = _parse_positive_int(fragment.removeprefix("event:"), "selected Task event id")
        row = connection.execute(
            """
            SELECT task_id, task_revision, event_type, operator_feedback,
                   operator_comment, jira_url, operator_status, created_at
            FROM task_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None or row[0] != task.task_id:
            raise ProjectRetrievalRefError("selected Task event ref does not belong to Task")
        event_type = _require_text(row[2], "selected Task event type")
        payload_names = (
            ("operator_feedback", row[3]),
            ("operator_comment", row[4]),
            ("jira_url", row[5]),
            ("operator_status", row[6]),
        )
        payload = next(
            ((name, value) for name, value in payload_names if isinstance(value, str)),
            None,
        )
        if (
            event_type
            not in {
                "operator_feedback",
                "operator_comment",
                "jira_link_updated",
                "operator_status_updated",
            }
            or payload is None
        ):
            raise ProjectRetrievalRefError("selected Task event ref is not searchable history")
        payload_name, payload_value = payload
        selected_event: dict[str, object] = {
            "event_id": event_id,
            "task_revision": row[1],
            "event_type": event_type,
            payload_name: _truncate_utf8(payload_value, _CONTEXT_TEXT_MAX_BYTES),
            f"{payload_name}_truncated": (
                len(payload_value.encode("utf-8")) > _CONTEXT_TEXT_MAX_BYTES
            ),
            "created_at": row[7],
        }
        data["selected_event"] = selected_event
    else:
        raise ProjectRetrievalRefError("selected Task fragment ref is unsupported")
    return ProjectContextItem(ref=ref, kind=ProjectSearchKind.TASK, data=data)


def _knowledge_context_data(card: KnowledgeCardRecord) -> dict[str, object]:
    body = _truncate_utf8(card.body, _CONTEXT_TEXT_MAX_BYTES)
    anchors: list[dict[str, object]] = []
    anchor_bytes = 0
    for anchor in card.anchors[:_CONTEXT_KNOWLEDGE_ANCHOR_LIMIT]:
        fields = (anchor.workspace_id, anchor.relative_path, anchor.symbol or "")
        size = sum(len(value.encode("utf-8")) for value in fields)
        if anchor_bytes + size > _CONTEXT_KNOWLEDGE_ANCHOR_BYTES:
            break
        anchors.append(
            {
                "workspace_id": anchor.workspace_id,
                "path": anchor.relative_path,
                "symbol": anchor.symbol,
            }
        )
        anchor_bytes += size
    return {
        "knowledge_id": card.knowledge_id,
        "knowledge_kind": card.kind.value,
        "title": card.title,
        "body": body,
        "body_truncated": body != card.body,
        "freshness": card.freshness.value,
        "historical_clue": card.freshness is KnowledgeFreshness.NEEDS_REVALIDATION,
        "source_type": card.source_type.value,
        "source_task_id": card.source_task_id,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
        "anchors": anchors,
        "anchor_count": len(card.anchors),
        "anchors_truncated": len(anchors) < len(card.anchors),
    }


def _bounded_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    bounded: list[str] = []
    used_bytes = 0
    for path in paths[:_CONTEXT_CHANGED_PATH_LIMIT]:
        size = len(path.encode("utf-8"))
        if used_bytes + size > _CONTEXT_CHANGED_PATH_BYTES:
            break
        bounded.append(path)
        used_bytes += size
    return tuple(bounded)


def _normalize_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        raise SearchError("project search query must be non-empty text")
    normalized = query.strip()
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SearchError("project search query must be valid UTF-8 text") from exc
    if size > MAX_SEARCH_QUERY_BYTES:
        raise SearchError(f"project search query exceeds {MAX_SEARCH_QUERY_BYTES} UTF-8 bytes")
    return normalized


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise SearchError(f"project search limit must be between 1 and {MAX_SEARCH_LIMIT}")


def _fts_query(query: str) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"\w+", query.casefold(), flags=re.UNICODE):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) == _QUERY_TOKEN_LIMIT:
            break
    if not tokens:
        raise SearchError("project search query has no searchable tokens")
    return " OR ".join(f'"{token}"' for token in tokens)


def _candidate_limit(limit: int) -> int:
    return min(_MAX_CANDIDATE_LIMIT, max(_DEFAULT_CANDIDATE_LIMIT, limit * 24))


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


def _validate_ref(ref: str) -> None:
    if not isinstance(ref, str) or not ref or "\x00" in ref:
        raise ProjectRetrievalRefError("Project context ref must be non-empty text")
    try:
        size = len(ref.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ProjectRetrievalRefError("Project context ref must be valid UTF-8") from exc
    if size > MAX_PROJECT_CONTEXT_REF_BYTES:
        raise ProjectRetrievalRefError("Project context ref exceeds byte limit")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectRetrievalError(f"{label} has invalid persisted text")
    return value


def _parse_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ProjectRetrievalRefError(f"{label} is invalid") from exc
    if parsed <= 0:
        raise ProjectRetrievalRefError(f"{label} is invalid")
    return parsed
