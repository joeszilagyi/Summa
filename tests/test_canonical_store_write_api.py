from __future__ import annotations

from pathlib import Path

import pytest

from tools.source_db_tools import canonical_store


FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.write_api",
    )
    return db_path


def test_write_api_records_provenance_and_core_rows(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-001",
            event_type="fixture_ingest",
            tool_name="pytest",
            tool_version="1.0",
            run_id="fixture-run",
            event_timestamp=FIXED_TIMESTAMP,
            note_text="fixture ingest provenance",
            provenance_event_key_v1="prov:fixture-ingest",
        )
        work = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-alpha",
            provenance_event_ref=provenance.event_key,
            work_type="article",
            title="Fixture Alpha Work",
            workspace_id="alpha_subject",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        source_access = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            work_id=work.row_id,
            source_lead_id="lead:fixture-alpha",
            original_locator="https://example.test/fixture-alpha",
            canonical_url="https://example.test/fixture-alpha",
            citation_hint="Fixture Alpha citation",
            workspace_id="alpha_subject",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        claim = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=provenance.event_key,
            source_claim_key_v1="claim:fixture-alpha",
            about_object_ref=f"work:{work.row_id}",
            claim_text="Fixture Alpha makes a reviewable claim.",
            claim_type="fixture_claim",
            workspace_id="alpha_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        capture = canonical_store.record_capture_event(
            conn,
            provenance_event_ref=provenance.event_key,
            original_locator="https://example.test/fixture-alpha",
            captured_at=FIXED_TIMESTAMP,
            capture_method="fixture_capture",
            work_id=work.row_id,
            content_hash="a" * 64,
            byte_count=128,
            mime_type="text/plain",
            workspace_id="alpha_subject",
            record_last_updated=FIXED_TIMESTAMP,
        )
        extraction = canonical_store.record_extraction_record(
            conn,
            provenance_event_ref=provenance.event_key,
            capture_event_id=capture.row_id,
            extraction_method="fixture_extract",
            extraction_status="completed",
            extractor_name="pytest",
            extractor_version="1.0",
            input_hash="a" * 64,
            output_hash="b" * 64,
            byte_count_in=128,
            byte_count_out=64,
            encoding_handling="utf8",
            truncation_status="not_truncated",
            workspace_id="alpha_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        entity = canonical_store.record_extraction_detected_entity(
            conn,
            provenance_event_ref=provenance.event_key,
            extraction_id=extraction.row_id,
            capture_event_id=capture.row_id,
            entity_label="Alpha Example",
            normalized_label="alpha example",
            entity_type="person",
            confidence_score=0.41,
            record_last_updated=FIXED_TIMESTAMP,
        )
        relationship = canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=provenance.event_key,
            from_object_ref=f"work:{work.row_id}",
            to_object_ref="entity:alpha-example",
            predicate="mentions",
            target_label="Alpha Example",
            workspace_id="alpha_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        history = canonical_store.record_review_state_history(
            conn,
            target_namespace="source_claim",
            target_id=claim.row_id,
            previous_state="proposed",
            new_state="needs_review",
            changed_by="pytest",
            changed_at=FIXED_TIMESTAMP,
            source_tool="pytest",
            source_run_id="fixture-run",
        )
        conn.commit()

        work_row = conn.execute(
            "SELECT review_state, accepted_for_citation, provenance_event_ref FROM work WHERE work_id=?",
            (work.row_id,),
        ).fetchone()
        claim_row = conn.execute(
            "SELECT review_state, provenance_event_ref FROM source_claim WHERE source_claim_id=?",
            (claim.row_id,),
        ).fetchone()
        capture_row = conn.execute(
            "SELECT review_state, provenance_event_ref FROM capture_event WHERE capture_event_id=?",
            (capture.row_id,),
        ).fetchone()
        extraction_row = conn.execute(
            "SELECT review_state, provenance_event_ref FROM extraction_record WHERE extraction_id=?",
            (extraction.row_id,),
        ).fetchone()
        entity_row = conn.execute(
            "SELECT review_state, provenance_event_ref FROM extraction_detected_entity WHERE detected_entity_id=?",
            (entity.row_id,),
        ).fetchone()
        relationship_row = conn.execute(
            "SELECT review_state, provenance_event_ref FROM source_relationship WHERE source_relationship_id=?",
            (relationship.row_id,),
        ).fetchone()
        history_row = conn.execute(
            "SELECT new_state FROM review_state_history WHERE rowid=?",
            (history.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert provenance.event_id > 0
    assert work.created is True
    assert source_access.created is True
    assert claim.created is True
    assert capture.created is True
    assert extraction.created is True
    assert entity.created is True
    assert relationship.created is True
    assert history.created is True
    assert work_row["review_state"] == "needs_review"
    assert int(work_row["accepted_for_citation"]) == 0
    assert claim_row["review_state"] == "proposed"
    assert capture_row["review_state"] == "needs_review"
    assert extraction_row["review_state"] == "needs_review"
    assert entity_row["review_state"] == "proposed"
    assert relationship_row["review_state"] == "proposed"
    assert work_row["provenance_event_ref"] == provenance.event_key
    assert claim_row["provenance_event_ref"] == provenance.event_key
    assert capture_row["provenance_event_ref"] == provenance.event_key
    assert extraction_row["provenance_event_ref"] == provenance.event_key
    assert entity_row["provenance_event_ref"] == provenance.event_key
    assert relationship_row["provenance_event_ref"] == provenance.event_key
    assert history_row["new_state"] == "needs_review"


def test_work_upsert_is_idempotent_and_preserves_reviewed_state(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-002",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-upsert",
        )
        first = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-beta",
            provenance_event_ref=provenance.event_key,
            work_type="article",
            title="Fixture Beta Work",
            review_state="needs_review",
            workspace_id="beta_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        conn.execute(
            "UPDATE work SET review_state='accepted', accepted_for_citation=1 WHERE work_id=?",
            (first.row_id,),
        )
        second = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-beta",
            provenance_event_ref=provenance.event_key,
            work_type="article",
            title="Fixture Beta Work Updated",
            review_state="needs_review",
            workspace_id="beta_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        conn.commit()
        row = conn.execute(
            "SELECT title, review_state, accepted_for_citation FROM work WHERE work_id=?",
            (first.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert second.row_id == first.row_id
    assert row["title"] == "Fixture Beta Work Updated"
    assert row["review_state"] == "accepted"
    assert int(row["accepted_for_citation"]) == 1


def test_write_api_rolls_back_transaction_on_invalid_review_state(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(canonical_store.CanonicalStoreError, match="review_state"):
            with conn:
                provenance = canonical_store.record_provenance_event(
                    conn,
                    object_namespace="fixture_ingest",
                    object_id="fixture-003",
                    event_type="fixture_ingest",
                    tool_name="pytest",
                    event_timestamp=FIXED_TIMESTAMP,
                    provenance_event_key_v1="prov:fixture-rollback",
                )
                canonical_store.upsert_work(
                    conn,
                    work_key_v1="work:fixture-gamma",
                    provenance_event_ref=provenance.event_key,
                    work_type="article",
                    title="Fixture Gamma Work",
                    workspace_id="gamma_subject",
                    created_at=FIXED_TIMESTAMP,
                    record_last_updated=FIXED_TIMESTAMP,
                )
                canonical_store.record_source_claim(
                    conn,
                    provenance_event_ref=provenance.event_key,
                    claim_text="This write should roll back.",
                    review_state="not_a_valid_review_state",
                    created_at=FIXED_TIMESTAMP,
                    record_last_updated=FIXED_TIMESTAMP,
                )

        counts = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert counts["provenance_event"] == 0
    assert counts["work"] == 0
    assert counts["source_claim"] == 0
