"""Deterministic topic saturation policy evaluation.

Saturation is an operational scheduling signal. It does not adjudicate truth,
delete records, or apply review decisions.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "topic-saturation.v1"
POLICY_SCHEMA_VERSION = "topic-saturation-policy.v1"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "topic_saturation_policy.v1.json"
)
GATHER_EVENT_SOURCE_NAMESPACE = "topic_subject"
USEFUL_FAMILY_TABLES = (
    "work",
    "source_claim",
    "source_access",
    "extraction_detected_entity",
    "source_relationship",
    "capture_event",
    "extraction_record",
    "authority_reconciliation",
)
REVIEW_STATE_TABLES = (
    "work",
    "source_claim",
    "source_access",
    "extraction_detected_entity",
    "source_relationship",
)


class TopicSaturationError(RuntimeError):
    """Raised when saturation policy or evaluation inputs are invalid."""


@dataclass(frozen=True)
class Policy:
    raw: dict[str, Any]

    @property
    def policy_id(self) -> str:
        return str(self.raw["policy_id"])

    @property
    def enabled(self) -> bool:
        return bool(self.raw["enabled"])

    @property
    def lookback_cycles(self) -> int:
        return int(self.raw["lookback_cycles"])

    @property
    def mode(self) -> str:
        return str(self.raw["mode"])


def load_policy(path: str | Path | None = None) -> Policy:
    policy_path = DEFAULT_POLICY_PATH if path is None else Path(path).expanduser()
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TopicSaturationError(
            f"could not read saturation policy: {policy_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise TopicSaturationError("saturation policy must be a JSON object")
    validate_policy(payload)
    return Policy(payload)


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise TopicSaturationError(
            f"saturation policy schema_version must be {POLICY_SCHEMA_VERSION}"
        )
    if not isinstance(policy.get("policy_id"), str) or not policy["policy_id"].strip():
        raise TopicSaturationError("saturation policy policy_id is required")
    if not isinstance(policy.get("enabled"), bool):
        raise TopicSaturationError("saturation policy enabled must be boolean")
    if policy.get("mode") not in {"mixed", "accepted_only", "reviewable_yield"}:
        raise TopicSaturationError("saturation policy mode is invalid")
    for field in (
        "lookback_cycles",
        "min_new_accepted_records",
        "min_new_reviewable_records",
        "max_consecutive_low_yield_cycles",
        "review_backlog_pressure_threshold",
        "cooldown_cycles",
    ):
        value = policy.get(field)
        if not isinstance(value, int):
            raise TopicSaturationError(f"saturation policy {field} must be an integer")
        minimum = 1 if field in {"lookback_cycles", "max_consecutive_low_yield_cycles"} else 0
        if value < minimum:
            raise TopicSaturationError(f"saturation policy {field} must be at least {minimum}")
    min_useful = policy.get("min_useful_yield")
    if not isinstance(min_useful, (int, float)) or min_useful < 0:
        raise TopicSaturationError("saturation policy min_useful_yield must be nonnegative")
    if policy.get("scheduler_action_on_saturated") not in {"deprioritize", "cooldown", "halt"}:
        raise TopicSaturationError("saturation policy scheduler_action_on_saturated is invalid")
    weights = policy.get("family_weights")
    if not isinstance(weights, dict):
        raise TopicSaturationError("saturation policy family_weights must be an object")
    for key in (
        "accepted_record",
        "reviewable_record",
        *USEFUL_FAMILY_TABLES,
    ):
        value = weights.get(key)
        if not isinstance(value, (int, float)) or value < 0:
            raise TopicSaturationError(
                f"saturation policy family_weights.{key} must be nonnegative"
            )
    for field in ("accepted_review_states", "reviewable_review_states", "backlog_review_states"):
        values = policy.get(field)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(item, str) and item for item in values)
        ):
            raise TopicSaturationError(
                f"saturation policy {field} must be a non-empty string array"
            )


def parse_note_text(note_text: Any) -> dict[str, Any]:
    if not isinstance(note_text, str) or not note_text.strip():
        return {}
    try:
        payload = json.loads(note_text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_recent_gather_events_for_subjects(
    conn: sqlite3.Connection,
    *,
    subject_ids: list[str],
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    unique_subject_ids = list(dict.fromkeys(subject_ids))
    if not unique_subject_ids:
        return {}
    rows = conn.execute(
        f"""
        WITH requested_subjects(subject_id) AS (
          VALUES {", ".join("(?)" for _ in unique_subject_ids)}
        ),
        ranked_events AS (
          SELECT
            requested_subjects.subject_id AS subject_id,
            provenance_event.provenance_event_id AS provenance_event_id,
            provenance_event.provenance_event_key_v1 AS provenance_event_key_v1,
            provenance_event.run_id AS run_id,
            provenance_event.event_timestamp AS event_timestamp,
            CASE WHEN json_valid(provenance_event.note_text) THEN json_extract(provenance_event.note_text, '$.facet') END AS facet,
            CASE WHEN json_valid(provenance_event.note_text) THEN json_extract(provenance_event.note_text, '$.cycle_depth') END AS cycle_depth,
            CASE WHEN json_valid(provenance_event.note_text) THEN json_extract(provenance_event.note_text, '$.artifact_hash') END AS artifact_hash,
            row_number() OVER (
              PARTITION BY requested_subjects.subject_id
              ORDER BY datetime(provenance_event.event_timestamp) DESC, provenance_event.provenance_event_id DESC
            ) AS rn
          FROM provenance_event
          INNER JOIN requested_subjects
            ON requested_subjects.subject_id = provenance_event.source_object_id
          WHERE provenance_event.event_type='gather_candidate_batch_ingest'
            AND provenance_event.source_object_namespace=?
        )
        SELECT
          subject_id,
          provenance_event_id,
          provenance_event_key_v1,
          run_id,
          event_timestamp,
          facet,
          cycle_depth,
          artifact_hash
        FROM ranked_events
        WHERE rn <= ?
        ORDER BY subject_id, datetime(event_timestamp) DESC, provenance_event_id DESC
        """,
        tuple(unique_subject_ids) + (GATHER_EVENT_SOURCE_NAMESPACE, limit),
    ).fetchall()
    events_by_subject: dict[str, list[dict[str, Any]]] = {
        subject_id: [] for subject_id in unique_subject_ids
    }
    for row in rows:
        subject_id = str(row["subject_id"])
        facet = row["facet"]
        cycle_depth = row["cycle_depth"]
        artifact_hash = row["artifact_hash"]
        events_by_subject.setdefault(subject_id, []).append(
            {
                "subject_id": subject_id,
                "provenance_event_id": int(row["provenance_event_id"]),
                "event_key": str(row["provenance_event_key_v1"]),
                "run_id": None if row["run_id"] is None else str(row["run_id"]),
                "event_timestamp": str(row["event_timestamp"]),
                "facet": None if facet is None else str(facet),
                "cycle_depth": None if cycle_depth is None else int(cycle_depth),
                "_artifact_hash": artifact_hash if isinstance(artifact_hash, str) else None,
            }
        )
    return events_by_subject


def load_recent_gather_events(
    conn: sqlite3.Connection, *, subject_id: str, limit: int
) -> list[dict[str, Any]]:
    return load_recent_gather_events_for_subjects(conn, subject_ids=[subject_id], limit=limit).get(
        subject_id,
        [],
    )


def _count_by_provenance(conn: sqlite3.Connection, table: str, event_key: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE provenance_event_ref=?",
        (event_key,),
    ).fetchone()
    return int(row["count"])


def _count_by_provenance_and_states(
    conn: sqlite3.Connection,
    table: str,
    event_key: str,
    states: list[str],
) -> int:
    placeholders = ", ".join("?" for _ in states)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE provenance_event_ref=? AND review_state IN ({placeholders})",
        (event_key, *states),
    ).fetchone()
    return int(row["count"])


def _count_by_provenance_grouped(
    conn: sqlite3.Connection,
    table: str,
    event_keys: list[str],
) -> dict[str, int]:
    if not event_keys:
        return {}
    rows = conn.execute(
        f"""
        WITH requested_events(event_key) AS (
          VALUES {", ".join("(?)" for _ in event_keys)}
        )
        SELECT requested_events.event_key AS event_key, COUNT(*) AS count
        FROM requested_events
        INNER JOIN {table}
          ON {table}.provenance_event_ref = requested_events.event_key
        GROUP BY requested_events.event_key
        """,
        tuple(event_keys),
    ).fetchall()
    result = {event_key: 0 for event_key in event_keys}
    result.update({str(row["event_key"]): int(row["count"]) for row in rows})
    return result


def _count_by_provenance_and_states_grouped(
    conn: sqlite3.Connection,
    table: str,
    event_keys: list[str],
    states: list[str],
) -> dict[str, int]:
    if not event_keys:
        return {}
    if not states:
        return {event_key: 0 for event_key in event_keys}
    rows = conn.execute(
        f"""
        WITH requested_events(event_key) AS (
          VALUES {", ".join("(?)" for _ in event_keys)}
        )
        SELECT requested_events.event_key AS event_key, COUNT(*) AS count
        FROM requested_events
        INNER JOIN {table}
          ON {table}.provenance_event_ref = requested_events.event_key
        WHERE {table}.review_state IN ({", ".join("?" for _ in states)})
        GROUP BY requested_events.event_key
        """,
        tuple(event_keys) + tuple(states),
    ).fetchall()
    result = {event_key: 0 for event_key in event_keys}
    result.update({str(row["event_key"]): int(row["count"]) for row in rows})
    return result


def source_access_count_for_event(
    conn: sqlite3.Connection,
    event_key: str,
    *,
    states: list[str] | None = None,
) -> int:
    ids: set[int] = set()
    state_clause = ""
    source_state_clause = ""
    state_params: tuple[Any, ...] = ()
    if states:
        placeholders = ", ".join("?" for _ in states)
        state_clause = f" AND access.review_state IN ({placeholders})"
        source_state_clause = f" AND review_state IN ({placeholders})"
        state_params = tuple(states)
    for row in conn.execute(
        f"""
        SELECT access.source_access_id
        FROM source_access AS access
        INNER JOIN work ON work.work_id = access.work_id
        WHERE work.provenance_event_ref=?
        {state_clause}
        """,
        (event_key, *state_params),
    ).fetchall():
        ids.add(int(row["source_access_id"]))
    for row in conn.execute(
        f"""
        SELECT source_access_id
        FROM source_access
        WHERE provenance_event_ref=?
        {source_state_clause}
        """,
        (event_key, *state_params),
    ).fetchall():
        ids.add(int(row["source_access_id"]))
    note_row = conn.execute(
        "SELECT note_text FROM provenance_event WHERE provenance_event_key_v1=?",
        (event_key,),
    ).fetchone()
    artifact_hash = (
        parse_note_text(note_row["note_text"]).get("artifact_hash")
        if note_row is not None
        else None
    )
    if isinstance(artifact_hash, str) and artifact_hash:
        for row in conn.execute(
            f"""
            SELECT source_access_id
            FROM source_access
            WHERE source_lead_id LIKE ?
            {source_state_clause}
            """,
            (f"source-lead:{artifact_hash}:%", *state_params),
        ).fetchall():
            ids.add(int(row["source_access_id"]))
    return len(ids)


def source_access_counts_for_events(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    *,
    states: list[str] | None = None,
) -> dict[str, int]:
    if not events:
        return {}
    rows = conn.execute(
        f"""
        WITH requested_events(event_key, artifact_hash) AS (
          VALUES {", ".join("(?, ?)" for _ in events)}
        ),
        matched AS (
          SELECT requested_events.event_key AS event_key, access.source_access_id AS source_access_id
          FROM requested_events
          INNER JOIN work
            ON work.provenance_event_ref = requested_events.event_key
          INNER JOIN source_access AS access
            ON access.work_id = work.work_id
          {f"WHERE access.review_state IN ({', '.join('?' for _ in states)})" if states else ""}
          UNION ALL
          SELECT requested_events.event_key AS event_key, access.source_access_id AS source_access_id
          FROM requested_events
          INNER JOIN source_access AS access
            ON access.provenance_event_ref = requested_events.event_key
          {f"WHERE access.review_state IN ({', '.join('?' for _ in states)})" if states else ""}
          UNION ALL
          SELECT requested_events.event_key AS event_key, access.source_access_id AS source_access_id
          FROM requested_events
          INNER JOIN source_access AS access
            ON access.source_lead_id LIKE 'source-lead:' || requested_events.artifact_hash || ':%'
          {f"WHERE access.review_state IN ({', '.join('?' for _ in states)})" if states else ""}
        )
        SELECT event_key, COUNT(DISTINCT source_access_id) AS count
        FROM matched
        GROUP BY event_key
        """,
        tuple(
            value for event in events for value in (event["event_key"], event.get("_artifact_hash"))
        )
        + (tuple(states) * 3 if states else ()),
    ).fetchall()
    result = {str(event["event_key"]): 0 for event in events}
    result.update({str(row["event_key"]): int(row["count"]) for row in rows})
    return result


def authority_reconciliation_count_for_event(
    conn: sqlite3.Connection,
    event_key: str,
    *,
    states: list[str] | None = None,
) -> int:
    state_clause = ""
    state_params: tuple[Any, ...] = ()
    if states:
        placeholders = ", ".join("?" for _ in states)
        state_clause = f" AND reconciliation.review_state IN ({placeholders})"
        state_params = tuple(states)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM authority_reconciliation AS reconciliation
        INNER JOIN extraction_detected_entity AS entity
          ON entity.detected_entity_id = reconciliation.detected_entity_id
        WHERE entity.provenance_event_ref=?
        {state_clause}
        """,
        (event_key, *state_params),
    ).fetchone()
    return int(row["count"])


def authority_reconciliation_counts_for_events(
    conn: sqlite3.Connection,
    event_keys: list[str],
    *,
    states: list[str] | None = None,
) -> dict[str, int]:
    if not event_keys:
        return {}
    rows = conn.execute(
        f"""
        WITH requested_events(event_key) AS (
          VALUES {", ".join("(?)" for _ in event_keys)}
        )
        SELECT requested_events.event_key AS event_key, COUNT(*) AS count
        FROM requested_events
        INNER JOIN extraction_detected_entity AS entity
          ON entity.provenance_event_ref = requested_events.event_key
        INNER JOIN authority_reconciliation AS reconciliation
          ON reconciliation.detected_entity_id = entity.detected_entity_id
        {f"WHERE reconciliation.review_state IN ({', '.join('?' for _ in states)})" if states else ""}
        GROUP BY requested_events.event_key
        """,
        tuple(event_keys) + (tuple(states) if states else ()),
    ).fetchall()
    result = {event_key: 0 for event_key in event_keys}
    result.update({str(row["event_key"]): int(row["count"]) for row in rows})
    return result


def _public_cycle_event(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "_artifact_hash"}


def _disabled_saturation_result(
    *,
    workspace_id: str,
    subject_id: str,
    policy: Policy,
    evaluated_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "policy_id": policy.policy_id,
        "evaluated_at": evaluated_at,
        "enabled": False,
        "state": "active",
        "scheduler_action": "run",
        "reason_codes": ["policy_disabled"],
        "lookback_cycles": policy.lookback_cycles,
        "cycles_considered": [],
        "recent_yield_summary": empty_summary(),
        "next_eligible_cycle": None,
        "warnings": [],
    }


def _evaluate_saturation_result(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_id: str,
    policy: Policy,
    evaluated_at: str,
    cycles: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = summarize_cycles(cycles)
    warnings: list[str] = []
    reason_codes: list[str] = []
    if len(cycles) < policy.lookback_cycles:
        state = "active_bootstrap"
        scheduler_action = "run"
        reason_codes.append("insufficient_history")
    else:
        low_streak = consecutive_low_yield(cycles)
        backlog = scoped_backlog_count(
            conn,
            subject_id=workspace_id,
            states=list(policy.raw["backlog_review_states"]),
        )
        summary["review_backlog_count"] = backlog
        if backlog >= int(policy.raw["review_backlog_pressure_threshold"]):
            reason_codes.append("review_backlog_pressure")
        if low_streak >= int(policy.raw["max_consecutive_low_yield_cycles"]):
            action = str(policy.raw["scheduler_action_on_saturated"])
            state = "cooldown" if action == "cooldown" else "saturated"
            scheduler_action = action
            reason_codes.append("consecutive_low_yield")
        else:
            state = "active"
            scheduler_action = "run"
            reason_codes.append("recent_useful_yield")
    if not cycles:
        summary["review_backlog_count"] = scoped_backlog_count(
            conn,
            subject_id=workspace_id,
            states=list(policy.raw["backlog_review_states"]),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "subject_id": subject_id,
        "policy_id": policy.policy_id,
        "evaluated_at": evaluated_at,
        "enabled": True,
        "state": state,
        "scheduler_action": scheduler_action,
        "reason_codes": reason_codes,
        "lookback_cycles": policy.lookback_cycles,
        "cycles_considered": cycles,
        "recent_yield_summary": summary,
        "next_eligible_cycle": next_eligible_cycle(cycles, policy=policy, state=state),
        "warnings": warnings,
    }


def cycle_yields(
    conn: sqlite3.Connection,
    *,
    events: list[dict[str, Any]],
    policy: Policy,
) -> list[dict[str, Any]]:
    if not events:
        return []
    raw = policy.raw
    accepted_states = list(raw["accepted_review_states"])
    reviewable_states = list(raw["reviewable_review_states"])
    useful_states = list(dict.fromkeys(accepted_states + reviewable_states))
    event_keys = [str(event["event_key"]) for event in events]
    family_counts_by_table = {
        "work": _count_by_provenance_grouped(conn, "work", event_keys),
        "source_claim": _count_by_provenance_grouped(conn, "source_claim", event_keys),
        "extraction_detected_entity": _count_by_provenance_grouped(
            conn, "extraction_detected_entity", event_keys
        ),
        "source_relationship": _count_by_provenance_grouped(
            conn, "source_relationship", event_keys
        ),
        "capture_event": _count_by_provenance_grouped(conn, "capture_event", event_keys),
        "extraction_record": _count_by_provenance_grouped(conn, "extraction_record", event_keys),
    }
    source_access_counts = source_access_counts_for_events(conn, events, states=useful_states)
    authority_reconciliation_counts = authority_reconciliation_counts_for_events(
        conn,
        event_keys,
        states=useful_states,
    )
    accepted_counts_by_table = {
        table: _count_by_provenance_and_states_grouped(conn, table, event_keys, accepted_states)
        for table in REVIEW_STATE_TABLES
    }
    reviewable_counts_by_table = {
        table: _count_by_provenance_and_states_grouped(conn, table, event_keys, reviewable_states)
        for table in REVIEW_STATE_TABLES
    }
    weights = raw["family_weights"]
    cycles: list[dict[str, Any]] = []
    for event in events:
        event_key = str(event["event_key"])
        family_counts = {table: 0 for table in USEFUL_FAMILY_TABLES}
        family_counts.update(
            {
                "work": family_counts_by_table["work"].get(event_key, 0),
                "source_claim": family_counts_by_table["source_claim"].get(event_key, 0),
                "extraction_detected_entity": family_counts_by_table[
                    "extraction_detected_entity"
                ].get(event_key, 0),
                "source_relationship": family_counts_by_table["source_relationship"].get(
                    event_key, 0
                ),
                "capture_event": family_counts_by_table["capture_event"].get(event_key, 0),
                "extraction_record": family_counts_by_table["extraction_record"].get(event_key, 0),
                "source_access": source_access_counts.get(event_key, 0),
                "authority_reconciliation": authority_reconciliation_counts.get(event_key, 0),
            }
        )
        accepted_records = sum(
            accepted_counts_by_table[table].get(event_key, 0) for table in REVIEW_STATE_TABLES
        )
        reviewable_records = sum(
            reviewable_counts_by_table[table].get(event_key, 0) for table in REVIEW_STATE_TABLES
        )
        useful_yield = (
            weights["accepted_record"] * accepted_records
            + weights["reviewable_record"] * reviewable_records
            + sum(weights[family] * family_counts[family] for family in USEFUL_FAMILY_TABLES)
        )
        low_yield = is_low_yield(
            mode=policy.mode,
            accepted_records=accepted_records,
            reviewable_records=reviewable_records,
            useful_yield=useful_yield,
            policy=raw,
        )
        cycles.append(
            {
                **_public_cycle_event(event),
                "family_counts": family_counts,
                "new_accepted_records": accepted_records,
                "new_reviewable_records": reviewable_records,
                "useful_yield": round(float(useful_yield), 4),
                "low_yield": low_yield,
            }
        )
    return cycles


def cycle_yield(
    conn: sqlite3.Connection, *, event: dict[str, Any], policy: Policy
) -> dict[str, Any]:
    cycles = cycle_yields(conn, events=[event], policy=policy)
    if cycles:
        return cycles[0]
    return {
        **_public_cycle_event(event),
        "family_counts": {table: 0 for table in USEFUL_FAMILY_TABLES},
        "new_accepted_records": 0,
        "new_reviewable_records": 0,
        "useful_yield": 0.0,
        "low_yield": True,
    }


def evaluate_saturations(
    conn: sqlite3.Connection,
    *,
    workspace_subject_pairs: list[tuple[str, str]],
    policy: Policy,
    evaluated_at: str,
) -> dict[str, dict[str, Any]]:
    if not workspace_subject_pairs:
        return {}
    if not policy.enabled:
        return {
            workspace_id: _disabled_saturation_result(
                workspace_id=workspace_id,
                subject_id=subject_id,
                policy=policy,
                evaluated_at=evaluated_at,
            )
            for workspace_id, subject_id in workspace_subject_pairs
        }

    unique_subject_ids = list(
        dict.fromkeys(subject_id for _, subject_id in workspace_subject_pairs)
    )
    events_by_subject = load_recent_gather_events_for_subjects(
        conn,
        subject_ids=unique_subject_ids,
        limit=policy.lookback_cycles,
    )
    ordered_events = [
        event
        for subject_id in unique_subject_ids
        for event in events_by_subject.get(subject_id, [])
    ]
    cycles = cycle_yields(conn, events=ordered_events, policy=policy)
    cycles_by_subject: dict[str, list[dict[str, Any]]] = {
        subject_id: [] for subject_id in unique_subject_ids
    }
    for cycle in cycles:
        subject_id = cycle.get("subject_id")
        if isinstance(subject_id, str):
            cycles_by_subject.setdefault(subject_id, []).append(cycle)

    results_by_subject = {
        subject_id: _evaluate_saturation_result(
            conn,
            workspace_id=subject_id,
            subject_id=subject_id,
            policy=policy,
            evaluated_at=evaluated_at,
            cycles=cycles_by_subject.get(subject_id, []),
        )
        for subject_id in unique_subject_ids
    }
    results: dict[str, dict[str, Any]] = {}
    for workspace_id, subject_id in workspace_subject_pairs:
        subject_result = results_by_subject[subject_id]
        result = copy.deepcopy(subject_result)
        result["workspace_id"] = workspace_id
        results[workspace_id] = result
    return results


def is_low_yield(
    *,
    mode: str,
    accepted_records: int,
    reviewable_records: int,
    useful_yield: float,
    policy: dict[str, Any],
) -> bool:
    accepted_low = accepted_records < int(policy["min_new_accepted_records"])
    reviewable_low = reviewable_records < int(policy["min_new_reviewable_records"])
    useful_low = useful_yield < float(policy["min_useful_yield"])
    if mode == "accepted_only":
        return accepted_low
    if mode == "reviewable_yield":
        return reviewable_low and useful_low
    return accepted_low and reviewable_low and useful_low


def scoped_backlog_count(conn: sqlite3.Connection, *, subject_id: str, states: list[str]) -> int:
    placeholders = ", ".join("?" for _ in states)
    total = 0
    for table in ("work", "source_claim", "source_relationship", "source_access"):
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE workspace_id=? AND review_state IN ({placeholders})",
            (subject_id, *states),
        ).fetchone()
        total += int(row["count"])
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM extraction_detected_entity AS entity
        LEFT JOIN capture_event AS capture
          ON capture.capture_event_id = entity.capture_event_id
        LEFT JOIN extraction_record AS extraction
          ON extraction.extraction_id = entity.extraction_id
        WHERE COALESCE(capture.workspace_id, extraction.workspace_id)=?
          AND entity.review_state IN ({placeholders})
        """,
        (subject_id, *states),
    ).fetchone()
    total += int(row["count"])
    return total


def consecutive_low_yield(cycles: list[dict[str, Any]]) -> int:
    count = 0
    for cycle in cycles:
        if cycle["low_yield"]:
            count += 1
        else:
            break
    return count


def evaluate_saturation(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_id: str,
    policy: Policy,
    evaluated_at: str,
) -> dict[str, Any]:
    if not policy.enabled:
        return _disabled_saturation_result(
            workspace_id=workspace_id,
            subject_id=subject_id,
            policy=policy,
            evaluated_at=evaluated_at,
        )
    events = load_recent_gather_events(conn, subject_id=subject_id, limit=policy.lookback_cycles)
    cycles = cycle_yields(conn, events=events, policy=policy)
    return _evaluate_saturation_result(
        conn,
        workspace_id=workspace_id,
        subject_id=subject_id,
        policy=policy,
        evaluated_at=evaluated_at,
        cycles=cycles,
    )


def empty_summary() -> dict[str, Any]:
    return {
        "cycle_count": 0,
        "low_yield_cycle_count": 0,
        "consecutive_low_yield_cycles": 0,
        "new_accepted_records": 0,
        "new_reviewable_records": 0,
        "useful_yield": 0.0,
        "review_backlog_count": 0,
    }


def summarize_cycles(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    summary = empty_summary()
    summary["cycle_count"] = len(cycles)
    summary["low_yield_cycle_count"] = sum(1 for cycle in cycles if cycle["low_yield"])
    summary["consecutive_low_yield_cycles"] = consecutive_low_yield(cycles)
    summary["new_accepted_records"] = sum(int(cycle["new_accepted_records"]) for cycle in cycles)
    summary["new_reviewable_records"] = sum(
        int(cycle["new_reviewable_records"]) for cycle in cycles
    )
    summary["useful_yield"] = round(sum(float(cycle["useful_yield"]) for cycle in cycles), 4)
    return summary


def next_eligible_cycle(cycles: list[dict[str, Any]], *, policy: Policy, state: str) -> int | None:
    if state != "cooldown":
        return None
    depths = [
        cycle.get("cycle_depth") for cycle in cycles if isinstance(cycle.get("cycle_depth"), int)
    ]
    current = max(depths) if depths else len(cycles)
    return current + int(policy.raw["cooldown_cycles"]) + 1
