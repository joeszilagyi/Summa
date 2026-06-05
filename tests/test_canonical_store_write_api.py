from __future__ import annotations

from pathlib import Path

import pytest

from tools.source_db_tools import canonical_store


FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"
OLDER_TIMESTAMP = "2026-06-02T09:08:07Z"
NEWER_TIMESTAMP = "2026-06-04T10:09:08Z"


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
        source_access_row = conn.execute(
            "SELECT review_state, provenance_event_ref FROM source_access WHERE source_access_id=?",
            (source_access.row_id,),
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
    assert source_access_row["review_state"] == "needs_review"
    assert work_row["provenance_event_ref"] == provenance.event_key
    assert source_access_row["provenance_event_ref"] == provenance.event_key
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


def test_work_upsert_does_not_demote_authority_envelope_on_pending_replay(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        accepted_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-work-authority",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-work-authority",
        )
        baseline = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-authority",
            provenance_event_ref=accepted_prov.event_key,
            work_type="article",
            title="Fixture Authority Work",
            review_state="accepted",
            confidence_score=0.95,
            authority_level="high",
            publication_state="published",
            public_blocker="trusted",
            accepted_for_citation=1,
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        proposed_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-work-authority-proposed",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-work-authority-proposed",
        )
        canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-authority",
            provenance_event_ref=proposed_prov.event_key,
            work_type="article",
            title="Fixture Authority Work Updated",
            review_state="proposed",
            confidence_score=0.10,
            authority_level="low",
            publication_state="draft",
            public_blocker="untrusted",
            accepted_for_citation=0,
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        row = conn.execute(
            "SELECT work_type, title, review_state, confidence_score, provenance_event_ref, authority_level, publication_state, public_blocker, accepted_for_citation FROM work WHERE work_id=?",
            (baseline.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["work_type"] == "article"
    assert row["title"] == "Fixture Authority Work Updated"
    assert row["review_state"] == "accepted"
    assert row["confidence_score"] == 0.95
    assert row["provenance_event_ref"] == accepted_prov.event_key
    assert row["authority_level"] == "high"
    assert row["publication_state"] == "published"
    assert row["public_blocker"] == "trusted"
    assert int(row["accepted_for_citation"]) == 1


def test_work_upsert_acceptance_replay_preserves_authority_envelope(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        accepted_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-work-authority-replay",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-work-authority-replay",
        )
        baseline = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-authority-replay",
            provenance_event_ref=accepted_provenance.event_key,
            work_type="article",
            title="Fixture Authority Work",
            review_state="accepted",
            confidence_score=0.95,
            authority_level="high",
            publication_state="published",
            public_blocker="trusted",
            accepted_for_citation=1,
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        replay_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-work-authority-replay-again",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-work-authority-replay-again",
        )
        canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-authority-replay",
            provenance_event_ref=replay_provenance.event_key,
            work_type="article",
            title="Fixture Authority Work Replayed",
            review_state="accepted",
            confidence_score=0.1,
            authority_level="low",
            publication_state="draft",
            public_blocker="untrusted",
            accepted_for_citation=0,
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        row = conn.execute(
            "SELECT review_state, confidence_score, provenance_event_ref, authority_level, publication_state, public_blocker, accepted_for_citation, title FROM work WHERE work_id=?",
            (baseline.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["review_state"] == "accepted"
    assert row["confidence_score"] == 0.95
    assert row["provenance_event_ref"] == accepted_provenance.event_key
    assert row["authority_level"] == "high"
    assert row["publication_state"] == "published"
    assert row["public_blocker"] == "trusted"
    assert int(row["accepted_for_citation"]) == 1
    assert row["title"] == "Fixture Authority Work Replayed"


def test_source_claim_replay_does_not_replace_authority_envelope_fields(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        accepted_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-claim-authority",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-claim-authority",
        )
        baseline = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=accepted_prov.event_key,
            source_claim_key_v1="claim:fixture-authority",
            about_object_ref="work:fixture-authority",
            claim_text="Baseline claim with high confidence.",
            claim_type="fixture_claim",
            review_state="accepted",
            confidence_score=0.95,
            authority_level="high",
            publication_state="published",
            public_blocker="trusted",
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        proposed_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-claim-authority-proposed",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-claim-authority-proposed",
        )
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=proposed_prov.event_key,
            source_claim_key_v1="claim:fixture-authority",
            about_object_ref="work:fixture-authority",
            claim_text="Baseline claim with low confidence.",
            claim_type="fixture_claim",
            review_state="proposed",
            confidence_score=0.10,
            authority_level="low",
            publication_state="draft",
            public_blocker="untrusted",
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        row = conn.execute(
            "SELECT claim_text, review_state, confidence_score, provenance_event_ref, authority_level, publication_state, public_blocker FROM source_claim WHERE source_claim_id=?",
            (baseline.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["review_state"] == "accepted"
    assert row["confidence_score"] == 0.95
    assert row["provenance_event_ref"] == accepted_prov.event_key
    assert row["authority_level"] == "high"
    assert row["publication_state"] == "published"
    assert row["public_blocker"] == "trusted"
    assert row["claim_text"] == "Baseline claim with high confidence."


def test_source_claim_acceptance_replay_preserves_claim_text_and_provenance(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        accepted_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-claim-authority-replay",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-claim-authority",
        )
        baseline = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=accepted_provenance.event_key,
            source_claim_key_v1="claim:fixture-authority-replay",
            about_object_ref="work:fixture-authority",
            claim_text="Claim text should be preserved.",
            claim_type="fixture_claim",
            review_state="accepted",
            confidence_score=0.95,
            authority_level="high",
            publication_state="published",
            public_blocker="trusted",
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        proposed_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-claim-authority-replay-proposed",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-claim-authority-replay-2",
        )
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=proposed_provenance.event_key,
            source_claim_key_v1="claim:fixture-authority-replay",
            about_object_ref="work:fixture-authority",
            claim_text="Mutated claim text should not overwrite.",
            claim_type="fixture_claim",
            review_state="accepted",
            confidence_score=0.10,
            authority_level="low",
            publication_state="draft",
            public_blocker="untrusted",
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        row = conn.execute(
            "SELECT claim_text, review_state, confidence_score, provenance_event_ref, authority_level, publication_state, public_blocker FROM source_claim WHERE source_claim_id=?",
            (baseline.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["claim_text"] == "Claim text should be preserved."
    assert row["review_state"] == "accepted"
    assert row["confidence_score"] == 0.95
    assert row["provenance_event_ref"] == accepted_provenance.event_key
    assert row["authority_level"] == "high"
    assert row["publication_state"] == "published"
    assert row["public_blocker"] == "trusted"


def test_source_relationship_replay_does_not_replace_authority_envelope_fields(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        accepted_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-relationship-authority",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-relationship-authority",
        )
        baseline = canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=accepted_prov.event_key,
            from_object_ref="work:fixture-alpha",
            predicate="mentions",
            to_object_ref="entity:fixture-alpha",
            target_label="Fixture Alpha",
            evidence_note="Baseline relation",
            review_state="accepted",
            confidence_score=0.95,
            authority_level="high",
            publication_state="published",
            public_blocker="trusted",
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        proposed_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-relationship-authority-proposed",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-relationship-authority-proposed",
        )
        canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=proposed_prov.event_key,
            from_object_ref="work:fixture-alpha",
            predicate="mentions",
            to_object_ref="entity:fixture-alpha",
            target_label="Fixture Alpha",
            evidence_note="Baseline relation",
            review_state="proposed",
            confidence_score=0.10,
            authority_level="low",
            publication_state="draft",
            public_blocker="untrusted",
            workspace_id="authoritative_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        row = conn.execute(
            "SELECT review_state, confidence_score, provenance_event_ref, authority_level, publication_state, public_blocker FROM source_relationship WHERE source_relationship_id=?",
            (baseline.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["review_state"] == "accepted"
    assert row["confidence_score"] == 0.95
    assert row["provenance_event_ref"] == accepted_prov.event_key
    assert row["authority_level"] == "high"
    assert row["publication_state"] == "published"
    assert row["public_blocker"] == "trusted"


def test_work_replay_preserves_monotonic_seen_timestamps(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        newer = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-002-newer",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-upsert-seen-newer",
        )
        first = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-seen-replay",
            provenance_event_ref=newer.event_key,
            work_type="article",
            title="Fixture Seen Replay",
            workspace_id="epsilon_subject",
            first_seen_at=NEWER_TIMESTAMP,
            last_seen_at=NEWER_TIMESTAMP,
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        older = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-002-older",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=OLDER_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-upsert-seen-older",
        )
        second = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-seen-replay",
            provenance_event_ref=older.event_key,
            work_type="article",
            title="Fixture Seen Replay",
            workspace_id="epsilon_subject",
            first_seen_at=OLDER_TIMESTAMP,
            last_seen_at=OLDER_TIMESTAMP,
            created_at=OLDER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT first_seen_at, last_seen_at, record_last_updated
            FROM work
            WHERE work_id=?
            """,
            (first.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert row["first_seen_at"] == OLDER_TIMESTAMP
    assert row["last_seen_at"] == NEWER_TIMESTAMP
    assert row["record_last_updated"] == NEWER_TIMESTAMP


def test_source_relationship_is_deduplicated_by_logical_identity(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        first_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="relationship-first",
            event_type="fixture_ingest",
            run_id="run-first",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:relationship-run-first",
        )
        first = canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=first_provenance.event_key,
            from_object_ref="work:1",
            to_object_ref="entity:alpha",
            predicate="mentions",
            target_label="Alpha",
            workspace_id="alpha_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )

        second_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="relationship-second",
            event_type="fixture_ingest",
            run_id="run-second",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:relationship-run-second",
        )
        second = canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=second_provenance.event_key,
            from_object_ref="work:1",
            to_object_ref="entity:alpha",
            predicate="mentions",
            target_label="Alpha",
            workspace_id="alpha_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM source_relationship WHERE from_object_ref=?",
            ("work:1",),
        ).fetchone()[0]
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert first.row_id == second.row_id
    assert count == 1


def test_source_claim_is_deduplicated_by_logical_identity_without_supplied_key(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        first_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="claim-first",
            event_type="fixture_ingest",
            run_id="claim-run-first",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:claim-run-first",
        )
        first = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=first_provenance.event_key,
            about_object_ref="work:fixture-alpha",
            claim_text="This work was authored in 2026.",
            claim_type="candidate_work",
            workspace_id="alpha_subject",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        second_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="claim-second",
            event_type="fixture_ingest",
            run_id="claim-run-second",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:claim-run-second",
        )
        second = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=second_provenance.event_key,
            about_object_ref="work:fixture-alpha",
            claim_text="This work was authored in 2026.",
            claim_type="candidate_work",
            workspace_id="alpha_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM source_claim WHERE about_object_ref=?",
            ("work:fixture-alpha",),
        ).fetchone()[0]
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert first.row_id == second.row_id
    assert count == 1


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


def test_source_access_without_work_or_lead_uses_provenance_lookup_path(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-004",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-fallback",
        )
        first = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            original_locator="https://example.test/source-access-fallback",
            canonical_url="https://example.test/source-access-fallback",
            workspace_id="delta_subject",
            citation_hint="Fixture fallback source access",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        second = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            original_locator="https://example.test/source-access-fallback",
            canonical_url="https://example.test/source-access-fallback",
            workspace_id="delta_subject",
            citation_hint="Fixture fallback source access updated",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT provenance_event_ref, citation_hint
            FROM source_access
            WHERE source_access_id=?
            """,
            (first.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert second.row_id == first.row_id
    assert row["provenance_event_ref"] == provenance.event_key
    assert row["citation_hint"] == "Fixture fallback source access updated"


def test_source_access_replay_preserves_monotonic_seen_timestamps(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        newer = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-004-newer",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-seen-newer",
        )
        work = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-source-access-seen",
            provenance_event_ref=newer.event_key,
            work_type="article",
            title="Fixture Source Access Seen",
            workspace_id="zeta_subject",
            first_seen_at=NEWER_TIMESTAMP,
            last_seen_at=NEWER_TIMESTAMP,
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        first = canonical_store.record_source_access(
            conn,
            provenance_event_ref=newer.event_key,
            work_id=work.row_id,
            source_lead_id="lead:fixture-source-access-seen",
            original_locator="https://example.test/source-access-seen",
            canonical_url="https://example.test/source-access-seen",
            citation_hint="Fixture source access seen",
            workspace_id="zeta_subject",
            first_seen_at=NEWER_TIMESTAMP,
            last_seen_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        older = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-004-older",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=OLDER_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-seen-older",
        )
        second = canonical_store.record_source_access(
            conn,
            provenance_event_ref=older.event_key,
            work_id=work.row_id,
            source_lead_id="lead:fixture-source-access-seen",
            original_locator="https://example.test/source-access-seen",
            canonical_url="https://example.test/source-access-seen",
            citation_hint="Fixture source access replayed",
            workspace_id="zeta_subject",
            first_seen_at=OLDER_TIMESTAMP,
            last_seen_at=OLDER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT first_seen_at, last_seen_at, record_last_updated, citation_hint
            FROM source_access
            WHERE source_access_id=?
            """,
            (first.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert row["first_seen_at"] == OLDER_TIMESTAMP
    assert row["last_seen_at"] == NEWER_TIMESTAMP
    assert row["record_last_updated"] == NEWER_TIMESTAMP
    assert row["citation_hint"] == "Fixture source access replayed"


def test_source_access_lead_identity_includes_workspace_and_locator(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-005",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-lead-identity",
        )
        first = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            source_lead_id="lead-shared",
            original_locator="https://example.test/source-access/one",
            workspace_id="workspace-one",
            citation_hint="Fixture source access one",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        second = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            source_lead_id="lead-shared",
            original_locator="https://example.test/source-access/two",
            workspace_id="workspace-two",
            citation_hint="Fixture source access two",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        rows = conn.execute(
            """
            SELECT source_access_id, source_lead_id, original_locator, workspace_id, citation_hint
            FROM source_access
            WHERE source_lead_id=?
            ORDER BY source_access_id ASC
            """,
            ("lead-shared",),
        ).fetchall()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is True
    assert second.row_id != first.row_id
    assert [int(row["source_access_id"]) for row in rows] == [first.row_id, second.row_id]
    assert [str(row["original_locator"]) for row in rows] == [
        "https://example.test/source-access/one",
        "https://example.test/source-access/two",
    ]
    assert [str(row["workspace_id"]) for row in rows] == ["workspace-one", "workspace-two"]
    assert [str(row["citation_hint"]) for row in rows] == [
        "Fixture source access one",
        "Fixture source access two",
    ]
