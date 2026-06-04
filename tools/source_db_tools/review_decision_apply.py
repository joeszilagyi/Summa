"""Apply explicit review decisions to canonical graph records.

This module is the mutation counterpart to the read-only review queue. It does
not detect new curation problems; it applies reviewer decisions to existing
review targets while preserving provenance, review history, and source rows.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from tools.source_db_tools import canonical_reconciliation, canonical_store


APPLY_TOOL = "tools/scripts/apply_review_decision.py"
RESULT_SCHEMA_VERSION = "review-decision-apply-result.v1"
SUPPORTED_ACTIONS = {
    "accept_merge",
    "reject_merge",
    "reject_claim",
    "mark_contradicted",
    "reject_relationship",
    "resolve_contradiction",
}
MERGE_REASON = "review_decision_accept_merge"


class ReviewDecisionApplyError(RuntimeError):
    """Raised when a review decision cannot be applied safely."""


@dataclass(frozen=True)
class ReviewTargetRef:
    target_type: str
    target_id: int


def parse_review_target(value: str) -> ReviewTargetRef:
    if ":" not in value:
        raise ReviewDecisionApplyError("target must be '<target_type>:<numeric_id>'")
    raw_type, raw_id = value.rsplit(":", 1)
    aliases = {
        "authority_reconciliation": "authority_reconciliation",
        "authrec": "authority_reconciliation",
        "claim": "source_claim",
        "source_claim": "source_claim",
        "relationship": "source_relationship",
        "source_relationship": "source_relationship",
    }
    target_type = aliases.get(raw_type.strip().lower().replace("-", "_"))
    if target_type is None:
        raise ReviewDecisionApplyError(f"unsupported review decision target type: {raw_type}")
    try:
        target_id = int(raw_id)
    except ValueError as exc:
        raise ReviewDecisionApplyError(f"target id must be numeric: {value}") from exc
    return ReviewTargetRef(target_type, target_id)


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], missing: str) -> sqlite3.Row:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise ReviewDecisionApplyError(missing)
    return row


def _fetch_authority(conn: sqlite3.Connection, authority_record_id: int) -> sqlite3.Row:
    return _fetch_one(
        conn,
        "SELECT * FROM authority_record WHERE authority_record_id=?",
        (authority_record_id,),
        f"unknown authority record id: {authority_record_id}",
    )


def _fetch_reconciliation(conn: sqlite3.Connection, reconciliation_id: int) -> sqlite3.Row:
    return _fetch_one(
        conn,
        "SELECT * FROM authority_reconciliation WHERE authority_reconciliation_id=?",
        (reconciliation_id,),
        f"unknown authority reconciliation id: {reconciliation_id}",
    )


def _fetch_claim(conn: sqlite3.Connection, source_claim_id: int) -> sqlite3.Row:
    return _fetch_one(
        conn,
        "SELECT * FROM source_claim WHERE source_claim_id=?",
        (source_claim_id,),
        f"unknown source claim id: {source_claim_id}",
    )


def _fetch_relationship(conn: sqlite3.Connection, source_relationship_id: int) -> sqlite3.Row:
    return _fetch_one(
        conn,
        "SELECT * FROM source_relationship WHERE source_relationship_id=?",
        (source_relationship_id,),
        f"unknown source relationship id: {source_relationship_id}",
    )


def _current_state(row: sqlite3.Row) -> str | None:
    value = row["review_state"] if "review_state" in row.keys() else None
    return None if value is None else str(value)


def _check_expected_state(row: sqlite3.Row, expected_state: str | None) -> None:
    if expected_state is None:
        return
    current = _current_state(row)
    if current != expected_state:
        raise ReviewDecisionApplyError(
            f"expected current review_state {expected_state!r}, found {current!r}"
        )


def record_review_decision_provenance(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: int,
    action: str,
    reviewer: str,
    reason: str,
    decided_at: str,
    run_id: str | None = None,
) -> canonical_store.ProvenanceEventRef:
    key = canonical_store.stable_write_key(
        "review-decision",
        target_type,
        target_id,
        action,
        reviewer,
        reason,
        decided_at,
    )
    return canonical_store.record_provenance_event(
        conn,
        object_namespace=target_type,
        object_id=str(target_id),
        event_type=f"review_decision_{action}",
        actor_type="human",
        actor_id=reviewer,
        tool_name=APPLY_TOOL,
        run_id=run_id,
        source_object_namespace=target_type,
        source_object_id=str(target_id),
        event_timestamp=decided_at,
        note_text=reason,
        provenance_event_key_v1=key,
    )


def _review_state_update(
    conn: sqlite3.Connection,
    *,
    target_namespace: str,
    target_id: int,
    new_state: str,
    changed_at: str,
    reason: str,
    note: str,
    source_namespace: str,
    source_id: str,
    source_run_id: str | None,
) -> bool:
    return canonical_reconciliation.update_review_state(
        conn,
        target_namespace=target_namespace,
        target_id=target_id,
        new_state=new_state,
        changed_at=changed_at,
        reason=reason,
        note=note,
        source_namespace=source_namespace,
        source_id=source_id,
        source_run_id=source_run_id,
    )


def _candidate_winner(row: sqlite3.Row) -> int:
    winner = row["candidate_authority_record_id"] or row["candidate_authority_id"] or row["accepted_authority_id"]
    if winner is None:
        raise ReviewDecisionApplyError("authority reconciliation target has no candidate authority to accept")
    return int(winner)


def _candidate_loser(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    target_namespace = str(row["target_namespace"])
    if target_namespace == "authority_record":
        return int(row["target_id"])
    if target_namespace == "extraction_detected_entity" and row["detected_entity_id"] is not None:
        entity = _fetch_one(
            conn,
            "SELECT authority_record_id FROM extraction_detected_entity WHERE detected_entity_id=?",
            (int(row["detected_entity_id"]),),
            f"unknown detected entity id: {row['detected_entity_id']}",
        )
        if entity["authority_record_id"] is not None:
            return int(entity["authority_record_id"])
    raise ReviewDecisionApplyError(
        "accept_merge requires a reconciliation target that identifies a losing authority record"
    )


def _merge_already_applied(conn: sqlite3.Connection, *, loser_id: int, winner_id: int, reconciliation_id: int) -> bool:
    loser = _fetch_authority(conn, loser_id)
    rec = _fetch_reconciliation(conn, reconciliation_id)
    merge = conn.execute(
        """
        SELECT authority_merge_event_id
        FROM authority_merge_event
        WHERE from_authority_record_id=? AND into_authority_record_id=? AND merge_reason=?
        """,
        (loser_id, winner_id, MERGE_REASON),
    ).fetchone()
    return (
        loser["merged_into_authority_record_id"] is not None
        and int(loser["merged_into_authority_record_id"]) == winner_id
        and rec["accepted_authority_id"] is not None
        and int(rec["accepted_authority_id"]) == winner_id
        and str(rec["review_state"]) == "accepted"
        and merge is not None
    )


def validate_review_decision_target(
    conn: sqlite3.Connection,
    *,
    target: ReviewTargetRef,
    action: str,
    expected_state: str | None = None,
) -> sqlite3.Row:
    if action not in SUPPORTED_ACTIONS:
        raise ReviewDecisionApplyError(f"unsupported review decision action: {action}")
    if action in {"accept_merge", "reject_merge"}:
        if target.target_type != "authority_reconciliation":
            raise ReviewDecisionApplyError(f"{action} requires an authority_reconciliation target")
        row = _fetch_reconciliation(conn, target.target_id)
    elif action == "reject_claim":
        if target.target_type != "source_claim":
            raise ReviewDecisionApplyError("reject_claim requires a source_claim target")
        row = _fetch_claim(conn, target.target_id)
    elif action == "resolve_contradiction":
        if target.target_type != "source_relationship":
            raise ReviewDecisionApplyError("resolve_contradiction requires a source_relationship target")
        row = _fetch_relationship(conn, target.target_id)
        if str(row["predicate"]) != canonical_reconciliation.CONTRADICTION_PREDICATE:
            raise ReviewDecisionApplyError("resolve_contradiction requires a contradiction relationship")
    elif action == "mark_contradicted":
        if target.target_type == "source_claim":
            row = _fetch_claim(conn, target.target_id)
        elif target.target_type == "source_relationship":
            row = _fetch_relationship(conn, target.target_id)
        else:
            raise ReviewDecisionApplyError("mark_contradicted requires a source_claim or source_relationship target")
    elif action == "reject_relationship":
        if target.target_type != "source_relationship":
            raise ReviewDecisionApplyError("reject_relationship requires a source_relationship target")
        row = _fetch_relationship(conn, target.target_id)
    else:  # pragma: no cover - guarded by action set
        raise ReviewDecisionApplyError(f"unsupported review decision action: {action}")
    _check_expected_state(row, expected_state)
    return row


def repoint_authority_references(
    conn: sqlite3.Connection,
    *,
    losing_authority_id: int,
    winning_authority_id: int,
    changed_at: str,
    dry_run: bool,
) -> dict[str, int]:
    updates = {
        "extraction_detected_entity.authority_record_id": conn.execute(
            "SELECT COUNT(*) FROM extraction_detected_entity WHERE authority_record_id=?",
            (losing_authority_id,),
        ).fetchone()[0],
        "work_subject.authority_record_id": conn.execute(
            "SELECT COUNT(*) FROM work_subject WHERE authority_record_id=?",
            (losing_authority_id,),
        ).fetchone()[0],
    }
    if dry_run:
        return {key: int(value) for key, value in updates.items()}
    conn.execute(
        """
        UPDATE extraction_detected_entity
        SET authority_record_id=?, record_last_updated=?
        WHERE authority_record_id=?
        """,
        (winning_authority_id, changed_at, losing_authority_id),
    )
    conn.execute(
        """
        UPDATE work_subject
        SET authority_record_id=?, record_last_updated=?
        WHERE authority_record_id=?
        """,
        (winning_authority_id, changed_at, losing_authority_id),
    )
    return {key: int(value) for key, value in updates.items()}


def _base_result(
    *,
    target: ReviewTargetRef,
    action: str,
    reviewer: str,
    reason: str,
    dry_run: bool,
    decided_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "target_type": target.target_type,
        "target_id": target.target_id,
        "target": f"{target.target_type}:{target.target_id}",
        "decision_action": action,
        "reviewer": reviewer,
        "reason": reason,
        "dry_run": dry_run,
        "decided_at": decided_at,
        "status": "planned" if dry_run else "completed",
        "provenance_event_id": None,
        "review_state_history_ids": [],
        "merge_event_id": None,
        "winner_authority_id": None,
        "loser_authority_id": None,
        "references_repointed": {},
        "rows_demoted": {},
        "rows_preserved": [],
        "skipped_updates": [],
        "warnings": [],
        "errors": [],
    }


def apply_authority_merge_decision(
    conn: sqlite3.Connection,
    *,
    target: ReviewTargetRef,
    row: sqlite3.Row,
    reviewer: str,
    reason: str,
    decided_at: str,
    dry_run: bool,
    run_id: str | None,
) -> dict[str, Any]:
    result = _base_result(
        target=target,
        action="accept_merge",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
        decided_at=decided_at,
    )
    winner_id = _candidate_winner(row)
    loser_id = _candidate_loser(conn, row)
    if winner_id == loser_id:
        raise ReviewDecisionApplyError("cannot merge an authority record into itself")
    winner = _fetch_authority(conn, winner_id)
    loser = _fetch_authority(conn, loser_id)
    if str(winner["authority_type"]) != str(loser["authority_type"]):
        raise ReviewDecisionApplyError(
            f"authority types are incompatible: loser={loser['authority_type']} winner={winner['authority_type']}"
        )
    current_merge = loser["merged_into_authority_record_id"]
    if current_merge is not None and int(current_merge) != winner_id:
        raise ReviewDecisionApplyError(f"losing authority is already merged into {current_merge}")
    result["winner_authority_id"] = winner_id
    result["loser_authority_id"] = loser_id
    result["rows_preserved"] = [f"authority_record:{loser_id}", f"authority_record:{winner_id}"]
    result["references_repointed"] = repoint_authority_references(
        conn,
        losing_authority_id=loser_id,
        winning_authority_id=winner_id,
        changed_at=decided_at,
        dry_run=True,
    )
    if dry_run:
        result["intended_review_states"] = {
            f"authority_reconciliation:{target.target_id}": "accepted",
            f"authority_record:{loser_id}": "demoted",
        }
        return result
    if _merge_already_applied(conn, loser_id=loser_id, winner_id=winner_id, reconciliation_id=target.target_id):
        result["status"] = "already_applied"
        result["references_repointed"] = {key: 0 for key in result["references_repointed"]}
        return result
    provenance = record_review_decision_provenance(
        conn,
        target_type=target.target_type,
        target_id=target.target_id,
        action="accept_merge",
        reviewer=reviewer,
        reason=reason,
        decided_at=decided_at,
        run_id=run_id,
    )
    merge = canonical_reconciliation.record_authority_merge_event(
        conn,
        from_authority_record_id=loser_id,
        into_authority_record_id=winner_id,
        merge_reason=MERGE_REASON,
        evidence_note=reason,
        merged_by=reviewer,
        merged_at=decided_at,
    )
    repointed = repoint_authority_references(
        conn,
        losing_authority_id=loser_id,
        winning_authority_id=winner_id,
        changed_at=decided_at,
        dry_run=False,
    )
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
        (winner_id, reason, decided_at, decided_at, decided_at, target.target_id),
    )
    canonical_store.record_review_state_history(
        conn,
        target_namespace="authority_reconciliation",
        target_id=target.target_id,
        previous_state=None if row["review_state"] is None else str(row["review_state"]),
        new_state="accepted",
        changed_by=reviewer,
        changed_at=decided_at,
        reason="accept_merge",
        note=reason,
        source_namespace="provenance_event",
        source_id=provenance.event_key,
        source_tool=APPLY_TOOL,
        source_run_id=run_id,
    )
    demoted = _review_state_update(
        conn,
        target_namespace="authority_record",
        target_id=loser_id,
        new_state="demoted",
        changed_at=decided_at,
        reason="accept_merge",
        note=reason,
        source_namespace="authority_merge_event",
        source_id=str(merge.row_id),
        source_run_id=run_id,
    )
    result.update(
        {
            "provenance_event_id": provenance.event_id,
            "merge_event_id": merge.row_id,
            "references_repointed": repointed,
            "rows_demoted": {"authority_record": 1 if demoted else 0},
        }
    )
    return result


def apply_authority_reconciliation_rejection(
    conn: sqlite3.Connection,
    *,
    target: ReviewTargetRef,
    row: sqlite3.Row,
    reviewer: str,
    reason: str,
    decided_at: str,
    dry_run: bool,
    run_id: str | None,
) -> dict[str, Any]:
    result = _base_result(
        target=target,
        action="reject_merge",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
        decided_at=decided_at,
    )
    candidate = row["candidate_authority_record_id"] or row["candidate_authority_id"]
    result["rows_preserved"] = [f"authority_reconciliation:{target.target_id}"]
    if candidate is not None:
        result["rows_preserved"].append(f"authority_record:{int(candidate)}")
    if dry_run:
        result["intended_review_states"] = {f"authority_reconciliation:{target.target_id}": "rejected"}
        return result
    if str(row["review_state"]) == "rejected":
        result["status"] = "already_applied"
        return result
    provenance = record_review_decision_provenance(
        conn,
        target_type=target.target_type,
        target_id=target.target_id,
        action="reject_merge",
        reviewer=reviewer,
        reason=reason,
        decided_at=decided_at,
        run_id=run_id,
    )
    rejected_ids: list[int] = []
    try:
        parsed = json.loads(row["rejected_candidate_ids_json"] or "[]")
        if isinstance(parsed, list):
            rejected_ids = [int(item) for item in parsed if isinstance(item, int)]
    except (TypeError, ValueError, json.JSONDecodeError):
        rejected_ids = []
    if candidate is not None and int(candidate) not in rejected_ids:
        rejected_ids.append(int(candidate))
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
        (reason, json.dumps(rejected_ids, sort_keys=True), decided_at, decided_at, decided_at, target.target_id),
    )
    canonical_store.record_review_state_history(
        conn,
        target_namespace="authority_reconciliation",
        target_id=target.target_id,
        previous_state=None if row["review_state"] is None else str(row["review_state"]),
        new_state="rejected",
        changed_by=reviewer,
        changed_at=decided_at,
        reason="reject_merge",
        note=reason,
        source_namespace="provenance_event",
        source_id=provenance.event_key,
        source_tool=APPLY_TOOL,
        source_run_id=run_id,
    )
    result["provenance_event_id"] = provenance.event_id
    return result


def apply_source_claim_rejection(
    conn: sqlite3.Connection,
    *,
    target: ReviewTargetRef,
    row: sqlite3.Row,
    reviewer: str,
    reason: str,
    decided_at: str,
    dry_run: bool,
    run_id: str | None,
) -> dict[str, Any]:
    result = _base_result(
        target=target,
        action="reject_claim",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
        decided_at=decided_at,
    )
    result["rows_preserved"] = [f"source_claim:{target.target_id}"]
    if dry_run:
        result["intended_review_states"] = {f"source_claim:{target.target_id}": "rejected"}
        return result
    if str(row["review_state"]) == "rejected":
        result["status"] = "already_applied"
        return result
    provenance = record_review_decision_provenance(
        conn,
        target_type=target.target_type,
        target_id=target.target_id,
        action="reject_claim",
        reviewer=reviewer,
        reason=reason,
        decided_at=decided_at,
        run_id=run_id,
    )
    changed = _review_state_update(
        conn,
        target_namespace="source_claim",
        target_id=target.target_id,
        new_state="rejected",
        changed_at=decided_at,
        reason="reject_claim",
        note=reason,
        source_namespace="provenance_event",
        source_id=provenance.event_key,
        source_run_id=run_id,
    )
    result["provenance_event_id"] = provenance.event_id
    if not changed:
        result["status"] = "already_applied"
    return result


def apply_relationship_rejection(
    conn: sqlite3.Connection,
    *,
    target: ReviewTargetRef,
    row: sqlite3.Row,
    reviewer: str,
    reason: str,
    decided_at: str,
    dry_run: bool,
    run_id: str | None,
    action: str = "mark_contradicted",
) -> dict[str, Any]:
    new_state = "rejected" if action == "reject_relationship" else "needs_review"
    result = _base_result(
        target=target,
        action=action,
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
        decided_at=decided_at,
    )
    result["rows_preserved"] = [f"{target.target_type}:{target.target_id}"]
    if dry_run:
        result["intended_review_states"] = {f"{target.target_type}:{target.target_id}": new_state}
        return result
    if str(row["review_state"]) == new_state:
        result["status"] = "already_applied"
        return result
    provenance = record_review_decision_provenance(
        conn,
        target_type=target.target_type,
        target_id=target.target_id,
        action=action,
        reviewer=reviewer,
        reason=reason,
        decided_at=decided_at,
        run_id=run_id,
    )
    namespace = target.target_type
    changed = _review_state_update(
        conn,
        target_namespace=namespace,
        target_id=target.target_id,
        new_state=new_state,
        changed_at=decided_at,
        reason=action,
        note=reason,
        source_namespace="provenance_event",
        source_id=provenance.event_key,
        source_run_id=run_id,
    )
    result["provenance_event_id"] = provenance.event_id
    if not changed:
        result["status"] = "already_applied"
    return result


def apply_contradiction_resolution(
    conn: sqlite3.Connection,
    *,
    target: ReviewTargetRef,
    row: sqlite3.Row,
    reviewer: str,
    reason: str,
    decided_at: str,
    dry_run: bool,
    run_id: str | None,
) -> dict[str, Any]:
    result = _base_result(
        target=target,
        action="resolve_contradiction",
        reviewer=reviewer,
        reason=reason,
        dry_run=dry_run,
        decided_at=decided_at,
    )
    result["rows_preserved"] = [
        f"source_relationship:{target.target_id}",
        str(row["from_object_ref"]),
        str(row["to_object_ref"]),
    ]
    if dry_run:
        result["intended_review_states"] = {f"source_relationship:{target.target_id}": "reviewed"}
        return result
    if str(row["review_state"]) == "reviewed":
        result["status"] = "already_applied"
        return result
    provenance = record_review_decision_provenance(
        conn,
        target_type=target.target_type,
        target_id=target.target_id,
        action="resolve_contradiction",
        reviewer=reviewer,
        reason=reason,
        decided_at=decided_at,
        run_id=run_id,
    )
    changed = _review_state_update(
        conn,
        target_namespace="source_relationship",
        target_id=target.target_id,
        new_state="reviewed",
        changed_at=decided_at,
        reason="resolve_contradiction",
        note=reason,
        source_namespace="provenance_event",
        source_id=provenance.event_key,
        source_run_id=run_id,
    )
    result["provenance_event_id"] = provenance.event_id
    if not changed:
        result["status"] = "already_applied"
    return result


def apply_review_decision(
    conn: sqlite3.Connection,
    *,
    target: str,
    decision_action: str,
    reviewer: str,
    reason: str,
    expected_state: str | None = None,
    dry_run: bool = False,
    decided_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not reviewer or not reviewer.strip():
        raise ReviewDecisionApplyError("reviewer is required")
    if not reason or not reason.strip():
        raise ReviewDecisionApplyError("reason is required")
    action = decision_action.strip().lower().replace("-", "_")
    target_ref = parse_review_target(target)
    timestamp = canonical_store._normalize_timestamp(  # type: ignore[attr-defined]
        decided_at,
        field_name="decided_at",
        default=canonical_store.now_rfc3339(),
    )
    row = validate_review_decision_target(
        conn,
        target=target_ref,
        action=action,
        expected_state=expected_state,
    )
    if dry_run:
        return _apply_review_decision_inner(
            conn,
            target_ref=target_ref,
            row=row,
            action=action,
            reviewer=reviewer,
            reason=reason,
            decided_at=timestamp,
            dry_run=True,
            run_id=run_id,
        )
    with conn:
        row = validate_review_decision_target(
            conn,
            target=target_ref,
            action=action,
            expected_state=expected_state,
        )
        return _apply_review_decision_inner(
            conn,
            target_ref=target_ref,
            row=row,
            action=action,
            reviewer=reviewer,
            reason=reason,
            decided_at=timestamp,
            dry_run=False,
            run_id=run_id,
        )


def _apply_review_decision_inner(
    conn: sqlite3.Connection,
    *,
    target_ref: ReviewTargetRef,
    row: sqlite3.Row,
    action: str,
    reviewer: str,
    reason: str,
    decided_at: str,
    dry_run: bool,
    run_id: str | None,
) -> dict[str, Any]:
    if action == "accept_merge":
        return apply_authority_merge_decision(
            conn,
            target=target_ref,
            row=row,
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            dry_run=dry_run,
            run_id=run_id,
        )
    if action == "reject_merge":
        return apply_authority_reconciliation_rejection(
            conn,
            target=target_ref,
            row=row,
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            dry_run=dry_run,
            run_id=run_id,
        )
    if action == "reject_claim":
        return apply_source_claim_rejection(
            conn,
            target=target_ref,
            row=row,
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            dry_run=dry_run,
            run_id=run_id,
        )
    if action in {"mark_contradicted", "reject_relationship"}:
        return apply_relationship_rejection(
            conn,
            target=target_ref,
            row=row,
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            dry_run=dry_run,
            run_id=run_id,
            action=action,
        )
    if action == "resolve_contradiction":
        return apply_contradiction_resolution(
            conn,
            target=target_ref,
            row=row,
            reviewer=reviewer,
            reason=reason,
            decided_at=decided_at,
            dry_run=dry_run,
            run_id=run_id,
        )
    raise ReviewDecisionApplyError(f"unsupported review decision action: {action}")
