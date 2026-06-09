#!/usr/bin/env python3
"""Append-only provenance event helpers for canonical source objects.

This module is a small persistence helper used by SQLite pipeline tooling when
recording immutable provenance rows. It does not open or manage database
connections itself; callers are responsible for transaction boundaries.

Documentation: docs/tools/source_db_tools/README.md
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from typing import Any

PROVENANCE_NAMESPACE = uuid.UUID("4f207df9-1645-4328-860e-4093387d3e81")

EVENT_TYPES = {
    "created",
    "imported",
    "extracted",
    "normalized",
    "reviewed",
    "corrected",
    "merged",
    "split",
    "demoted",
    "promoted",
    "exported",
    "discarded_payload",
    "retained_by_override",
    "authority_candidate_proposed",
    "authority_candidate_accepted",
    "authority_candidate_rejected",
}


def now_iso() -> str:
    """Return the current UTC timestamp used by provenance rows."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def stable_key(*parts: Any) -> str:
    """Derive a deterministic UUID5 key for an event payload."""
    seed = "|".join("" if part is None else str(part) for part in parts)
    return "prov:" + str(uuid.uuid5(PROVENANCE_NAMESPACE, seed))


def _required_text(value: Any, field_name: str) -> str:
    if value is None:
        raise ValueError(f"{field_name} is required")
    value_text = str(value).strip()
    if value_text == "":
        raise ValueError(f"{field_name} is required")
    return value_text


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    value_text = str(value).strip()
    if value_text == "":
        raise ValueError(f"{field_name} may not be empty when provided")
    return value_text


def _normalize_confidence_score(confidence_score: float | None) -> float | None:
    if confidence_score is None:
        return None
    if isinstance(confidence_score, bool):
        raise TypeError("confidence_score must be a number between 0.0 and 1.0, or None")
    try:
        score = float(confidence_score)
    except (TypeError, ValueError) as exc:
        raise TypeError("confidence_score must be a number between 0.0 and 1.0, or None") from exc
    if not 0.0 <= score <= 1.0:
        raise ValueError("confidence_score must be between 0.0 and 1.0")
    return score


def _normalize_timestamp(event_timestamp: str | None) -> str:
    """Validate the supplied timestamp or generate a fresh UTC timestamp."""
    if event_timestamp is None:
        return now_iso()
    if not isinstance(event_timestamp, str):
        raise TypeError("event_timestamp must be a string in ISO format, or None")

    # Allow both "+00:00" and "Z" suffixes for UTC while validating only.
    # Preserve the original value for round-trip compatibility.
    try:
        dt.datetime.fromisoformat(event_timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("event_timestamp must be a valid ISO-8601 timestamp") from exc
    return event_timestamp


def record_event(
    conn: sqlite3.Connection,
    *,
    object_namespace: str,
    object_id: str | int,
    event_type: str,
    actor_type: str | None = None,
    actor_id: str | None = None,
    actor_label: str | None = None,
    tool_name: str | None = None,
    tool_version: str | None = None,
    model_name: str | None = None,
    prompt_id: str | None = None,
    run_id: str | None = None,
    source_object_namespace: str | None = None,
    source_object_id: str | int | None = None,
    event_timestamp: str | None = None,
    confidence_score: float | None = None,
    note_text: str | None = None,
    provenance_event_key_v1: str | None = None,
) -> int:
    """Insert one provenance event row and return the inserted row id.

    Commit/rollback ownership stays with the caller so the provenance row can
    be part of the same transaction as the object change it describes.
    """
    object_namespace_value = _required_text(object_namespace, "object_namespace")
    object_id_value = _required_text(object_id, "object_id")
    event_type_value = _required_text(event_type, "event_type")
    source_object_namespace_value = _optional_text(
        source_object_namespace, "source_object_namespace"
    )
    source_object_id_value = _optional_text(source_object_id, "source_object_id")
    confidence_score_value = _normalize_confidence_score(confidence_score)

    if (source_object_namespace_value is None) != (source_object_id_value is None):
        raise ValueError("source_object_namespace and source_object_id must be provided together")

    if event_type_value not in EVENT_TYPES:
        raise ValueError(f"unsupported provenance event_type: {event_type_value}")

    timestamp = _normalize_timestamp(event_timestamp)
    explicit_key = (
        None if provenance_event_key_v1 is None else str(provenance_event_key_v1).strip() or None
    )
    key = explicit_key or stable_key(
        object_namespace_value,
        object_id_value,
        event_type_value,
        actor_type,
        actor_id,
        tool_name,
        run_id,
        source_object_namespace_value,
        source_object_id_value,
        timestamp,
    )
    cursor = conn.execute(
        """
        INSERT INTO provenance_event (
          provenance_event_key_v1, object_namespace, object_id, event_type,
          actor_type, actor_id, actor_label, tool_name, tool_version,
          model_name, prompt_id, run_id, source_object_namespace,
          source_object_id, event_timestamp, confidence_score, note_text,
          record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            object_namespace_value,
            object_id_value,
            event_type_value,
            actor_type,
            actor_id,
            actor_label,
            tool_name,
            tool_version,
            model_name,
            prompt_id,
            run_id,
            source_object_namespace_value,
            source_object_id_value,
            timestamp,
            confidence_score_value,
            note_text,
            timestamp,
        ),
    )
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("sqlite did not return a provenance_event row id")
    return int(row_id)
