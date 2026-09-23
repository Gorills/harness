from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from harness.git_applicability import WorkspaceApplicability
from harness.index import (
    get_indexed_file,
)
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
    SearchError,
)
from harness.search_text import (
    AnalyzedSearchQuery,
    analyze_search_query,
    contains_russian_case_phrase,
    contains_term_phrase,
    is_document_path,
    matching_term_count,
)
from harness.task_checkpoints import TaskCheckpointError, get_task_checkpoint, list_task_events
from harness.tasks import (
    TaskNotFoundError,
    TaskOperatorStatus,
    get_relevant_task,
    get_task,
    get_task_stack_hints,
)
from harness.verification import list_checkpoint_verification

_DEFAULT_CANDIDATE_LIMIT = 96
_MAX_CANDIDATE_LIMIT = 384
_KNOWLEDGE_RECALL_DEADLINE_SECONDS = 4.0
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

_QUALITY_TITLE_OR_IDENTIFIER_PHRASE = 4
_QUALITY_RUSSIAN_CASE_PHRASE = 5
_QUALITY_ALL_TERMS = 7
_QUALITY_PARTIAL = 8
_QUALITY_EXACT_TASK_ID = 0
_QUALITY_TASK_ID_PREFIX = 1


class ProjectRetrievalError(RuntimeError):
    """Base class for bounded Project Intelligence retrieval failures."""


class ProjectRetrievalRefError(ProjectRetrievalError):
    """Raised when a selected context ref is invalid or outside the active Project."""


class ProjectRecallDeadlineError(ProjectRetrievalError):
    """Raised when authoritative Knowledge cannot be fully scanned in time."""


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


@dataclass(frozen=True, slots=True)
class ProjectContextItem:
    ref: str
    kind: ProjectSearchKind
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class _RankedProjectHit:
    hit: ProjectSearchHit
    quality: int
    matched_terms: int
    lexical_score: float
    relevance_boost: int = 0


def search_tasks(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    project_id: str | None = None,
    applicability: WorkspaceApplicability | None = None,
) -> tuple[ProjectSearchHit, ...]:
    """Search durable Task history, optionally filtered to the active Git context."""
    normalized = _normalize_query(query)
    analyzed = analyze_search_query(normalized)
    if not analyzed.terms:
        raise SearchError("Task search query has no searchable tokens")
    _validate_limit(limit)
    if applicability is not None and applicability.workspace.project_id != project_id:
        raise ProjectRetrievalError("Task recall Project does not match active Workspace")
    return _project_hits(
        _task_hits(
            connection,
            analyzed,
            limit,
            project_id=project_id,
            active_workspace_id=(None if applicability is None else applicability.workspace_id),
            applicability=applicability,
        )
    )


def recall_knowledge(
    connection: sqlite3.Connection,
    project_id: str,
    query: str,
    *,
    limit: int,
    applicability: WorkspaceApplicability,
) -> tuple[ProjectSearchHit, ...]:
    """Find durable Knowledge from authoritative cards in the active Git context."""
    if applicability.workspace.project_id != project_id:
        raise ProjectRetrievalError("Knowledge recall Project does not match active Workspace")
    analyzed = analyze_search_query(_normalize_query(query))
    if not analyzed.terms:
        raise SearchError("Knowledge recall query has no searchable tokens")
    _validate_limit(limit)
    deadline = monotonic() + _KNOWLEDGE_RECALL_DEADLINE_SECONDS
    # Stream every authoritative card. A fixed newest-card window silently hides older
    # Knowledge, so a deadline failure must be explicit instead of returning partial hits.
    candidates = connection.execute(
        "SELECT id, title, body FROM knowledge_cards WHERE project_id = ? "
        "ORDER BY created_at DESC, id DESC",
        (project_id,),
    )
    ranked: list[tuple[tuple[int, int, str], ProjectSearchHit]] = []
    for knowledge_id, title, body in candidates:
        if monotonic() >= deadline:
            raise ProjectRecallDeadlineError("Knowledge recall deadline exceeded")
        if not all(isinstance(value, str) for value in (knowledge_id, title, body)):
            raise ProjectRetrievalError("Knowledge recall candidate has invalid persisted types")
        matched = matching_term_count(analyzed.terms, title, body)
        if not matched:
            continue
        title_match = contains_term_phrase(analyzed.terms, title) or contains_russian_case_phrase(
            analyzed.terms, title
        )
        rank = (0 if title_match else 1, len(analyzed.terms) - matched, knowledge_id)
        if len(ranked) >= limit and rank >= ranked[-1][0]:
            continue
        card = get_knowledge_card(connection, knowledge_id)
        if card.project_id != project_id:
            raise ProjectRetrievalError("Knowledge recall crossed Project ownership")
        if not applicability.knowledge_visible(card):
            continue
        ranked.append(
            (
                rank,
                ProjectSearchHit(
                    ref=f"knowledge:{card.knowledge_id}",
                    kind=ProjectSearchKind.KNOWLEDGE,
                    title=card.title,
                    location=f"knowledge:{card.knowledge_id}",
                    short_summary=None,
                    match_reason="durable Knowledge",
                    freshness=card.freshness.value,
                ),
            )
        )
        ranked.sort(key=lambda item: item[0])
        if len(ranked) > limit:
            ranked.pop()
    if monotonic() >= deadline:
        raise ProjectRecallDeadlineError("Knowledge recall deadline exceeded")
    return tuple(hit for _, hit in ranked)


def read_project_context(
    connection: sqlite3.Connection,
    workspace_id: str,
    refs: tuple[str, ...],
    *,
    applicability: WorkspaceApplicability | None = None,
) -> tuple[ProjectContextItem, ...]:
    """Expand only explicitly selected refs and fail closed on cross-Project identities."""
    if applicability is None:
        applicability = WorkspaceApplicability(connection, workspace_id)
        resolved_items = read_project_context(
            connection, workspace_id, refs, applicability=applicability
        )
        applicability.validate()
        return resolved_items
    if applicability.workspace_id != workspace_id:
        raise ProjectRetrievalError("applicability Workspace mismatch")
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
            if not applicability.knowledge_visible(card):
                raise ProjectRetrievalRefError(
                    "selected Knowledge ref is unavailable in the active Git context"
                )
            items.append(
                ProjectContextItem(
                    ref=ref,
                    kind=ProjectSearchKind.KNOWLEDGE,
                    data=_knowledge_context_data(card),
                )
            )
            continue
        if ref.startswith("task:"):
            task_id = ref.removeprefix("task:").partition("#")[0]
            if not applicability.task_visible(task_id):
                raise ProjectRetrievalRefError(
                    "selected Task ref is unavailable in the active Git context"
                )
            fragment = ref.partition("#")[2]
            if fragment.startswith("checkpoint:") and not applicability.checkpoint_visible(
                fragment.removeprefix("checkpoint:")
            ):
                raise ProjectRetrievalRefError(
                    "selected checkpoint is unavailable in the active Git context"
                )
            items.append(
                _task_context(connection, project.project_id, ref, applicability=applicability)
            )
            continue
        raise ProjectRetrievalRefError("selected Project context ref kind is unsupported")
    return tuple(items)


def _task_hits(
    connection: sqlite3.Connection,
    query: AnalyzedSearchQuery,
    limit: int,
    *,
    project_id: str | None = None,
    active_workspace_id: str | None = None,
    applicability: WorkspaceApplicability | None = None,
) -> tuple[_RankedProjectHit, ...]:
    def fragment_visible(ref: str) -> bool:
        return (
            applicability is None
            or not ref.startswith("checkpoint:")
            or applicability.checkpoint_visible(ref.removeprefix("checkpoint:"))
        )

    if applicability is not None:
        connection.create_function("harness_task_fragment_visible", 1, fragment_visible)
        connection.create_function("harness_task_visible", 1, applicability.task_visible)
    try:
        return _task_hits_with_visibility(
            connection,
            query,
            limit,
            project_id=project_id,
            active_workspace_id=active_workspace_id,
            applicability=applicability,
        )
    finally:
        if applicability is not None:
            connection.create_function("harness_task_visible", 1, None)
            connection.create_function("harness_task_fragment_visible", 1, None)


def _task_hits_with_visibility(
    connection: sqlite3.Connection,
    query: AnalyzedSearchQuery,
    limit: int,
    *,
    project_id: str | None = None,
    active_workspace_id: str | None = None,
    applicability: WorkspaceApplicability | None = None,
) -> tuple[_RankedProjectHit, ...]:
    current = (
        None
        if active_workspace_id is None
        else get_relevant_task(connection, active_workspace_id, applicability=applicability)
    )
    current_task_id = None if current is None else current.task_id
    identifier_hits = _task_identifier_hits(
        connection,
        query,
        limit,
        project_id=project_id,
        current_task_id=current_task_id,
        applicability=applicability,
    )
    if identifier_hits:
        return identifier_hits

    rank_width = len(query.terms) + 1

    def match_rank(title: object, body: object) -> int:
        if not isinstance(title, str) or not isinstance(body, str):
            raise ProjectRetrievalError("Task search index returned invalid persisted types")
        matched_terms = matching_term_count(query.terms, title, body)
        if contains_term_phrase(query.terms, title):
            quality = _QUALITY_TITLE_OR_IDENTIFIER_PHRASE
        elif contains_russian_case_phrase(query.terms, title):
            quality = _QUALITY_RUSSIAN_CASE_PHRASE
        elif matched_terms == len(query.terms):
            quality = _QUALITY_ALL_TERMS
        else:
            quality = _QUALITY_PARTIAL
        return quality * rank_width + len(query.terms) - matched_terms

    # Materialize only compact rank/identity data: FTS auxiliary functions must run in the
    # MATCH query, before windowing. The candidate cap applies to Tasks, not history fragments.
    sql = """
        WITH matched AS MATERIALIZED (
            SELECT fragment_ref, task_id, workspace_id, project_id,
                   harness_task_match_rank(title, body) AS match_rank,
                   bm25(task_search, 0.0, 0.0, 0.0, 0.0, 5.0, 1.0) AS score
            FROM task_search
            WHERE task_search MATCH ?
    """
    params: list[object] = [query.fts_expression]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if applicability is not None:
        sql += " AND harness_task_visible(task_id) AND harness_task_fragment_visible(fragment_ref)"
    sql += """
        ), per_task AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY task_id ORDER BY match_rank, score, fragment_ref
            ) AS fragment_position
            FROM matched
        )
        SELECT fragment_ref, task_id, workspace_id, project_id, match_rank, score
        FROM per_task
        WHERE fragment_position = 1
        ORDER BY match_rank, CASE WHEN task_id = ? THEN 0 ELSE 1 END,
                 score, task_id, fragment_ref
        LIMIT ?
    """
    params.extend((current_task_id, _candidate_limit(limit)))
    connection.create_function("harness_task_match_rank", 2, match_rank, deterministic=True)
    try:
        rows = connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise ProjectRetrievalError("Task search candidate ranking failed") from exc
    finally:
        connection.create_function("harness_task_match_rank", 2, None)

    ranked: list[_RankedProjectHit] = []
    for (
        fragment_ref,
        task_id,
        workspace_id,
        indexed_project_id,
        raw_match_rank,
        raw_score,
    ) in rows:
        if (
            not isinstance(fragment_ref, str)
            or not isinstance(task_id, str)
            or not isinstance(workspace_id, str)
            or not isinstance(indexed_project_id, str)
            or not isinstance(raw_match_rank, int)
            or not isinstance(raw_score, (int, float))
        ):
            raise ProjectRetrievalError("Task search index returned invalid persisted types")
        quality, unmatched_terms = divmod(raw_match_rank, rank_width)
        matched_terms = len(query.terms) - unmatched_terms
        task = get_task(connection, task_id)
        owner = get_workspace(connection, task.workspace_id)
        if owner.project_id != indexed_project_id or workspace_id != task.workspace_id:
            raise ProjectRetrievalError("Task search index crossed Project ownership")
        if project_id is not None and owner.project_id != project_id:
            raise ProjectRetrievalError("Task search index crossed Project ownership")
        result_ref, reason, summary, location = _task_fragment_projection(
            connection, task_id, fragment_ref
        )
        ranked.append(
            _RankedProjectHit(
                hit=ProjectSearchHit(
                    ref=result_ref,
                    kind=ProjectSearchKind.TASK,
                    title=task.title,
                    location=location,
                    short_summary=summary,
                    match_reason=reason,
                    freshness="durable_history",
                ),
                quality=quality,
                matched_terms=matched_terms,
                lexical_score=float(raw_score),
                relevance_boost=(0 if current_task_id == task_id else 1),
            )
        )
    ranked.sort(key=_ranked_hit_key)
    return tuple(ranked[:limit])


def _task_identifier_hits(
    connection: sqlite3.Connection,
    query: AnalyzedSearchQuery,
    limit: int,
    *,
    project_id: str | None,
    current_task_id: str | None,
    applicability: WorkspaceApplicability | None = None,
) -> tuple[_RankedProjectHit, ...]:
    identifier = query.normalized.removeprefix("task:")
    canonical = (
        identifier.lower() if re.fullmatch(r"[0-9a-fA-F]{10,32}", identifier) else identifier
    )
    scope_sql = " AND workspaces.project_id = ?" if project_id is not None else ""
    if applicability is not None:
        scope_sql += " AND harness_task_visible(tasks.id)"
    owner_sql = "FROM tasks JOIN workspaces ON workspaces.id = tasks.workspace_id"
    params: list[object] = [query.normalized, identifier, canonical]
    if project_id is not None:
        params.append(project_id)
    params.extend((query.normalized, identifier))
    rows = connection.execute(
        "SELECT tasks.id "
        + owner_sql
        + " WHERE tasks.id IN (?, ?, ?)"
        + scope_sql
        + " ORDER BY CASE WHEN tasks.id = ? THEN 0 WHEN tasks.id = ? THEN 1 ELSE 2 END LIMIT 1",
        params,
    ).fetchall()
    quality = _QUALITY_EXACT_TASK_ID
    reason = "Task ID"
    if not rows and re.fullmatch(r"[0-9a-f]{10,31}", canonical):
        params = [canonical + "*"]
        if project_id is not None:
            params.append(project_id)
        params.extend((current_task_id, limit))
        rows = connection.execute(
            "SELECT tasks.id "
            + owner_sql
            + " WHERE tasks.id GLOB ? AND length(tasks.id) = 32"
            + " AND tasks.id NOT GLOB '*[^0-9a-f]*'"
            + scope_sql
            + " ORDER BY CASE WHEN tasks.id = ? THEN 0 ELSE 1 END, tasks.id LIMIT ?",
            params,
        ).fetchall()
        quality = _QUALITY_TASK_ID_PREFIX
        reason = "Task ID prefix"

    ranked: list[_RankedProjectHit] = []
    for (raw_task_id,) in rows:
        task_id = _require_text(raw_task_id, "Task identifier")
        task = get_task(connection, task_id)
        if (
            project_id is not None
            and get_workspace(connection, task.workspace_id).project_id != project_id
        ):
            raise ProjectRetrievalError("Task identifier lookup crossed Project ownership")
        result_ref, _reason, summary, location = _task_fragment_projection(
            connection, task_id, f"task:{task_id}"
        )
        ranked.append(
            _RankedProjectHit(
                hit=ProjectSearchHit(
                    ref=result_ref,
                    kind=ProjectSearchKind.TASK,
                    title=task.title,
                    location=location,
                    short_summary=summary,
                    match_reason=reason,
                    freshness="durable_history",
                ),
                quality=quality,
                matched_terms=len(query.terms),
                lexical_score=0.0,
                relevance_boost=(0 if current_task_id == task_id else 1),
            )
        )
    return tuple(ranked)


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
                   operator_comment, jira_url, operator_status, deploy_test, deploy_prod
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
        operator_status = _task_event_delivery_status(row[6], row[7], row[8])
        raw_summary = next(
            (value for value in (*row[3:6], operator_status) if isinstance(value, str)), None
        )
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


def _task_event_delivery_status(
    legacy_status: object, deploy_test: object, deploy_prod: object
) -> str | None:
    """Project immutable event delivery evidence, retaining pre-v22 history."""
    if deploy_test is None and deploy_prod is None:
        if legacy_status is not None and not isinstance(legacy_status, str):
            raise ProjectRetrievalError("Task event delivery status is invalid")
        return legacy_status
    if (
        type(deploy_test) is not int
        or deploy_test not in (0, 1)
        or type(deploy_prod) is not int
        or deploy_prod not in (0, 1)
        or legacy_status is not None
    ):
        raise ProjectRetrievalError("Task event deployment flags are invalid")
    if deploy_test and deploy_prod:
        return TaskOperatorStatus.DEPLOY_BOTH.value
    if deploy_test:
        return TaskOperatorStatus.DEPLOY_TEST.value
    return TaskOperatorStatus.DEPLOY_PROD.value if deploy_prod else None


def _task_context(
    connection: sqlite3.Connection,
    project_id: str,
    ref: str,
    *,
    applicability: WorkspaceApplicability,
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
        for event in list_task_events(
            connection, task.task_id, limit=_CONTEXT_HISTORY_LIMIT, applicability=applicability
        ):
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
                   operator_comment, jira_url, operator_status, created_at,
                   deploy_test, deploy_prod
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
            ("operator_status", _task_event_delivery_status(row[6], row[8], row[9])),
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
        raise SearchError("Task search query must be non-empty text")
    normalized = query.strip()
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SearchError("Task search query must be valid UTF-8 text") from exc
    if size > MAX_SEARCH_QUERY_BYTES:
        raise SearchError(f"Task search query exceeds {MAX_SEARCH_QUERY_BYTES} UTF-8 bytes")
    return normalized


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise SearchError(f"Task search limit must be between 1 and {MAX_SEARCH_LIMIT}")


def _ranked_hit_key(item: _RankedProjectHit) -> tuple[int, int, int, float, str]:
    return (
        item.quality,
        -item.matched_terms,
        item.relevance_boost,
        item.lexical_score,
        item.hit.ref,
    )


def _project_hits(items: tuple[_RankedProjectHit, ...]) -> tuple[ProjectSearchHit, ...]:
    return tuple(item.hit for item in items)


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
