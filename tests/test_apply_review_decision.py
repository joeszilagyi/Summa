from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.source_db_tools import (
    authority_reconciliation,
    canonical_store,
    review_decision_apply,
)
from tools.scripts import apply_review_decision as apply_review_script


REPO_ROOT = Path(__file__).resolve().parents[1]
APPLY_SCRIPT = REPO_ROOT / "tools" / "scripts" / "apply_review_decision.py"
APPLY_WRAPPER = REPO_ROOT / "tools" / "scripts" / "Index_Apply_Review_Decision.sh"
FIXED_TIMESTAMP = "2026-06-04T10:11:12Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.apply_review_decision",
    )
    return db_path


def connect(db_path: Path):
    return canonical_store.connect_canonical_store(db_path)


def provenance(conn, suffix: str = "fixture") -> canonical_store.ProvenanceEventRef:
    return canonical_store.record_provenance_event(
        conn,
        object_namespace="review_fixture",
        object_id=suffix,
        event_type="review_fixture",
        actor_type="tool",
        actor_id="pytest",
        tool_name="tests/test_apply_review_decision.py",
        run_id=f"run-{suffix}",
        event_timestamp=FIXED_TIMESTAMP,
        note_text=f"fixture provenance {suffix}",
        provenance_event_key_v1=f"prov:apply-review:{suffix}",
    )


def create_authority(conn, label: str, *, authority_type: str = "person") -> int:
    return authority_reconciliation.create_local_authority(
        conn,
        authority_type=authority_type,
        preferred_label=label,
        source_namespace="pytest",
        source_id=label,
        review_state="needs_review",
        confidence_score=0.8,
        created_at=FIXED_TIMESTAMP,
    )


def insert_reconciliation(
    conn,
    *,
    loser_id: int,
    winner_id: int,
    state: str = "needs_review",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO authority_reconciliation (
          reconciliation_key_v1,
          target_namespace,
          target_id,
          raw_label,
          entity_type,
          candidate_label,
          candidate_authority_record_id,
          method,
          match_method,
          match_score,
          confidence_score,
          review_state,
          created_at,
          updated_at,
          record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"reconcile:pytest:{loser_id}:{winner_id}:{state}",
            "authority_record",
            str(loser_id),
            "Jane Smith",
            "person",
            "Jane Smith",
            winner_id,
            "review_fixture",
            "review_fixture",
            0.95,
            0.95,
            state,
            FIXED_TIMESTAMP,
            FIXED_TIMESTAMP,
            FIXED_TIMESTAMP,
        ),
    )
    return int(cursor.lastrowid)


def create_work_subject_for_authority(conn, *, authority_id: int, suffix: str = "merge") -> int:
    prov = provenance(conn, f"work-{suffix}")
    work = canonical_store.upsert_work(
        conn,
        work_key_v1=f"work:apply-review:{suffix}",
        provenance_event_ref=prov.event_key,
        work_type="article",
        title=f"Apply Review Work {suffix}",
        review_state="needs_review",
        first_seen_at=FIXED_TIMESTAMP,
        last_seen_at=FIXED_TIMESTAMP,
        created_at=FIXED_TIMESTAMP,
        record_last_updated=FIXED_TIMESTAMP,
    )
    cursor = conn.execute(
        """
        INSERT INTO work_subject (
          work_id,
          authority_record_id,
          subject_role,
          source_note,
          review_state,
          confidence_score,
          provenance_event_ref,
          created_at,
          record_last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            work.row_id,
            authority_id,
            "about",
            "fixture subject",
            "needs_review",
            0.7,
            prov.event_key,
            FIXED_TIMESTAMP,
            FIXED_TIMESTAMP,
        ),
    )
    return int(cursor.lastrowid)


def create_detected_entity_for_authority(conn, *, authority_id: int, suffix: str = "merge") -> int:
    prov = provenance(conn, f"entity-{suffix}")
    entity = canonical_store.record_extraction_detected_entity(
        conn,
        provenance_event_ref=prov.event_key,
        entity_label=f"Jane Smith {suffix}",
        normalized_label=f"jane smith {suffix}",
        entity_type="person",
        authority_record_id=authority_id,
        review_state="needs_review",
        confidence_score=0.7,
        record_last_updated=FIXED_TIMESTAMP,
    )
    return entity.row_id


def count_rows(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def review_history_count(conn, *, namespace: str, target_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM review_state_history
            WHERE target_namespace=? AND target_id=?
            """,
            (namespace, str(target_id)),
        ).fetchone()[0]
    )


def test_cli_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(APPLY_SCRIPT), "--help"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert "Apply an explicit review decision" in result.stdout


def test_shell_wrapper_help_exits_zero() -> None:
    env = {**os.environ, "PYTHON": sys.executable}
    result = subprocess.run(
        [str(APPLY_WRAPPER), "--help"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Apply an explicit review decision" in result.stdout


def test_cli_reject_claim_applies_decision_and_outputs_json(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            prov = provenance(conn, "cli-claim")
            claim = canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:apply-review:cli",
                about_object_ref="authority:cli",
                claim_text="CLI claim to reject.",
                claim_type="fixture",
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
    finally:
        conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(APPLY_SCRIPT),
            "--db",
            str(db_path),
            "--target",
            f"source_claim:{claim.row_id}",
            "--decision",
            "reject_claim",
            "--reviewer",
            "operator",
            "--reason",
            "CLI reviewed rejection.",
            "--decided-at",
            FIXED_TIMESTAMP,
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    conn = connect(db_path)
    try:
        claim_row = conn.execute(
            "SELECT review_state FROM source_claim WHERE source_claim_id=?",
            (claim.row_id,),
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["schema_version"] == "review-decision-apply-result.v1"
    assert payload["status"] == "completed"
    assert payload["target"] == f"source_claim:{claim.row_id}"
    assert claim_row["review_state"] == "rejected"


def test_run_spools_on_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = bootstrap_db(tmp_path)
    spool_dir = tmp_path / "spool"

    def fail_apply(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise review_decision_apply.ReviewDecisionApplyError("synthetic write failure")

    monkeypatch.setattr(
        apply_review_script.review_decision_apply, "apply_review_decision", fail_apply
    )

    result = apply_review_script.run(
        [
            "--db",
            str(db_path),
            "--target",
            "source_claim:1",
            "--decision",
            "reject_claim",
            "--reviewer",
            "operator",
            "--reason",
            "exercise degraded spool",
            "--degraded-spool",
            "--spool-dir",
            str(spool_dir),
        ]
    )

    assert result["status"] == "spooled"
    assert spool_dir.exists()
    assert Path(result["spool_record_path"]).is_file()


def test_accept_authority_merge_repoints_safe_references_and_preserves_rows(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            winner_id = create_authority(conn, "Jane Smith Winner")
            loser_id = create_authority(conn, "Jane Smith Loser")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
            entity_id = create_detected_entity_for_authority(conn, authority_id=loser_id)
            work_subject_id = create_work_subject_for_authority(conn, authority_id=loser_id)

        result = review_decision_apply.apply_review_decision(
            conn,
            target=f"authority_reconciliation:{reconciliation_id}",
            decision_action="accept_merge",
            reviewer="operator",
            reason="Reviewed same controlled identity.",
            decided_at=FIXED_TIMESTAMP,
        )

        loser = conn.execute(
            "SELECT merged_into_authority_record_id, review_state FROM authority_record WHERE authority_record_id=?",
            (loser_id,),
        ).fetchone()
        rec = conn.execute(
            "SELECT review_state, accepted_authority_id FROM authority_reconciliation WHERE authority_reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        entity = conn.execute(
            "SELECT authority_record_id FROM extraction_detected_entity WHERE detected_entity_id=?",
            (entity_id,),
        ).fetchone()
        work_subject = conn.execute(
            "SELECT authority_record_id FROM work_subject WHERE work_subject_id=?",
            (work_subject_id,),
        ).fetchone()
        merge_count = count_rows(conn, "authority_merge_event")
        claim_count = count_rows(conn, "source_claim")
    finally:
        conn.close()

    assert result["status"] == "completed"
    assert result["merge_event_id"] is not None
    assert result["winner_authority_id"] == winner_id
    assert result["loser_authority_id"] == loser_id
    assert result["references_repointed"] == {
        "extraction_detected_entity.authority_record_id": 1,
        "work_subject.authority_record_id": 1,
    }
    assert loser["merged_into_authority_record_id"] == winner_id
    assert loser["review_state"] == "demoted"
    assert rec["review_state"] == "accepted"
    assert rec["accepted_authority_id"] == winner_id
    assert entity["authority_record_id"] == winner_id
    assert work_subject["authority_record_id"] == winner_id
    assert merge_count == 1
    assert claim_count == 0


def test_reject_authority_merge_records_review_without_repointing(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            winner_id = create_authority(conn, "Rejected Merge Winner")
            loser_id = create_authority(conn, "Rejected Merge Loser")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
            entity_id = create_detected_entity_for_authority(conn, authority_id=loser_id, suffix="reject")

        result = review_decision_apply.apply_review_decision(
            conn,
            target=f"authority_reconciliation:{reconciliation_id}",
            decision_action="reject_merge",
            reviewer="operator",
            reason="Different people after review.",
            decided_at=FIXED_TIMESTAMP,
        )
        rec = conn.execute(
            "SELECT review_state, rejected_candidate_ids_json FROM authority_reconciliation WHERE authority_reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        entity = conn.execute(
            "SELECT authority_record_id FROM extraction_detected_entity WHERE detected_entity_id=?",
            (entity_id,),
        ).fetchone()
    finally:
        conn.close()

    assert result["status"] == "completed"
    assert result["merge_event_id"] is None
    assert rec["review_state"] == "rejected"
    assert winner_id in json.loads(rec["rejected_candidate_ids_json"])
    assert entity["authority_record_id"] == loser_id


def test_reject_contradicted_claim_preserves_claim_and_records_audit(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            prov = provenance(conn, "claim-reject")
            claim = canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:apply-review:reject",
                about_object_ref="authority:person-a",
                claim_text="Person A impossible claim.",
                claim_type="structured_fixture",
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
        before_claims = count_rows(conn, "source_claim")
        result = review_decision_apply.apply_review_decision(
            conn,
            target=f"source_claim:{claim.row_id}",
            decision_action="reject_claim",
            reviewer="operator",
            reason="Chronologically impossible.",
            decided_at=FIXED_TIMESTAMP,
        )
        after_claims = count_rows(conn, "source_claim")
        claim_row = conn.execute(
            "SELECT review_state FROM source_claim WHERE source_claim_id=?",
            (claim.row_id,),
        ).fetchone()
        history_count = review_history_count(conn, namespace="source_claim", target_id=claim.row_id)
    finally:
        conn.close()

    assert result["status"] == "completed"
    assert before_claims == after_claims == 1
    assert claim_row["review_state"] == "rejected"
    assert history_count == 1


def test_resolve_contradiction_preserves_relationship_and_underlying_claims(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            prov = provenance(conn, "contradiction")
            left = canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:apply-review:left",
                about_object_ref="authority:person-a",
                claim_text="Left claim.",
                claim_type="fixture",
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            right = canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:apply-review:right",
                about_object_ref="authority:person-a",
                claim_text="Right claim.",
                claim_type="fixture",
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            contradiction = canonical_store.record_source_relationship(
                conn,
                provenance_event_ref=prov.event_key,
                from_object_ref=f"source_claim:{left.row_id}",
                to_object_ref=f"source_claim:{right.row_id}",
                predicate="contradicts",
                evidence_note="fixture contradiction",
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
        result = review_decision_apply.apply_review_decision(
            conn,
            target=f"source_relationship:{contradiction.row_id}",
            decision_action="resolve_contradiction",
            reviewer="operator",
            reason="Keep both claims and mark contradiction reviewed.",
            decided_at=FIXED_TIMESTAMP,
        )
        relationship = conn.execute(
            "SELECT predicate, review_state FROM source_relationship WHERE source_relationship_id=?",
            (contradiction.row_id,),
        ).fetchone()
        claim_count = count_rows(conn, "source_claim")
    finally:
        conn.close()

    assert result["status"] == "completed"
    assert relationship["predicate"] == "contradicts"
    assert relationship["review_state"] == "reviewed"
    assert claim_count == 2


def test_dry_run_reports_intended_merge_without_mutating(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            winner_id = create_authority(conn, "Dry Winner")
            loser_id = create_authority(conn, "Dry Loser")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
            create_detected_entity_for_authority(conn, authority_id=loser_id, suffix="dry")
        result = review_decision_apply.apply_review_decision(
            conn,
            target=f"authority_reconciliation:{reconciliation_id}",
            decision_action="accept_merge",
            reviewer="operator",
            reason="Dry run only.",
            dry_run=True,
            decided_at=FIXED_TIMESTAMP,
        )
        loser = conn.execute(
            "SELECT merged_into_authority_record_id, review_state FROM authority_record WHERE authority_record_id=?",
            (loser_id,),
        ).fetchone()
    finally:
        conn.close()

    assert result["dry_run"] is True
    assert result["status"] == "planned"
    assert result["references_repointed"]["extraction_detected_entity.authority_record_id"] == 1
    assert loser["merged_into_authority_record_id"] is None
    assert loser["review_state"] == "needs_review"


def test_accept_merge_is_idempotent_for_repeated_apply(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            winner_id = create_authority(conn, "Idempotent Winner")
            loser_id = create_authority(conn, "Idempotent Loser")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
        first = review_decision_apply.apply_review_decision(
            conn,
            target=f"authority_reconciliation:{reconciliation_id}",
            decision_action="accept_merge",
            reviewer="operator",
            reason="Same identity.",
            decided_at=FIXED_TIMESTAMP,
        )
        second = review_decision_apply.apply_review_decision(
            conn,
            target=f"authority_reconciliation:{reconciliation_id}",
            decision_action="accept_merge",
            reviewer="operator",
            reason="Same identity.",
            decided_at=FIXED_TIMESTAMP,
        )
        merge_count = count_rows(conn, "authority_merge_event")
    finally:
        conn.close()

    assert first["status"] == "completed"
    assert second["status"] == "already_applied"
    assert merge_count == 1


def test_invalid_target_fails_without_mutation(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            prov = provenance(conn, "invalid-target")
            claim = canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:apply-review:invalid-target",
                about_object_ref="authority:x",
                claim_text="Not a reconciliation.",
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
        with pytest.raises(review_decision_apply.ReviewDecisionApplyError):
            review_decision_apply.apply_review_decision(
                conn,
                target=f"source_claim:{claim.row_id}",
                decision_action="accept_merge",
                reviewer="operator",
                reason="Invalid target.",
                decided_at=FIXED_TIMESTAMP,
            )
        assert count_rows(conn, "authority_merge_event") == 0
    finally:
        conn.close()


def test_unsafe_merge_incompatible_authority_types_refuses(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            winner_id = create_authority(conn, "Organization Winner", authority_type="organization")
            loser_id = create_authority(conn, "Person Loser", authority_type="person")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
        with pytest.raises(review_decision_apply.ReviewDecisionApplyError):
            review_decision_apply.apply_review_decision(
                conn,
                target=f"authority_reconciliation:{reconciliation_id}",
                decision_action="accept_merge",
                reviewer="operator",
                reason="Unsafe merge.",
                decided_at=FIXED_TIMESTAMP,
            )
        loser = conn.execute(
            "SELECT merged_into_authority_record_id FROM authority_record WHERE authority_record_id=?",
            (loser_id,),
        ).fetchone()
    finally:
        conn.close()

    assert loser["merged_into_authority_record_id"] is None


def test_expected_state_mismatch_fails_and_rolls_back(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            winner_id = create_authority(conn, "Mismatch Winner")
            loser_id = create_authority(conn, "Mismatch Loser")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
        with pytest.raises(review_decision_apply.ReviewDecisionApplyError):
            review_decision_apply.apply_review_decision(
                conn,
                target=f"authority_reconciliation:{reconciliation_id}",
                decision_action="accept_merge",
                reviewer="operator",
                reason="State mismatch.",
                expected_state="proposed",
                decided_at=FIXED_TIMESTAMP,
            )
        assert count_rows(conn, "authority_merge_event") == 0
    finally:
        conn.close()


def test_transaction_rolls_back_partial_merge_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    original_repoint = review_decision_apply.repoint_authority_references

    def failing_repoint(*args, **kwargs):
        if kwargs.get("dry_run"):
            return original_repoint(*args, **kwargs)
        raise RuntimeError("forced repoint failure")

    try:
        with conn:
            winner_id = create_authority(conn, "Rollback Winner")
            loser_id = create_authority(conn, "Rollback Loser")
            reconciliation_id = insert_reconciliation(conn, loser_id=loser_id, winner_id=winner_id)
        monkeypatch.setattr(review_decision_apply, "repoint_authority_references", failing_repoint)
        with pytest.raises(RuntimeError):
            review_decision_apply.apply_review_decision(
                conn,
                target=f"authority_reconciliation:{reconciliation_id}",
                decision_action="accept_merge",
                reviewer="operator",
                reason="Trigger rollback.",
                decided_at=FIXED_TIMESTAMP,
            )
        loser = conn.execute(
            "SELECT merged_into_authority_record_id, review_state FROM authority_record WHERE authority_record_id=?",
            (loser_id,),
        ).fetchone()
        rec = conn.execute(
            "SELECT review_state, accepted_authority_id FROM authority_reconciliation WHERE authority_reconciliation_id=?",
            (reconciliation_id,),
        ).fetchone()
        merge_count = count_rows(conn, "authority_merge_event")
    finally:
        conn.close()

    assert merge_count == 0
    assert loser["merged_into_authority_record_id"] is None
    assert loser["review_state"] == "needs_review"
    assert rec["review_state"] == "needs_review"
    assert rec["accepted_authority_id"] is None
