#!/usr/bin/env python3
"""Reviewable authority reconciliation queue helpers.

These helpers update country-local authority reconciliation tables for the
subject/entity importer and review workflows. They mutate only the provided
SQLite connection and do not commit; callers own transaction boundaries so
multi-step review decisions can be rolled back atomically.

Assumptions:
    - Pass a `sqlite3.Connection` with `row_factory=sqlite3.Row` so row
      lookups by column name work.
    - The schema must define the `authority_reconciliation`,
      `authority_record`, `authority_identifier`, `extraction_detected_entity`,
      and `authority_merge_event` tables with the columns used in each SQL
      statement.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import uuid
from typing import Any

from tools.source_db_tools import identifier_normalization  # noqa: E402

AUTHORITY_NAMESPACE = uuid.UUID("8b3022f5-6267-4545-8f41-3d337657d4f5")


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def label_norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def stable_key(*parts: Any) -> str:
    seed = "|".join("" if part is None else str(part) for part in parts)
    return str(uuid.uuid5(AUTHORITY_NAMESPACE, seed))


def fetch_reconciliation(conn: sqlite3.Connection, reconciliation_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM authority_reconciliation WHERE authority_reconciliation_id=?",
        (reconciliation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown authority reconciliation id: {reconciliation_id}")
    return row


def fetch_authority_record(conn: sqlite3.Connection, authority_record_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM authority_record WHERE authority_record_id=?",
        (authority_record_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown authority record id: {authority_record_id}")
    return row


def propose_candidate(
    conn: sqlite3.Connection,
    *,
    detected_entity_id: int | None,
    raw_label: str,
    entity_type: str | None,
    candidate_authority_id: int | None = None,
    candidate_scheme: str | None = None,
    candidate_uri: str | None = None,
    match_method: str | None = None,
    match_score: float | None = None,
    evidence_context: str | None = None,
    reviewer_note: str | None = None,
    review_state: str = "proposed",
    created_at: str | None = None,
) -> int:
    timestamp = created_at or now_iso()
    target_namespace = (
        "extraction_detected_entity"
        if detected_entity_id is not None
        else "authority_reconciliation"
    )
    target_id = (
        str(detected_entity_id)
        if detected_entity_id is not None
        else stable_key(raw_label, entity_type, candidate_authority_id, candidate_uri)
    )
    reconciliation_key = "authrec:" + stable_key(
        target_namespace, target_id, raw_label, candidate_authority_id, candidate_uri
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO authority_reconciliation (
          reconciliation_key_v1, target_namespace, target_id,
          detected_entity_id, raw_label, entity_type, candidate_label,
          candidate_authority_record_id, candidate_authority_id,
          external_scheme, external_uri, candidate_scheme, candidate_uri,
          method, match_method, match_score, evidence_context,
          confidence_score, review_state, reviewer_note, created_at,
          updated_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reconciliation_key,
            target_namespace,
            target_id,
            detected_entity_id,
            raw_label,
            entity_type,
            raw_label,
            candidate_authority_id,
            candidate_authority_id,
            candidate_scheme,
            candidate_uri,
            candidate_scheme,
            candidate_uri,
            match_method,
            match_method,
            match_score,
            evidence_context,
            match_score,
            review_state,
            reviewer_note,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    row = conn.execute(
        "SELECT authority_reconciliation_id FROM authority_reconciliation WHERE reconciliation_key_v1=?",
        (reconciliation_key,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"failed to insert or fetch authority reconciliation key: {reconciliation_key}"
        )
    return int(row[0])


def reject_candidate(
    conn: sqlite3.Connection,
    reconciliation_id: int,
    *,
    reason: str | None = None,
    rejected_candidate_id: int | None = None,
    changed_at: str | None = None,
) -> None:
    row = fetch_reconciliation(conn, reconciliation_id)
    timestamp = changed_at or now_iso()
    try:
        rejected = json.loads(row["rejected_candidate_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        rejected = []
    if not isinstance(rejected, list):
        rejected = []
    candidate_id = rejected_candidate_id
    if candidate_id is None:
        candidate_id = row["candidate_authority_id"] or row["candidate_authority_record_id"]
    if candidate_id is not None and candidate_id not in rejected:
        rejected.append(candidate_id)
    conn.execute(
        """
        UPDATE authority_reconciliation
        SET review_state='rejected',
            reviewer_note=?,
            rejected_candidate_ids_json=?,
            decided_at=?,
            updated_at=?,
            record_last_updated=?
        WHERE authority_reconciliation_id=?
        """,
        (
            reason,
            json.dumps(rejected, sort_keys=True),
            timestamp,
            timestamp,
            timestamp,
            reconciliation_id,
        ),
    )


def mark_ambiguous(
    conn: sqlite3.Connection,
    reconciliation_id: int,
    *,
    note: str | None = None,
    changed_at: str | None = None,
) -> None:
    fetch_reconciliation(conn, reconciliation_id)
    timestamp = changed_at or now_iso()
    conn.execute(
        """
        UPDATE authority_reconciliation
        SET review_state='ambiguous',
            reviewer_note=?,
            updated_at=?,
            record_last_updated=?
        WHERE authority_reconciliation_id=?
        """,
        (note, timestamp, timestamp, reconciliation_id),
    )


def accept_candidate(
    conn: sqlite3.Connection,
    reconciliation_id: int,
    *,
    accepted_authority_id: int | None = None,
    note: str | None = None,
    changed_at: str | None = None,
) -> None:
    row = fetch_reconciliation(conn, reconciliation_id)
    authority_id = accepted_authority_id
    if authority_id is None:
        authority_id = row["candidate_authority_id"] or row["candidate_authority_record_id"]
    if authority_id is None:
        raise ValueError(
            "accepted authority id is required when no candidate authority was proposed"
        )
    fetch_authority_record(conn, authority_id)
    timestamp = changed_at or now_iso()
    conn.execute(
        """
        UPDATE authority_reconciliation
        SET review_state='accepted',
            accepted_authority_id=?,
            reviewer_note=?,
            decided_at=?,
            updated_at=?,
            record_last_updated=?
        WHERE authority_reconciliation_id=?
        """,
        (authority_id, note, timestamp, timestamp, timestamp, reconciliation_id),
    )
    if row["detected_entity_id"] is not None:
        conn.execute(
            """
            UPDATE extraction_detected_entity
            SET authority_record_id=?,
                review_state=CASE
                    WHEN review_state IN ('accepted', 'approved', 'curated', 'reviewed') THEN review_state
                    ELSE 'accepted'
                END,
                record_last_updated=?
            WHERE detected_entity_id=?
            """,
            (authority_id, timestamp, row["detected_entity_id"]),
        )


def create_local_authority(
    conn: sqlite3.Connection,
    *,
    authority_type: str,
    preferred_label: str,
    source_namespace: str = "authority_reconciliation",
    source_id: str | None = None,
    review_state: str = "accepted",
    confidence_score: float | None = None,
    created_at: str | None = None,
) -> int:
    timestamp = created_at or now_iso()
    key = "auth:local:" + stable_key(authority_type, preferred_label, source_namespace, source_id)
    conn.execute(
        """
        INSERT INTO authority_record (
          authority_key_v1, authority_type, preferred_label, label_norm,
          sort_label, source_namespace, source_id, reconciliation_status,
          review_state, confidence_score, created_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(authority_key_v1) DO UPDATE SET
          review_state=excluded.review_state,
          confidence_score=COALESCE(excluded.confidence_score, authority_record.confidence_score),
          record_last_updated=excluded.record_last_updated
        """,
        (
            key,
            authority_type,
            preferred_label,
            label_norm(preferred_label),
            preferred_label,
            source_namespace,
            source_id,
            "local",
            review_state,
            confidence_score,
            timestamp,
            timestamp,
        ),
    )
    return int(
        conn.execute(
            "SELECT authority_record_id FROM authority_record WHERE authority_key_v1=?", (key,)
        ).fetchone()[0]
    )


def add_authority_identifier(
    conn: sqlite3.Connection,
    *,
    authority_record_id: int,
    scheme: str,
    value: str,
    is_primary: int = 0,
    confidence_score: float | None = None,
    review_state: str | None = None,
    verified_at: str | None = None,
) -> int:
    timestamp = verified_at or now_iso()
    normalized = identifier_normalization.identifier_storage_values(scheme, value)
    fetch_authority_record(conn, authority_record_id)
    existing = conn.execute(
        "SELECT authority_identifier_id, authority_record_id FROM authority_identifier WHERE scheme=? AND value=?",
        (normalized["scheme"], normalized["value"]),
    ).fetchone()
    if existing is not None and int(existing[1]) != authority_record_id:
        raise ValueError(
            f"{normalized['scheme']} identifier {normalized['value']} already belongs to authority record {existing[1]}"
        )
    conn.execute(
        """
        INSERT INTO authority_identifier (
          authority_record_id, scheme, value, raw_value, normalized_value,
          uri, normalized_uri, validity_status, validation_warning, is_primary,
          confidence_score, review_state, last_verified_at, record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(scheme, value) DO UPDATE SET
          raw_value=excluded.raw_value,
          normalized_value=excluded.normalized_value,
          uri=excluded.uri,
          normalized_uri=excluded.normalized_uri,
          validity_status=excluded.validity_status,
          validation_warning=excluded.validation_warning,
          is_primary=excluded.is_primary,
          confidence_score=COALESCE(excluded.confidence_score, authority_identifier.confidence_score),
          review_state=COALESCE(excluded.review_state, authority_identifier.review_state),
          last_verified_at=excluded.last_verified_at,
          record_last_updated=excluded.record_last_updated
        """,
        (
            authority_record_id,
            normalized["scheme"],
            normalized["value"],
            normalized["raw_value"],
            normalized["normalized_value"],
            normalized["normalized_uri"],
            normalized["normalized_uri"],
            normalized["validity_status"],
            normalized["validation_warning"],
            is_primary,
            confidence_score,
            review_state,
            timestamp,
            timestamp,
        ),
    )
    row = conn.execute(
        "SELECT authority_identifier_id, authority_record_id FROM authority_identifier WHERE scheme=? AND value=?",
        (normalized["scheme"], normalized["value"]),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"failed to insert or fetch authority identifier: {normalized['scheme']}:{normalized['value']}"
        )
    return int(row[0])


def merge_local_authority_into_external(
    conn: sqlite3.Connection,
    *,
    local_authority_id: int,
    external_authority_id: int,
    reason: str | None = None,
    merged_by: str | None = None,
    merged_at: str | None = None,
) -> None:
    if local_authority_id == external_authority_id:
        raise ValueError("cannot merge an authority record into itself")
    local_authority = fetch_authority_record(conn, local_authority_id)
    fetch_authority_record(conn, external_authority_id)
    current_merge_target = local_authority["merged_into_authority_record_id"]
    if current_merge_target is not None:
        if current_merge_target == external_authority_id:
            return
        raise ValueError(
            f"authority record {local_authority_id} is already merged into {current_merge_target}"
        )
    timestamp = merged_at or now_iso()
    conn.execute(
        """
        UPDATE authority_record
        SET merged_into_authority_record_id=?,
            reconciliation_status='merged',
            record_last_updated=?
        WHERE authority_record_id=?
        """,
        (external_authority_id, timestamp, local_authority_id),
    )
    conn.execute(
        """
        INSERT INTO authority_merge_event (
          from_authority_record_id, into_authority_record_id, merge_reason,
          evidence_note, merged_at, merged_by, review_state,
          record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            local_authority_id,
            external_authority_id,
            reason,
            reason,
            timestamp,
            merged_by,
            "accepted",
            timestamp,
        ),
    )
