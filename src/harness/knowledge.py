from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from harness.registry import get_project, get_workspace

if TYPE_CHECKING:
    from harness.tasks import TaskRecord

MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT = 8
MAX_KNOWLEDGE_ANCHORS_PER_CARD = 8
MAX_KNOWLEDGE_ANCHORS_PER_CHECKPOINT = 32
MAX_KNOWLEDGE_TITLE_BYTES = 256
MAX_KNOWLEDGE_BODY_BYTES = 8192
MAX_KNOWLEDGE_ANCHOR_PATH_BYTES = 4096
MAX_KNOWLEDGE_ANCHOR_SYMBOL_BYTES = 512
_KNOWLEDGE_ANCHOR_CAPTURE_TIMEOUT_SECONDS = 10.0
_HASH_CHUNK_BYTES = 128 * 1024


class KnowledgeError(RuntimeError):
    """Base class for durable Harness Knowledge failures."""


class KnowledgeValidationError(KnowledgeError):
    """Raised when a Knowledge draft violates the bounded domain contract."""


class KnowledgeAnchorError(KnowledgeError):
    """Raised when a code anchor cannot be captured mechanically and safely."""


class KnowledgeCorruptionError(KnowledgeError):
    """Raised when persisted Knowledge cannot be interpreted safely."""


class KnowledgeKind(StrEnum):
    """Sparse semantic Knowledge kinds accepted from real Task investigation."""

    BEHAVIOR = "behavior"
    DATA_FLOW = "data_flow"
    INVARIANT = "invariant"
    ARCHITECTURE_RATIONALE = "architecture_rationale"
    DECISION = "decision"
    CAVEAT = "caveat"
    OPERATIONAL_DETAIL = "operational_detail"


class KnowledgeSourceType(StrEnum):
    """Provenance source classes defined by the v1 product contract."""

    AGENT_ASSERTED = "agent_asserted"
    OPERATOR = "operator"
    REPOSITORY_DOCUMENT = "repository_document"
    ADR = "ADR"


class KnowledgeFreshness(StrEnum):
    """Explicit non-boolean freshness state for durable semantic Knowledge."""

    FRESH = "fresh"
    NEEDS_REVALIDATION = "needs_revalidation"


class KnowledgeAnchorKind(StrEnum):
    """Filesystem entry kind used by a mechanical Knowledge anchor fingerprint."""

    FILE = "file"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class KnowledgeAnchorDraft:
    """Requested code/document anchor; its fingerprint is always captured mechanically."""

    path: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeDraft:
    """One bounded semantic card proposed by a real Task checkpoint."""

    kind: KnowledgeKind
    title: str
    body: str
    anchors: tuple[KnowledgeAnchorDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeAnchorRecord:
    """Persisted mechanical anchor metadata without raw source content."""

    knowledge_id: str
    workspace_id: str
    relative_path: str
    symbol: str | None
    fingerprint_kind: KnowledgeAnchorKind
    content_sha256: str


@dataclass(frozen=True, slots=True)
class KnowledgeCardRecord:
    """One durable provenance-bearing semantic Knowledge card."""

    knowledge_id: str
    project_id: str
    kind: KnowledgeKind
    title: str
    body: str
    source_type: KnowledgeSourceType
    source_task_id: str | None
    source_checkpoint_id: str | None
    created_at: str
    updated_at: str
    freshness: KnowledgeFreshness
    anchors: tuple[KnowledgeAnchorRecord, ...]


def normalize_knowledge_drafts(drafts: Sequence[KnowledgeDraft]) -> tuple[KnowledgeDraft, ...]:
    """Validate and normalize one bounded checkpoint Knowledge batch without persistence."""
    if len(drafts) > MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT:
        raise KnowledgeValidationError(
            f"checkpoint knowledge exceeds {MAX_KNOWLEDGE_CARDS_PER_CHECKPOINT} cards"
        )

    normalized: list[KnowledgeDraft] = []
    total_anchors = 0
    for draft in drafts:
        if not isinstance(draft, KnowledgeDraft):
            raise KnowledgeValidationError("checkpoint knowledge items must be KnowledgeDraft")
        if not isinstance(draft.kind, KnowledgeKind):
            raise KnowledgeValidationError("knowledge kind must be a KnowledgeKind")
        title = _normalize_text(
            draft.title,
            label="knowledge title",
            maximum_bytes=MAX_KNOWLEDGE_TITLE_BYTES,
        )
        body = _normalize_text(
            draft.body,
            label="knowledge body",
            maximum_bytes=MAX_KNOWLEDGE_BODY_BYTES,
        )
        if not isinstance(draft.anchors, tuple):
            raise KnowledgeValidationError("knowledge anchors must be a tuple")
        if len(draft.anchors) > MAX_KNOWLEDGE_ANCHORS_PER_CARD:
            raise KnowledgeValidationError(
                f"knowledge card exceeds {MAX_KNOWLEDGE_ANCHORS_PER_CARD} anchors"
            )
        total_anchors += len(draft.anchors)
        if total_anchors > MAX_KNOWLEDGE_ANCHORS_PER_CHECKPOINT:
            raise KnowledgeValidationError(
                f"checkpoint knowledge exceeds {MAX_KNOWLEDGE_ANCHORS_PER_CHECKPOINT} anchors"
            )

        seen: set[tuple[str, str | None]] = set()
        anchors: list[KnowledgeAnchorDraft] = []
        for anchor in draft.anchors:
            if not isinstance(anchor, KnowledgeAnchorDraft):
                raise KnowledgeValidationError("knowledge anchors must be KnowledgeAnchorDraft")
            relative_path = _normalize_relative_path(anchor.path)
            symbol = _normalize_optional_text(
                anchor.symbol,
                label="knowledge anchor symbol",
                maximum_bytes=MAX_KNOWLEDGE_ANCHOR_SYMBOL_BYTES,
            )
            identity = (relative_path, symbol)
            if identity in seen:
                raise KnowledgeValidationError("knowledge card contains a duplicate anchor")
            seen.add(identity)
            anchors.append(KnowledgeAnchorDraft(path=relative_path, symbol=symbol))
        normalized.append(
            KnowledgeDraft(kind=draft.kind, title=title, body=body, anchors=tuple(anchors))
        )
    return tuple(normalized)


def persist_checkpoint_knowledge(
    connection: sqlite3.Connection,
    task: TaskRecord,
    checkpoint_id: str,
    drafts: Sequence[KnowledgeDraft],
    *,
    timestamp: str,
) -> tuple[KnowledgeCardRecord, ...]:
    """Persist one agent-asserted Knowledge batch inside the caller's checkpoint transaction."""
    if not connection.in_transaction:
        raise KnowledgeError("checkpoint Knowledge persistence requires an active transaction")
    normalized = normalize_knowledge_drafts(drafts)
    if not normalized:
        return ()
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise KnowledgeValidationError("source checkpoint identity must be non-empty text")
    if not isinstance(timestamp, str) or not timestamp:
        raise KnowledgeValidationError("knowledge timestamp must be non-empty text")

    workspace = get_workspace(connection, task.workspace_id)
    project = get_project(connection, workspace.project_id)
    deadline = monotonic() + _KNOWLEDGE_ANCHOR_CAPTURE_TIMEOUT_SECONDS
    fingerprint_cache: dict[str, tuple[KnowledgeAnchorKind, str]] = {}
    records: list[KnowledgeCardRecord] = []

    for draft in normalized:
        knowledge_id = uuid4().hex
        anchor_records: list[KnowledgeAnchorRecord] = []
        for anchor in draft.anchors:
            fingerprint = fingerprint_cache.get(anchor.path)
            if fingerprint is None:
                fingerprint = _capture_anchor_fingerprint(
                    workspace.workspace_root,
                    anchor.path,
                    deadline=deadline,
                )
                fingerprint_cache[anchor.path] = fingerprint
            fingerprint_kind, content_sha256 = fingerprint
            anchor_records.append(
                KnowledgeAnchorRecord(
                    knowledge_id=knowledge_id,
                    workspace_id=workspace.workspace_id,
                    relative_path=anchor.path,
                    symbol=anchor.symbol,
                    fingerprint_kind=fingerprint_kind,
                    content_sha256=content_sha256,
                )
            )

        connection.execute(
            """
            INSERT INTO knowledge_cards(
                id, project_id, kind, title, body, source_type,
                source_task_id, source_checkpoint_id,
                created_at, updated_at, freshness
            ) VALUES (?, ?, ?, ?, ?, 'agent_asserted', ?, ?, ?, ?, 'fresh')
            """,
            (
                knowledge_id,
                project.project_id,
                draft.kind.value,
                draft.title,
                draft.body,
                task.task_id,
                checkpoint_id,
                timestamp,
                timestamp,
            ),
        )
        connection.executemany(
            """
            INSERT INTO knowledge_anchors(
                knowledge_id, workspace_id, relative_path, symbol,
                fingerprint_kind, content_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    anchor.knowledge_id,
                    anchor.workspace_id,
                    anchor.relative_path,
                    anchor.symbol or "",
                    anchor.fingerprint_kind.value,
                    anchor.content_sha256,
                )
                for anchor in anchor_records
            ),
        )
        records.append(
            KnowledgeCardRecord(
                knowledge_id=knowledge_id,
                project_id=project.project_id,
                kind=draft.kind,
                title=draft.title,
                body=draft.body,
                source_type=KnowledgeSourceType.AGENT_ASSERTED,
                source_task_id=task.task_id,
                source_checkpoint_id=checkpoint_id,
                created_at=timestamp,
                updated_at=timestamp,
                freshness=KnowledgeFreshness.FRESH,
                anchors=tuple(anchor_records),
            )
        )
    return tuple(records)


def get_knowledge_card(connection: sqlite3.Connection, knowledge_id: str) -> KnowledgeCardRecord:
    """Load one Knowledge card and fail closed on malformed persisted state."""
    row = connection.execute(
        """
        SELECT
            id, project_id, kind, title, body, source_type,
            source_task_id, source_checkpoint_id, created_at, updated_at, freshness
        FROM knowledge_cards
        WHERE id = ?
        """,
        (knowledge_id,),
    ).fetchone()
    if row is None:
        raise KnowledgeError(f"knowledge card does not exist: {knowledge_id}")
    return _card_from_row(connection, row)


def list_project_knowledge(
    connection: sqlite3.Connection,
    project_id: str,
) -> tuple[KnowledgeCardRecord, ...]:
    """Load project Knowledge in stable creation/id order for internal domain use."""
    get_project(connection, project_id)
    rows = connection.execute(
        """
        SELECT
            id, project_id, kind, title, body, source_type,
            source_task_id, source_checkpoint_id, created_at, updated_at, freshness
        FROM knowledge_cards
        WHERE project_id = ?
        ORDER BY created_at, id
        """,
        (project_id,),
    ).fetchall()
    return tuple(_card_from_row(connection, row) for row in rows)


def snapshot_fresh_anchored_knowledge_ids(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> frozenset[str]:
    """Capture fresh anchored Knowledge that existed before a filesystem scan snapshot."""
    get_workspace(connection, workspace_id)
    rows = connection.execute(
        """
        SELECT DISTINCT a.knowledge_id
        FROM knowledge_anchors AS a
        JOIN knowledge_cards AS c ON c.id = a.knowledge_id
        WHERE a.workspace_id = ? AND c.freshness = 'fresh'
        ORDER BY a.knowledge_id
        """,
        (workspace_id,),
    ).fetchall()
    knowledge_ids: set[str] = set()
    for (knowledge_id,) in rows:
        if not isinstance(knowledge_id, str) or not knowledge_id:
            raise KnowledgeCorruptionError("knowledge anchor has invalid persisted identity")
        knowledge_ids.add(knowledge_id)
    return frozenset(knowledge_ids)


def reconcile_knowledge_staleness(
    connection: sqlite3.Connection,
    workspace_id: str,
    current_snapshot: Mapping[str, tuple[str, str]],
    *,
    eligible_knowledge_ids: frozenset[str],
    now: datetime | None = None,
) -> int:
    """Mark pre-snapshot fresh cards stale when source-Workspace anchors no longer match."""
    if not connection.in_transaction:
        raise KnowledgeError("Knowledge staleness reconciliation requires an active transaction")
    get_workspace(connection, workspace_id)
    rows = connection.execute(
        """
        SELECT
            a.knowledge_id, a.workspace_id, a.relative_path, a.symbol,
            a.fingerprint_kind, a.content_sha256
        FROM knowledge_anchors AS a
        JOIN knowledge_cards AS c ON c.id = a.knowledge_id
        WHERE a.workspace_id = ? AND c.freshness = 'fresh'
        ORDER BY a.knowledge_id, a.relative_path, a.symbol
        """,
        (workspace_id,),
    ).fetchall()
    stale_ids: set[str] = set()
    for row in rows:
        anchor = _anchor_from_row(row)
        if anchor.knowledge_id not in eligible_knowledge_ids:
            continue
        current = current_snapshot.get(anchor.relative_path)
        expected = (anchor.fingerprint_kind.value, anchor.content_sha256)
        if current != expected:
            stale_ids.add(anchor.knowledge_id)

    if not stale_ids:
        return 0
    timestamp = _utc_timestamp(now)
    changed = 0
    for knowledge_id in sorted(stale_ids):
        cursor = connection.execute(
            """
            UPDATE knowledge_cards
            SET freshness = 'needs_revalidation', updated_at = ?
            WHERE id = ? AND freshness = 'fresh'
            """,
            (timestamp, knowledge_id),
        )
        changed += cursor.rowcount
    return changed


def _card_from_row(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> KnowledgeCardRecord:
    (
        knowledge_id,
        project_id,
        kind,
        title,
        body,
        source_type,
        source_task_id,
        source_checkpoint_id,
        created_at,
        updated_at,
        freshness,
    ) = row
    if (
        not isinstance(knowledge_id, str)
        or not knowledge_id
        or not isinstance(project_id, str)
        or not project_id
        or not isinstance(kind, str)
        or not isinstance(title, str)
        or not isinstance(body, str)
        or not isinstance(source_type, str)
        or (
            source_task_id is not None
            and (not isinstance(source_task_id, str) or not source_task_id)
        )
        or (
            source_checkpoint_id is not None
            and (not isinstance(source_checkpoint_id, str) or not source_checkpoint_id)
        )
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(updated_at, str)
        or not updated_at
        or not isinstance(freshness, str)
    ):
        raise KnowledgeCorruptionError("knowledge card row has invalid persisted types")
    try:
        parsed_kind = KnowledgeKind(kind)
        parsed_source_type = KnowledgeSourceType(source_type)
        parsed_freshness = KnowledgeFreshness(freshness)
    except ValueError as exc:
        raise KnowledgeCorruptionError(
            "knowledge card row has unsupported persisted value"
        ) from exc
    normalized_title = _normalize_persisted_text(
        title,
        label="knowledge title",
        maximum_bytes=MAX_KNOWLEDGE_TITLE_BYTES,
    )
    normalized_body = _normalize_persisted_text(
        body,
        label="knowledge body",
        maximum_bytes=MAX_KNOWLEDGE_BODY_BYTES,
    )
    if parsed_source_type is KnowledgeSourceType.AGENT_ASSERTED and (
        source_task_id is None or source_checkpoint_id is None
    ):
        raise KnowledgeCorruptionError("agent-asserted Knowledge is missing Task provenance")

    anchor_rows = connection.execute(
        """
        SELECT
            knowledge_id, workspace_id, relative_path, symbol,
            fingerprint_kind, content_sha256
        FROM knowledge_anchors
        WHERE knowledge_id = ?
        ORDER BY workspace_id, relative_path, symbol
        """,
        (knowledge_id,),
    ).fetchall()
    if len(anchor_rows) > MAX_KNOWLEDGE_ANCHORS_PER_CARD:
        raise KnowledgeCorruptionError("knowledge card exceeds persisted anchor bound")
    anchors = tuple(_anchor_from_row(anchor_row) for anchor_row in anchor_rows)
    return KnowledgeCardRecord(
        knowledge_id=knowledge_id,
        project_id=project_id,
        kind=parsed_kind,
        title=normalized_title,
        body=normalized_body,
        source_type=parsed_source_type,
        source_task_id=source_task_id,
        source_checkpoint_id=source_checkpoint_id,
        created_at=created_at,
        updated_at=updated_at,
        freshness=parsed_freshness,
        anchors=anchors,
    )


def _anchor_from_row(row: tuple[object, ...]) -> KnowledgeAnchorRecord:
    knowledge_id, workspace_id, relative_path, symbol, fingerprint_kind, content_sha256 = row
    if (
        not isinstance(knowledge_id, str)
        or not knowledge_id
        or not isinstance(workspace_id, str)
        or not workspace_id
        or not isinstance(relative_path, str)
        or not isinstance(symbol, str)
        or not isinstance(fingerprint_kind, str)
        or not isinstance(content_sha256, str)
        or not _is_sha256(content_sha256)
    ):
        raise KnowledgeCorruptionError("knowledge anchor row has invalid persisted types")
    safe_path = _normalize_persisted_relative_path(relative_path)
    parsed_symbol = None
    if symbol:
        parsed_symbol = _normalize_persisted_text(
            symbol,
            label="knowledge anchor symbol",
            maximum_bytes=MAX_KNOWLEDGE_ANCHOR_SYMBOL_BYTES,
        )
    try:
        parsed_kind = KnowledgeAnchorKind(fingerprint_kind)
    except ValueError as exc:
        raise KnowledgeCorruptionError(
            f"knowledge anchor has unsupported fingerprint kind: {fingerprint_kind!r}"
        ) from exc
    return KnowledgeAnchorRecord(
        knowledge_id=knowledge_id,
        workspace_id=workspace_id,
        relative_path=safe_path,
        symbol=parsed_symbol,
        fingerprint_kind=parsed_kind,
        content_sha256=content_sha256.lower(),
    )


def _capture_anchor_fingerprint(
    workspace_root: Path,
    relative_path: str,
    *,
    deadline: float,
) -> tuple[KnowledgeAnchorKind, str]:
    _require_deadline(deadline)
    try:
        root = workspace_root.resolve(strict=True)
    except OSError as exc:
        raise KnowledgeAnchorError("registered Workspace root cannot be resolved safely") from exc
    if root != workspace_root:
        raise KnowledgeAnchorError("registered Workspace root is no longer canonical")
    path = root / relative_path
    try:
        parent = path.parent.resolve(strict=True)
        if not parent.is_relative_to(root):
            raise KnowledgeAnchorError(
                f"knowledge anchor escapes Workspace through parent symlink: {relative_path}"
            )
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            target = os.readlink(path)
            after = path.lstat()
            _require_stable_stat(relative_path, before, after)
            symlink_digest = hashlib.sha256(b"symlink\0" + os.fsencode(target)).hexdigest()
            return KnowledgeAnchorKind.SYMLINK, symlink_digest
        if not stat.S_ISREG(before.st_mode):
            raise KnowledgeAnchorError(
                f"knowledge anchor must reference a regular file or symlink: {relative_path}"
            )
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise KnowledgeAnchorError(
                f"knowledge anchor file resolves outside Workspace: {relative_path}"
            )
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            _require_stable_stat(relative_path, before, opened_before)
            while True:
                _require_deadline(deadline)
                chunk = stream.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                file_digest.update(chunk)
            opened_after = os.fstat(stream.fileno())
        _require_stable_stat(relative_path, opened_before, opened_after)
        current = path.lstat()
        _require_stable_stat(relative_path, opened_after, current)
        return KnowledgeAnchorKind.FILE, file_digest.hexdigest()
    except FileNotFoundError as exc:
        raise KnowledgeAnchorError(f"knowledge anchor does not exist: {relative_path}") from exc
    except KnowledgeAnchorError:
        raise
    except OSError as exc:
        raise KnowledgeAnchorError(
            f"knowledge anchor could not be inspected: {relative_path}"
        ) from exc


def _require_stable_stat(relative_path: str, before: os.stat_result, after: os.stat_result) -> None:
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise KnowledgeAnchorError(f"knowledge anchor changed during capture: {relative_path}")


def _normalize_relative_path(value: str) -> str:
    try:
        return _validated_relative_path(value)
    except KnowledgeCorruptionError as exc:
        raise KnowledgeValidationError(str(exc)) from exc


def _normalize_persisted_relative_path(value: str) -> str:
    return _validated_relative_path(value)


def _validated_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeCorruptionError("knowledge anchor path must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise KnowledgeCorruptionError("knowledge anchor path must be valid UTF-8") from exc
    path = PurePosixPath(value)
    if (
        not value
        or len(encoded) > MAX_KNOWLEDGE_ANCHOR_PATH_BYTES
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
        or path.as_posix() != value
        or path.parts[0] == ".git"
    ):
        raise KnowledgeCorruptionError(f"unsafe knowledge anchor path: {value!r}")
    return value


def _normalize_text(value: str, *, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise KnowledgeValidationError(f"{label} must be text")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise KnowledgeValidationError(f"{label} must be non-empty text")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise KnowledgeValidationError(f"{label} must be valid UTF-8 text") from exc
    if size > maximum_bytes:
        raise KnowledgeValidationError(f"{label} exceeds {maximum_bytes} UTF-8 bytes")
    return normalized


def _normalize_optional_text(
    value: str | None,
    *,
    label: str,
    maximum_bytes: int,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, label=label, maximum_bytes=maximum_bytes)


def _normalize_persisted_text(value: str, *, label: str, maximum_bytes: int) -> str:
    try:
        normalized = _normalize_text(value, label=label, maximum_bytes=maximum_bytes)
    except KnowledgeValidationError as exc:
        raise KnowledgeCorruptionError(str(exc)) from exc
    if normalized != value:
        raise KnowledgeCorruptionError(f"persisted {label} is not normalized")
    return normalized


def _utc_timestamp(now: datetime | None) -> str:
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise KnowledgeValidationError("Knowledge timestamps require a timezone-aware datetime")
    return current.astimezone(UTC).isoformat(timespec="microseconds")


def _require_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise KnowledgeAnchorError("knowledge anchor capture deadline exceeded")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
