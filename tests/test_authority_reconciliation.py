from __future__ import annotations

import pytest

from tools.source_db_tools import authority_reconciliation, canonical_reconciliation, canonical_store

FIXED_TIMESTAMP = "2026-06-05T10:20:30Z"


def bootstrap_db(tmp_path):
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.authority_reconciliation",
    )
    return canonical_store.connect_canonical_store(db_path)


@pytest.mark.parametrize(
    "initial_state",
    ["machine_extracted", "needs_review", "proposed", "recorded", "unreviewed"],
)
def test_accept_candidate_promotes_pending_authority_linked_entity_state(tmp_path, initial_state: str) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        with conn:
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="jane-smith",
                review_state="needs_review",
                confidence_score=0.9,
                created_at=FIXED_TIMESTAMP,
            )
            prov = canonical_store.record_provenance_event(
                conn,
                object_namespace="authority-reconciliation-tests",
                object_id="accept-upgrade",
                event_type="authority_reconciliation",
                actor_type="pytest",
                actor_id="pytest.authority_reconciliation",
                tool_name="tests.test_authority_reconciliation",
                run_id="authority-recon-upgrade",
                event_timestamp=FIXED_TIMESTAMP,
                note_text="authority reconciliation acceptance upgrade test",
                provenance_event_key_v1="prov:authority-recon-upgrade",
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=prov.event_key,
                entity_label="Jane Smith",
                normalized_label="jane smith",
                entity_type="person",
                review_state=initial_state,
                confidence_score=0.8,
                record_last_updated=FIXED_TIMESTAMP,
            )
            reconciliation_id = authority_reconciliation.propose_candidate(
                conn,
                detected_entity_id=entity.row_id,
                raw_label="Jane Smith",
                entity_type="person",
                candidate_authority_id=authority_id,
                match_score=0.99,
                review_state="proposed",
            )
            authority_reconciliation.accept_candidate(
                conn,
                reconciliation_id,
                accepted_authority_id=authority_id,
                changed_at=FIXED_TIMESTAMP,
            )
            entity_row = conn.execute(
                """
                SELECT review_state, authority_record_id
                FROM extraction_detected_entity
                WHERE detected_entity_id=?
                """,
                (entity.row_id,),
            ).fetchone()
    finally:
        conn.close()

    assert entity_row["review_state"] == "accepted"
    assert entity_row["authority_record_id"] == authority_id


@pytest.mark.parametrize("initial_state", ["accepted", "approved", "curated", "reviewed"])
def test_accept_candidate_preserves_terminal_entity_review_state(tmp_path, initial_state: str) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        with conn:
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="jane-smith-terminal",
                review_state="needs_review",
                confidence_score=0.9,
                created_at=FIXED_TIMESTAMP,
            )
            prov = canonical_store.record_provenance_event(
                conn,
                object_namespace="authority-reconciliation-tests",
                object_id="accept-terminal",
                event_type="authority_reconciliation",
                actor_type="pytest",
                actor_id="pytest.authority_reconciliation",
                tool_name="tests.test_authority_reconciliation",
                run_id="authority-recon-terminal",
                event_timestamp=FIXED_TIMESTAMP,
                note_text="terminal review state preservation test",
                provenance_event_key_v1="prov:authority-recon-terminal",
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=prov.event_key,
                entity_label="Jane Smith",
                normalized_label="jane smith",
                entity_type="person",
                review_state=initial_state,
                confidence_score=0.8,
                record_last_updated=FIXED_TIMESTAMP,
            )
            reconciliation_id = authority_reconciliation.propose_candidate(
                conn,
                detected_entity_id=entity.row_id,
                raw_label="Jane Smith",
                entity_type="person",
                candidate_authority_id=authority_id,
                match_score=0.99,
                review_state="proposed",
            )
            authority_reconciliation.accept_candidate(
                conn,
                reconciliation_id,
                accepted_authority_id=authority_id,
                changed_at=FIXED_TIMESTAMP,
            )
            entity_row = conn.execute(
                """
                SELECT review_state, authority_record_id
                FROM extraction_detected_entity
                WHERE detected_entity_id=?
                """,
                (entity.row_id,),
            ).fetchone()
    finally:
        conn.close()

    assert entity_row["review_state"] == initial_state
    assert entity_row["authority_record_id"] == authority_id


@pytest.mark.parametrize("existing_state", ["accepted", "rejected", "curated"])
def test_record_authority_reconciliation_preserves_established_review_state_on_replay(
    tmp_path,
    existing_state: str,
) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        with conn:
            prov = canonical_store.record_provenance_event(
                conn,
                object_namespace="authority-reconciliation-tests",
                object_id=f"replay-{existing_state}",
                event_type="authority_reconciliation",
                actor_type="pytest",
                actor_id="pytest.authority_reconciliation",
                tool_name="tests.test_authority_reconciliation",
                run_id="authority-recon-replay",
                event_timestamp=FIXED_TIMESTAMP,
                note_text=f"{existing_state} replay baseline",
                provenance_event_key_v1=f"prov:authority-reconciliation-replay:{existing_state}",
            )
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="jane-smith-replay",
                review_state="needs_review",
                confidence_score=0.9,
                created_at=FIXED_TIMESTAMP,
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=prov.event_key,
                entity_label="Jane Smith",
                normalized_label="jane smith",
                entity_type="person",
                review_state="needs_review",
                confidence_score=0.8,
                record_last_updated=FIXED_TIMESTAMP,
            )
            baseline = canonical_reconciliation.record_authority_reconciliation(
                conn,
                detected_entity_id=entity.row_id,
                raw_label="Jane Smith",
                entity_type="person",
                candidate_authority_record_id=authority_id,
                method="authority-reconciliation",
                match_method="exact_name",
                confidence_score=0.99,
                evidence_context="before",
                review_state=existing_state,
                created_at=FIXED_TIMESTAMP,
            )
            replay = canonical_reconciliation.record_authority_reconciliation(
                conn,
                detected_entity_id=entity.row_id,
                raw_label="Jane Smith",
                entity_type="person",
                candidate_authority_record_id=authority_id,
                method="authority-reconciliation",
                match_method="exact_name",
                confidence_score=0.70,
                evidence_context="after",
                review_state="needs_review",
                created_at="2026-06-05T10:21:30Z",
            )
            row = conn.execute(
                """
                SELECT review_state, confidence_score, evidence_context
                FROM authority_reconciliation
                WHERE authority_reconciliation_id=?
                """,
                (baseline.row_id,),
            ).fetchone()
    finally:
        conn.close()

    assert baseline.created is True
    assert replay.created is False
    assert replay.row_id == baseline.row_id
    assert row["review_state"] == existing_state
    assert row["confidence_score"] == 0.7
    assert row["evidence_context"] == "after"
