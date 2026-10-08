"""Blocked established-state changes remain auditable without changing the target."""

from __future__ import annotations

import json

from tools.source_db_tools import authority_reconciliation, canonical_reconciliation, canonical_store

STAMP = "2026-10-08T12:00:00Z"


def test_blocked_review_transitions_are_recorded_without_demoting_target(tmp_path) -> None:
    conn = canonical_store.connect_canonical_store(tmp_path / "canonical.sqlite")
    try:
        canonical_store.apply_migrations(conn, applied_by="pytest")
        authority_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="blocked-review-transition",
            created_at=STAMP,
        )
        conn.execute(
            "UPDATE authority_record SET review_state='accepted' WHERE authority_record_id=?",
            (authority_id,),
        )
        no_op = canonical_reconciliation.update_review_state(
            conn,
            target_namespace="authority_record",
            target_id=authority_id,
            new_state="accepted",
            changed_at=STAMP,
            reason="replay",
            note="already accepted",
            source_namespace="pytest",
            source_id="attempt",
        )
        attempts = []
        for state in ("needs_review", "rejected", "needs_review"):
            attempts.append(
                canonical_reconciliation.update_review_state(
                    conn,
                    target_namespace="authority_record",
                    target_id=authority_id,
                    new_state=state,
                    changed_at=STAMP,
                    reason="automated contradiction",
                    note=f"requested {state}",
                    source_namespace="pytest",
                    source_id="attempt",
                )
            )
        target_state = conn.execute(
            "SELECT review_state FROM authority_record WHERE authority_record_id=?",
            (authority_id,),
        ).fetchone()[0]
        history = conn.execute(
            """SELECT previous_state, new_state, reason, note
            FROM review_state_history WHERE target_namespace='authority_record'
            AND target_id=? ORDER BY rowid""",
            (str(authority_id),),
        ).fetchall()
    finally:
        conn.close()

    assert no_op is False
    assert attempts == [False, False, False]
    assert target_state == "accepted"
    assert len(history) == 2
    assert [(row["previous_state"], row["new_state"], row["reason"]) for row in history] == [
        ("accepted", "accepted", "blocked_established_state"),
        ("accepted", "accepted", "blocked_established_state"),
    ]
    assert [json.loads(row["note"])["requested_state"] for row in history] == [
        "needs_review",
        "rejected",
    ]
