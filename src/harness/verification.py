from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

MAX_VERIFICATIONS_PER_CHECKPOINT = 12
MAX_VERIFICATION_NAME_BYTES = 256
MAX_VERIFICATION_EVIDENCE_BYTES = 2048


class VerificationError(RuntimeError):
    pass


class VerificationValidationError(VerificationError):
    pass


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class VerificationSource(StrEnum):
    AGENT_REPORTED = "agent_reported"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class VerificationDraft:
    name: str
    status: VerificationStatus
    evidence: str


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    checkpoint_id: str
    position: int
    name: str
    status: VerificationStatus
    evidence: str
    source: VerificationSource


def normalize_verification_drafts(
    drafts: Sequence[VerificationDraft],
) -> tuple[VerificationDraft, ...]:
    if len(drafts) > MAX_VERIFICATIONS_PER_CHECKPOINT:
        raise VerificationValidationError(
            f"verification exceeds {MAX_VERIFICATIONS_PER_CHECKPOINT} entries"
        )
    out = []
    seen = set()
    for d in drafts:
        if not isinstance(d, VerificationDraft) or not isinstance(d.status, VerificationStatus):
            raise VerificationValidationError("verification item has invalid type")
        name = _normalize_text(d.name, "verification name", MAX_VERIFICATION_NAME_BYTES)
        evidence = _normalize_text(
            d.evidence, "verification evidence", MAX_VERIFICATION_EVIDENCE_BYTES
        )
        key = name.casefold()
        if key in seen:
            raise VerificationValidationError("verification names must be unique per checkpoint")
        seen.add(key)
        out.append(VerificationDraft(name, d.status, evidence))
    return tuple(out)


def persist_checkpoint_verification(
    connection: sqlite3.Connection, checkpoint_id: str, drafts: Sequence[VerificationDraft]
) -> tuple[VerificationRecord, ...]:
    normalized = normalize_verification_drafts(drafts)
    records = tuple(
        VerificationRecord(
            checkpoint_id, i, d.name, d.status, d.evidence, VerificationSource.AGENT_REPORTED
        )
        for i, d in enumerate(normalized)
    )
    connection.executemany(
        "INSERT INTO task_checkpoint_verification(checkpoint_id,position,name,status,evidence,source) VALUES (?,?,?,?,?,?)",
        (
            (r.checkpoint_id, r.position, r.name, r.status.value, r.evidence, r.source.value)
            for r in records
        ),
    )
    return records


def list_checkpoint_verification(
    connection: sqlite3.Connection, checkpoint_id: str
) -> tuple[VerificationRecord, ...]:
    rows = connection.execute(
        "SELECT checkpoint_id,position,name,status,evidence,source FROM task_checkpoint_verification WHERE checkpoint_id=? ORDER BY position",
        (checkpoint_id,),
    ).fetchall()
    out = []
    for cid, pos, name, status, evidence, source in rows:
        if (
            cid != checkpoint_id
            or isinstance(pos, bool)
            or not isinstance(pos, int)
            or pos < 0
            or not all(isinstance(x, str) for x in (name, status, evidence, source))
        ):
            raise VerificationError("verification row has invalid persisted types")
        try:
            st = VerificationStatus(status)
            src = VerificationSource(source)
        except ValueError as exc:
            raise VerificationError("verification row has unsupported persisted enum") from exc
        out.append(
            VerificationRecord(
                cid,
                pos,
                _normalize_text(name, "verification name", MAX_VERIFICATION_NAME_BYTES),
                st,
                _normalize_text(evidence, "verification evidence", MAX_VERIFICATION_EVIDENCE_BYTES),
                src,
            )
        )
    if tuple(r.position for r in out) != tuple(range(len(out))):
        raise VerificationError("verification positions are not contiguous")
    return tuple(out)


def _normalize_text(value: str, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise VerificationValidationError(f"{label} must be text")
    value = value.strip()
    if not value or "\x00" in value:
        raise VerificationValidationError(f"{label} must be non-empty text")
    try:
        size = len(value.encode())
    except UnicodeEncodeError as exc:
        raise VerificationValidationError(f"{label} must be valid UTF-8 text") from exc
    if size > maximum_bytes:
        raise VerificationValidationError(f"{label} exceeds {maximum_bytes} UTF-8 bytes")
    return value
