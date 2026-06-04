"""Deterministic topic saturation policy evaluation.

Saturation is an operational scheduling signal. It does not adjudicate truth,
delete records, or apply review decisions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "topic-saturation.v1"
POLICY_SCHEMA_VERSION = "topic-saturation-policy.v1"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "topic_saturation_policy.v1.json"
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
        raise TopicSaturationError(f"could not read saturation policy: {policy_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TopicSaturationError("saturation policy must be a JSON object")
    validate_policy(payload)
    return Policy(payload)


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise TopicSaturationError(f"saturation policy schema_version must be {POLICY_SCHEMA_VERSION}")
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
            raise TopicSaturationError(f"saturation policy family_weights.{key} must be nonnegative")
    for field in ("accepted_review_states", "reviewable_review_states", "backlog_review_states"):
        values = policy.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
            raise TopicSaturationError(f"saturation policy {field} must be a non-empty string array")


def parse_note_text(note_text: Any) -> dict[str, Any]:
    if not isinstance(note_text, str) or not note_text.strip():
        return {}
    try:
        payload = json.loads(note_text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_recent_gather_events(conn: sqlite3.Connection, *, subject_id: str, limit: int) -> list[dict[str, Any]]:
    pattern = f'%\"subject_id\": \"{subject_id}\"%'
    rows = conn.execute(
        """
        SELECT provenance_event_id, provenance_event_key_v1, run_id, event_timestamp, note_text
        FROM provenance_event
        WHERE event_type='gather_candidate_batch_ingest'
          AND note_text LIKE ?
        ORDER BY event_timestamp DESC, provenance_event_id DESC
        LIMIT ?
        """,
        (pattern, limit),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        note = parse_note_text(row["note_text"])
        if note.get("subject_id") != subject_id:
            continue
        events.append(
            {
                "provenance_event_id": int(row["provenance_event_id"]),
                "event_key": str(row["provenance_event_key_v1"]),
                "run_id": None if row["run_id"] is None else str(row["run_id"]),
                "event_timestamp": str(row["event_timestamp"]),
                "facet": note.get("facet"),
                "cycle_depth": note.get("cycle_depth"),
            }
        )
    return events


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


def source_access_count_for_event(conn: sqlite3.Connection, event_key: str) -> int:
    ids: set[int] = set()
    for row in conn.execute(
        """
        SELECT access.source_access_id
        FROM source_access AS access
        INNER JOIN work ON work.work_id = access.work_id
        WHERE work.provenance_event_ref=?
        """,
        (event_key,),
    ).fetchall():
        ids.add(int(row["source_access_id"]))
    note_row = conn.execute(
        "SELECT note_text FROM provenance_event WHERE provenance_event_key_v1=?",
        (event_key,),
    ).fetchone()
    artifact_hash = parse_note_text(note_row["note_text"]).get("artifact_hash") if note_row is not None else None
    if isinstance(artifact_hash, str) and artifact_hash:
        for row in conn.execute(
            "SELECT source_access_id FROM source_access WHERE source_lead_id LIKE ?",
            (f"source-lead:{artifact_hash}:%",),
        ).fetchall():
            ids.add(int(row["source_access_id"]))
    return len(ids)


def cycle_yield(conn: sqlite3.Connection, *, event: dict[str, Any], policy: Policy) -> dict[str, Any]:
    event_key = str(event["event_key"])
    raw = policy.raw
    accepted_states = list(raw["accepted_review_states"])
    reviewable_states = list(raw["reviewable_review_states"])
    family_counts = {table: 0 for table in USEFUL_FAMILY_TABLES}
    family_counts.update(
        {
            "work": _count_by_provenance(conn, "work", event_key),
            "source_claim": _count_by_provenance(conn, "source_claim", event_key),
            "extraction_detected_entity": _count_by_provenance(conn, "extraction_detected_entity", event_key),
            "source_relationship": _count_by_provenance(conn, "source_relationship", event_key),
            "capture_event": _count_by_provenance(conn, "capture_event", event_key),
            "extraction_record": _count_by_provenance(conn, "extraction_record", event_key),
            "source_access": source_access_count_for_event(conn, event_key),
            "authority_reconciliation": 0,
        }
    )
    accepted_records = sum(
        _count_by_provenance_and_states(conn, table, event_key, accepted_states)
        for table in REVIEW_STATE_TABLES
        if table != "source_access"
    )
    reviewable_records = sum(
        _count_by_provenance_and_states(conn, table, event_key, reviewable_states)
        for table in REVIEW_STATE_TABLES
        if table != "source_access"
    )
    weights = raw["family_weights"]
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
    return {
        **event,
        "family_counts": family_counts,
        "new_accepted_records": accepted_records,
        "new_reviewable_records": reviewable_records,
        "useful_yield": round(float(useful_yield), 4),
        "low_yield": low_yield,
    }


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
    events = load_recent_gather_events(conn, subject_id=subject_id, limit=policy.lookback_cycles)
    cycles = [cycle_yield(conn, event=event, policy=policy) for event in events]
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
            subject_id=subject_id,
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
            subject_id=subject_id,
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
    summary["new_reviewable_records"] = sum(int(cycle["new_reviewable_records"]) for cycle in cycles)
    summary["useful_yield"] = round(sum(float(cycle["useful_yield"]) for cycle in cycles), 4)
    return summary


def next_eligible_cycle(cycles: list[dict[str, Any]], *, policy: Policy, state: str) -> int | None:
    if state != "cooldown":
        return None
    depths = [cycle.get("cycle_depth") for cycle in cycles if isinstance(cycle.get("cycle_depth"), int)]
    current = max(depths) if depths else len(cycles)
    return current + int(policy.raw["cooldown_cycles"]) + 1
