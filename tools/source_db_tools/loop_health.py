"""Read-only loop-health observability for the canonical Summa loop."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any

from tools.source_db_tools import canonical_store


SCHEMA_VERSION = "loop-health-summary.v1"
DEFAULT_LOOKBACK_CYCLES = 5
MIN_CYCLES_FOR_TREND = 2
BACKLOG_WARNING_THRESHOLD = 25
PENDING_AGE_WARNING_DAYS = 30
CONTRADICTION_RATE_WARNING_THRESHOLD = 0.25
RESOLUTION_COVERAGE_WARNING_THRESHOLD = 0.5

PENDING_REVIEW_STATES = frozenset(
    {
        "",
        "ambiguous",
        "machine_extracted",
        "needs_review",
        "proposed",
        "unreviewed",
    }
)
ACCEPTED_REVIEW_STATES = frozenset({"accepted", "approved", "curated", "reviewed"})
RESOLVED_REVIEW_STATES = frozenset(
    {
        "accepted",
        "approved",
        "curated",
        "demoted",
        "deprecated",
        "rejected",
        "reviewed",
    }
)

REVIEWABLE_TABLES: tuple[tuple[str, str, str], ...] = (
    ("work", "work_id", "created_at"),
    ("source_claim", "source_claim_id", "created_at"),
    ("source_relationship", "source_relationship_id", "created_at"),
    ("extraction_detected_entity", "detected_entity_id", "record_last_updated"),
    ("authority_reconciliation", "authority_reconciliation_id", "created_at"),
    ("source_access", "source_access_id", "first_seen_at"),
    ("capture_event", "capture_event_id", "captured_at"),
    ("extraction_record", "extraction_id", "created_at"),
)

PER_CYCLE_TABLES: tuple[str, ...] = (
    "work",
    "source_claim",
    "extraction_detected_entity",
    "source_relationship",
    "capture_event",
    "extraction_record",
)


def _scope_identifier(*, subject_id: str | None, workspace_id: str | None) -> str | None:
    if isinstance(workspace_id, str) and workspace_id.strip():
        return workspace_id
    if isinstance(subject_id, str) and subject_id.strip():
        return subject_id
    return None


class LoopHealthError(RuntimeError):
    """Raised when a loop-health summary cannot be built."""


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _normalize_now(value: str | None) -> tuple[str, dt.datetime]:
    if value is None:
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        return now.isoformat().replace("+00:00", "Z"), now
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise LoopHealthError(f"now timestamp must be RFC3339: {value}")
    normalized = parsed.replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z"), normalized


def _load_note(raw_text: Any) -> dict[str, Any]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return {}
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row is not None else 0)


def _state_placeholders(states: frozenset[str]) -> str:
    return ",".join("?" for _ in states)


def _review_state_count(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    provenance_event_ref: str,
    states: frozenset[str],
) -> int:
    placeholders = _state_placeholders(states)
    return _safe_count(
        conn,
        f"""
        SELECT COUNT(*)
        FROM {table_name}
        WHERE provenance_event_ref=?
          AND COALESCE(review_state, '') IN ({placeholders})
        """,
        (provenance_event_ref, *sorted(states)),
    )


def _count_for_event(conn: sqlite3.Connection, table_name: str, event_key: str) -> int:
    return _safe_count(
        conn,
        f"SELECT COUNT(*) FROM {table_name} WHERE provenance_event_ref=?",
        (event_key,),
    )


def _load_cycle_events(
    conn: sqlite3.Connection,
    *,
    scope_id: str | None,
    lookback_cycles: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT provenance_event_id, provenance_event_key_v1, event_type, run_id,
               event_timestamp, note_text
        FROM provenance_event
        WHERE event_type=?
        ORDER BY event_timestamp DESC, provenance_event_id DESC
        """,
        ("gather_candidate_batch_ingest",),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        note = _load_note(row["note_text"])
        note_subject = note.get("subject_id")
        note_workspace = note.get("workspace_id")
        note_scopes = {
            value
            for value in (note_workspace, note_subject)
            if isinstance(value, str) and value.strip()
        }
        if scope_id is not None and scope_id not in note_scopes:
            continue
        events.append(
            {
                "provenance_event_id": int(row["provenance_event_id"]),
                "event_key": str(row["provenance_event_key_v1"]),
                "run_id": row["run_id"],
                "event_timestamp": row["event_timestamp"],
                "subject_id": note_subject,
                "workspace_id": note_workspace,
                "facet": note.get("facet"),
                "cycle_depth": note.get("cycle_depth"),
                "prompt_bundle_id": note.get("prompt_bundle_id"),
            }
        )
        if len(events) >= lookback_cycles:
            break
    return list(reversed(events))


def _scoped_row_query(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    timestamp_column: str,
    scope_id: str | None,
    placeholders: str,
    params: tuple[Any, ...],
) -> list[sqlite3.Row]:
    if scope_id is None:
        return conn.execute(
            f"""
            SELECT {timestamp_column} AS timestamp_value
            FROM {table_name}
            WHERE COALESCE(review_state, '') IN ({placeholders})
            """,
            params,
        ).fetchall()
    if table_name == "extraction_detected_entity":
        return conn.execute(
            f"""
            SELECT entity.{timestamp_column} AS timestamp_value
            FROM extraction_detected_entity AS entity
            LEFT JOIN capture_event AS capture
              ON capture.capture_event_id = entity.capture_event_id
            LEFT JOIN extraction_record AS extraction
              ON extraction.extraction_id = entity.extraction_id
            WHERE COALESCE(entity.workspace_id, capture.workspace_id, extraction.workspace_id)=?
              AND COALESCE(entity.review_state, '') IN ({placeholders})
            """,
            (scope_id, *params),
        ).fetchall()
    if table_name == "authority_reconciliation":
        return conn.execute(
            f"""
            SELECT reconciliation.{timestamp_column} AS timestamp_value
            FROM authority_reconciliation AS reconciliation
            LEFT JOIN extraction_detected_entity AS entity
              ON entity.detected_entity_id = reconciliation.detected_entity_id
            LEFT JOIN extraction_record AS extraction
              ON extraction.extraction_id = entity.extraction_id
            LEFT JOIN capture_event AS capture
              ON capture.capture_event_id = entity.capture_event_id
            LEFT JOIN authority_record AS target_record
              ON target_record.authority_record_id = CAST(reconciliation.target_id AS INTEGER)
             AND reconciliation.target_namespace = 'authority_record'
            LEFT JOIN authority_record AS candidate_record
              ON candidate_record.authority_record_id = reconciliation.candidate_authority_record_id
            WHERE COALESCE(
                    entity.workspace_id,
                    target_record.workspace_id,
                    candidate_record.workspace_id,
                    capture.workspace_id,
                    extraction.workspace_id
                  )=?
              AND COALESCE(reconciliation.review_state, '') IN ({placeholders})
            """,
            (scope_id, *params),
        ).fetchall()
    return conn.execute(
        f"""
        SELECT {timestamp_column} AS timestamp_value
        FROM {table_name}
        WHERE workspace_id=?
          AND COALESCE(review_state, '') IN ({placeholders})
        """,
        (scope_id, *params),
    ).fetchall()


def _per_cycle_metrics(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    event_key = str(event["event_key"])
    table_counts = {table_name: _count_for_event(conn, table_name, event_key) for table_name in PER_CYCLE_TABLES}
    accepted_counts = {
        table_name: _review_state_count(
            conn,
            table_name,
            provenance_event_ref=event_key,
            states=ACCEPTED_REVIEW_STATES,
        )
        for table_name in ("work", "source_claim", "source_relationship", "extraction_detected_entity")
    }
    reviewable_counts = {
        table_name: _review_state_count(
            conn,
            table_name,
            provenance_event_ref=event_key,
            states=PENDING_REVIEW_STATES,
        )
        for table_name in ("work", "source_claim", "source_relationship", "extraction_detected_entity")
    }
    contradiction_count = _safe_count(
        conn,
        """
        SELECT COUNT(*)
        FROM source_relationship
        WHERE provenance_event_ref=? AND predicate='contradicts'
        """,
        (event_key,),
    )
    new_reviewable = sum(reviewable_counts.values())
    new_accepted = sum(accepted_counts.values())
    return {
        "cycle_id": event.get("run_id") or event_key,
        "cycle_depth": event.get("cycle_depth"),
        "started_at": event.get("event_timestamp"),
        "ended_at": event.get("event_timestamp"),
        "final_status": "completed",
        "facet": event.get("facet"),
        "gather_candidate_count": None,
        "candidate_ingest_count": table_counts["work"]
        + table_counts["source_claim"]
        + table_counts["extraction_detected_entity"]
        + table_counts["source_relationship"],
        "execution_capture_count": table_counts["capture_event"],
        "execution_extraction_count": table_counts["extraction_record"],
        "new_work_count": table_counts["work"],
        "new_source_claim_count": table_counts["source_claim"],
        "new_detected_entity_count": table_counts["extraction_detected_entity"],
        "new_source_relationship_count": table_counts["source_relationship"],
        "new_authority_reconciliation_count": None,
        "new_contradiction_count": contradiction_count,
        "new_reviewable_count": new_reviewable,
        "new_accepted_count": new_accepted,
        "new_rejected_or_resolved_count": None,
        "review_backlog_delta": None,
        "feedback_selected_action": None,
        "yield_score": new_reviewable + new_accepted,
        "warning_count": 0,
        "failure_stage": None,
        "table_counts": table_counts,
    }


def _backlog_metrics(conn: sqlite3.Connection, *, now: dt.datetime) -> dict[str, Any]:
    return _backlog_metrics_scoped(conn, workspace_id=None, now=now)


def _backlog_metrics_scoped(
    conn: sqlite3.Connection,
    *,
    workspace_id: str | None,
    now: dt.datetime,
) -> dict[str, Any]:
    by_family: dict[str, int] = {}
    pending_timestamps: list[dt.datetime] = []
    placeholders = _state_placeholders(PENDING_REVIEW_STATES)
    params = tuple(sorted(PENDING_REVIEW_STATES))
    for table_name, _pk_column, timestamp_column in REVIEWABLE_TABLES:
        rows = _scoped_row_query(
            conn,
            table_name=table_name,
            timestamp_column=timestamp_column,
            scope_id=workspace_id,
            placeholders=placeholders,
            params=params,
        )
        by_family[table_name] = len(rows)
        for row in rows:
            parsed = _parse_timestamp(row["timestamp_value"])
            if parsed is not None:
                pending_timestamps.append(parsed)
    ages = [max(0.0, (now - timestamp).total_seconds() / 86400.0) for timestamp in pending_timestamps]
    return {
        "pending_review_count": sum(by_family.values()),
        "pending_by_family": by_family,
        "oldest_pending_age_days": round(max(ages), 2) if ages else None,
        "median_pending_age_days": round(float(median(ages)), 2) if ages else None,
        "p90_pending_age_days": _percentile(ages, 0.9),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * percentile))
    return round(float(ordered[index]), 2)


def _contradiction_metrics(
    conn: sqlite3.Connection,
    *,
    per_cycle: list[dict[str, Any]],
    workspace_id: str | None,
) -> dict[str, Any]:
    if workspace_id is None:
        total = _safe_count(conn, "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'")
        unresolved = _safe_count(
            conn,
            """
            SELECT COUNT(*)
            FROM source_relationship
            WHERE predicate='contradicts'
              AND COALESCE(review_state, '') IN ({})
            """.format(_state_placeholders(PENDING_REVIEW_STATES)),
            tuple(sorted(PENDING_REVIEW_STATES)),
        )
    else:
        total = _safe_count(
            conn,
            """
            SELECT COUNT(*)
            FROM source_relationship
            WHERE predicate='contradicts'
              AND workspace_id=?
            """,
            (workspace_id,),
        )
        unresolved = _safe_count(
            conn,
            """
            SELECT COUNT(*)
            FROM source_relationship
            WHERE predicate='contradicts'
              AND workspace_id=?
              AND COALESCE(review_state, '') IN ({})
            """.format(_state_placeholders(PENDING_REVIEW_STATES)),
            (workspace_id, *sorted(PENDING_REVIEW_STATES)),
        )
    new_in_lookback = sum(int(cycle["new_contradiction_count"]) for cycle in per_cycle)
    new_claims = sum(int(cycle["new_source_claim_count"]) for cycle in per_cycle)
    return {
        "total_contradictions": total,
        "new_contradictions": new_in_lookback,
        "unresolved_contradictions": unresolved,
        "contradictions_per_cycle": round(new_in_lookback / len(per_cycle), 4) if per_cycle else None,
        "contradictions_per_new_source_claim": round(new_in_lookback / new_claims, 4) if new_claims else None,
    }


def _resolution_count(
    conn: sqlite3.Connection,
    *,
    cycle_events: list[dict[str, Any]],
    evaluated_at: str,
    workspace_id: str | None,
) -> tuple[bool, int | None]:
    if workspace_id is None:
        total_decisions = _safe_count(
            conn,
            "SELECT COUNT(*) FROM provenance_event WHERE event_type LIKE 'review_decision_%'",
        )
    else:
        total_decisions = _safe_count(
            conn,
            """
            SELECT COUNT(*)
            FROM provenance_event AS event
            WHERE event.event_type LIKE 'review_decision_%'
              AND (
                (event.object_namespace='source_claim' AND EXISTS (
                    SELECT 1
                    FROM source_claim AS claim
                    WHERE claim.source_claim_id = CAST(event.object_id AS INTEGER)
                      AND claim.workspace_id=?
                ))
                OR (event.object_namespace='source_relationship' AND EXISTS (
                    SELECT 1
                    FROM source_relationship AS relationship
                    WHERE relationship.source_relationship_id = CAST(event.object_id AS INTEGER)
                      AND relationship.workspace_id=?
                ))
                OR (event.object_namespace='authority_reconciliation' AND EXISTS (
                    SELECT 1
                    FROM authority_reconciliation AS reconciliation
                    LEFT JOIN extraction_detected_entity AS entity
                      ON entity.detected_entity_id = reconciliation.detected_entity_id
                    LEFT JOIN extraction_record AS extraction
                      ON extraction.extraction_id = entity.extraction_id
                    LEFT JOIN capture_event AS capture
                      ON capture.capture_event_id = entity.capture_event_id
                    LEFT JOIN authority_record AS target_record
                      ON target_record.authority_record_id = CAST(reconciliation.target_id AS INTEGER)
                     AND reconciliation.target_namespace = 'authority_record'
                    LEFT JOIN authority_record AS candidate_record
                      ON candidate_record.authority_record_id = reconciliation.candidate_authority_record_id
                    WHERE reconciliation.authority_reconciliation_id = CAST(event.object_id AS INTEGER)
                      AND COALESCE(
                        entity.workspace_id,
                        target_record.workspace_id,
                        candidate_record.workspace_id,
                        capture.workspace_id,
                        extraction.workspace_id
                      )=?
                ))
              )
            """,
            (workspace_id, workspace_id, workspace_id),
        )
    if total_decisions == 0:
        return False, None
    if cycle_events:
        first = cycle_events[0].get("event_timestamp")
        if workspace_id is None:
            count = _safe_count(
                conn,
                """
                SELECT COUNT(*)
                FROM provenance_event
                WHERE event_type LIKE 'review_decision_%'
                  AND event_timestamp >= ?
                  AND event_timestamp <= ?
                """,
                (first, evaluated_at),
            )
        else:
            count = _safe_count(
                conn,
                """
                SELECT COUNT(*)
                FROM provenance_event AS event
                WHERE event.event_type LIKE 'review_decision_%'
                  AND event.event_timestamp >= ?
                  AND event.event_timestamp <= ?
                  AND (
                    (event.object_namespace='source_claim' AND EXISTS (
                        SELECT 1
                        FROM source_claim AS claim
                        WHERE claim.source_claim_id = CAST(event.object_id AS INTEGER)
                          AND claim.workspace_id=?
                    ))
                    OR (event.object_namespace='source_relationship' AND EXISTS (
                        SELECT 1
                        FROM source_relationship AS relationship
                        WHERE relationship.source_relationship_id = CAST(event.object_id AS INTEGER)
                          AND relationship.workspace_id=?
                    ))
                    OR (event.object_namespace='authority_reconciliation' AND EXISTS (
                        SELECT 1
                        FROM authority_reconciliation AS reconciliation
                        LEFT JOIN extraction_detected_entity AS entity
                          ON entity.detected_entity_id = reconciliation.detected_entity_id
                        LEFT JOIN extraction_record AS extraction
                          ON extraction.extraction_id = entity.extraction_id
                        LEFT JOIN capture_event AS capture
                          ON capture.capture_event_id = entity.capture_event_id
                        LEFT JOIN authority_record AS target_record
                          ON target_record.authority_record_id = CAST(reconciliation.target_id AS INTEGER)
                         AND reconciliation.target_namespace = 'authority_record'
                        LEFT JOIN authority_record AS candidate_record
                          ON candidate_record.authority_record_id = reconciliation.candidate_authority_record_id
                        WHERE reconciliation.authority_reconciliation_id = CAST(event.object_id AS INTEGER)
                          AND COALESCE(
                            entity.workspace_id,
                            target_record.workspace_id,
                            candidate_record.workspace_id,
                            capture.workspace_id,
                            extraction.workspace_id
                          )=?
                    ))
                  )
                """,
                (first, evaluated_at, workspace_id, workspace_id, workspace_id),
            )
    else:
        count = total_decisions
    return True, count


def _yield_trend(per_cycle: list[dict[str, Any]]) -> str:
    if len(per_cycle) < MIN_CYCLES_FOR_TREND:
        return "insufficient_data"
    deltas = [
        int(per_cycle[index]["new_reviewable_count"]) - int(per_cycle[index - 1]["new_reviewable_count"])
        for index in range(1, len(per_cycle))
    ]
    average_delta = sum(deltas) / len(deltas)
    if average_delta > 0:
        return "rising"
    if average_delta < 0:
        return "declining"
    return "flat"


def _health_status(
    *,
    per_cycle: list[dict[str, Any]],
    backlog: dict[str, Any],
    contradictions: dict[str, Any],
    resolution_available: bool,
    resolution_coverage: float | None,
    yield_trend: str,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if not per_cycle:
        return "insufficient_data", ["no cycle ingest history found"]
    contradiction_rate = contradictions["contradictions_per_new_source_claim"]
    if contradiction_rate is not None and contradiction_rate > CONTRADICTION_RATE_WARNING_THRESHOLD:
        warnings.append("contradiction rate exceeds loop-health threshold")
        return "contradiction_spike", warnings
    if resolution_available and resolution_coverage is not None and resolution_coverage < RESOLUTION_COVERAGE_WARNING_THRESHOLD:
        warnings.append("review decisions are not keeping pace with reviewable ingestion")
        return "review_lagging", warnings
    oldest_age = backlog.get("oldest_pending_age_days")
    if oldest_age is not None and oldest_age > PENDING_AGE_WARNING_DAYS:
        warnings.append("oldest pending review item exceeds age threshold")
        return "review_lagging", warnings
    if int(backlog["pending_review_count"]) > BACKLOG_WARNING_THRESHOLD:
        warnings.append("pending review backlog exceeds loop-health threshold")
        return "accumulating", warnings
    if len(per_cycle) >= MIN_CYCLES_FOR_TREND and yield_trend in {"flat", "declining"}:
        total_reviewable = sum(int(cycle["new_reviewable_count"]) for cycle in per_cycle)
        if total_reviewable == 0:
            warnings.append("recent completed cycles produced no reviewable records")
            return "stalled", warnings
    return "healthy", warnings


def build_loop_health_summary(
    conn: sqlite3.Connection,
    *,
    subject_id: str | None = None,
    workspace_id: str | None = None,
    lookback_cycles: int = DEFAULT_LOOKBACK_CYCLES,
    now: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only loop-health summary from canonical rows."""

    if lookback_cycles < 1:
        raise LoopHealthError("lookback_cycles must be at least 1")
    evaluated_at, now_dt = _normalize_now(now)
    scope_id = _scope_identifier(subject_id=subject_id, workspace_id=workspace_id)
    events = _load_cycle_events(conn, scope_id=scope_id, lookback_cycles=lookback_cycles)
    per_cycle = [_per_cycle_metrics(conn, event) for event in events]
    backlog = _backlog_metrics_scoped(conn, workspace_id=scope_id, now=now_dt)
    contradictions = _contradiction_metrics(conn, per_cycle=per_cycle, workspace_id=scope_id)
    resolution_available, resolution_count = _resolution_count(
        conn,
        cycle_events=events,
        evaluated_at=evaluated_at,
        workspace_id=scope_id,
    )
    reviewable_ingested = sum(int(cycle["new_reviewable_count"]) for cycle in per_cycle)
    accepted_count = sum(int(cycle["new_accepted_count"]) for cycle in per_cycle)
    resolution_coverage = (
        round(float(resolution_count) / float(reviewable_ingested), 4)
        if resolution_available and resolution_count is not None and reviewable_ingested > 0
        else None
    )
    trend = _yield_trend(per_cycle)
    status, warnings = _health_status(
        per_cycle=per_cycle,
        backlog=backlog,
        contradictions=contradictions,
        resolution_available=resolution_available,
        resolution_coverage=resolution_coverage,
        yield_trend=trend,
    )
    limitations: list[str] = []
    if not events:
        limitations.append("cycle_history_unavailable")
    if not resolution_available:
        limitations.append("review_decision_provenance_unavailable")
    if any(cycle["new_authority_reconciliation_count"] is None for cycle in per_cycle):
        limitations.append("authority_reconciliation_per_cycle_count_unavailable")
    return {
        "schema_version": SCHEMA_VERSION,
        "subject_id": subject_id,
        "workspace_id": workspace_id,
        "evaluated_at": evaluated_at,
        "lookback_cycles": lookback_cycles,
        "cycle_ids_considered": [cycle["cycle_id"] for cycle in per_cycle],
        "data_availability": {
            "cycle_history_available": bool(events),
            "review_resolution_available": resolution_available,
            "feedback_yield_available": bool(events),
            "contradiction_data_available": True,
        },
        "per_cycle_metrics": per_cycle,
        "aggregate_metrics": {
            "yield_trend": trend,
            "new_reviewable_records": reviewable_ingested,
            "new_accepted_records": accepted_count,
            "new_source_claims": sum(int(cycle["new_source_claim_count"]) for cycle in per_cycle),
            "new_detected_entities": sum(int(cycle["new_detected_entity_count"]) for cycle in per_cycle),
            "new_works": sum(int(cycle["new_work_count"]) for cycle in per_cycle),
            "new_source_relationships": sum(int(cycle["new_source_relationship_count"]) for cycle in per_cycle),
        },
        "review_backlog": backlog,
        "contradictions": contradictions,
        "ingestion_resolution": {
            "reviewable_ingested_count": reviewable_ingested,
            "review_decision_applied_count": resolution_count,
            "resolution_coverage": resolution_coverage,
            "ingestion_outpacing_resolution": (
                bool(reviewable_ingested > int(resolution_count))
                if resolution_available and resolution_count is not None
                else None
            ),
        },
        "health_status": status,
        "status": status,
        "thresholds": {
            "lookback_cycles": lookback_cycles,
            "minimum_cycles_for_trend": MIN_CYCLES_FOR_TREND,
            "review_backlog_warning_threshold": BACKLOG_WARNING_THRESHOLD,
            "pending_age_warning_days": PENDING_AGE_WARNING_DAYS,
            "contradiction_rate_warning_threshold": CONTRADICTION_RATE_WARNING_THRESHOLD,
            "resolution_coverage_warning_threshold": RESOLUTION_COVERAGE_WARNING_THRESHOLD,
        },
        "warnings": warnings,
        "limitations": limitations,
        "read_only": True,
    }


def summarize_loop_health(
    db_path: Path | str,
    *,
    subject_id: str | None = None,
    workspace_id: str | None = None,
    lookback_cycles: int = DEFAULT_LOOKBACK_CYCLES,
    now: str | None = None,
) -> dict[str, Any]:
    """Open an initialized canonical store read-only and summarize loop health."""

    path = canonical_store.resolve_db_path(db_path)
    base = {
        "schema_version": SCHEMA_VERSION,
        "subject_id": subject_id,
        "workspace_id": workspace_id,
        "evaluated_at": _normalize_now(now)[0],
        "lookback_cycles": lookback_cycles,
        "cycle_ids_considered": [],
        "data_availability": {
            "cycle_history_available": False,
            "review_resolution_available": False,
            "feedback_yield_available": False,
            "contradiction_data_available": False,
        },
        "per_cycle_metrics": [],
        "aggregate_metrics": {},
        "review_backlog": {},
        "contradictions": {},
        "ingestion_resolution": {},
        "health_status": "unavailable",
        "status": "unavailable",
        "warnings": [],
        "limitations": [],
        "read_only": True,
    }
    population = canonical_store.summarize_canonical_store_population(path)
    if population["status"] in {"absent", "uninitialized", "invalid"}:
        base["limitations"].append(f"canonical_store_{population['status']}")
        base["warnings"].extend(population.get("errors", []))
        return base
    conn = canonical_store.connect_existing_read_only(path)
    try:
        return build_loop_health_summary(
            conn,
            subject_id=subject_id,
            workspace_id=workspace_id,
            lookback_cycles=lookback_cycles,
            now=now,
        )
    finally:
        conn.close()
