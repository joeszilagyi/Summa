from __future__ import annotations

import pytest

from tools.source_db_tools import authority_reconciliation, canonical_store

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
