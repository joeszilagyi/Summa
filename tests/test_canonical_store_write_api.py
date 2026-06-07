from __future__ import annotations

import sqlite3
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


class SourceAccessIntegrityProxy:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._skipped_lookup = False
        self._raised = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)

    def execute(self, sql: str, params: object = ()) -> object:
        if (
            not self._skipped_lookup
            and isinstance(sql, str)
            and sql.lstrip().upper().startswith("SELECT * FROM SOURCE_ACCESS")
        ):
            self._skipped_lookup = True

            class _EmptyCursor:
                def fetchone(self) -> None:
                    return None

            return _EmptyCursor()
        if (
            not self._raised
            and isinstance(sql, str)
            and sql.lstrip().upper().startswith("INSERT INTO SOURCE_ACCESS")
        ):
            self._raised = True
            raise sqlite3.IntegrityError("simulated source_access integrity failure")
        return self._conn.execute(sql, params)


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
            """
            SELECT review_state, workspace_id, provenance_event_ref
            FROM extraction_detected_entity
            WHERE detected_entity_id=?
            """,
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
    assert entity_row["workspace_id"] == "alpha_subject"
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


def test_write_api_allows_unscoped_detected_entity_without_workspace(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-unscoped-entity",
            event_type="fixture_ingest",
            tool_name="pytest",
            tool_version="1.0",
            run_id="fixture-unscoped-entity",
            event_timestamp=FIXED_TIMESTAMP,
            note_text="fixture ingest provenance",
            provenance_event_key_v1="prov:fixture-ingest:unscoped-entity",
        )
        entity = canonical_store.record_extraction_detected_entity(
            conn,
            provenance_event_ref=provenance.event_key,
            entity_label="Unscoped Example",
            normalized_label="unscoped example",
            entity_type="person",
            confidence_score=0.41,
            record_last_updated=FIXED_TIMESTAMP,
        )
        entity_row = conn.execute(
            """
            SELECT review_state, workspace_id, provenance_event_ref
            FROM extraction_detected_entity
            WHERE detected_entity_id=?
            """,
            (entity.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert entity.created is True
    assert entity_row["review_state"] == "proposed"
    assert entity_row["workspace_id"] is None
    assert entity_row["provenance_event_ref"] == provenance.event_key


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
        with pytest.raises(canonical_store.CanonicalStoreError, match="review_state"), conn:
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


def test_source_access_without_work_or_lead_replays_by_locator_without_duplicate_rows(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        first_provenance = canonical_store.record_provenance_event(
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
            provenance_event_ref=first_provenance.event_key,
            original_locator="https://example.test/source-access-fallback",
            canonical_url="https://example.test/source-access-fallback",
            workspace_id="delta_subject",
            citation_hint="Fixture fallback source access",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        second_provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-004b",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-fallback-replay",
        )
        second = canonical_store.record_source_access(
            conn,
            provenance_event_ref=second_provenance.event_key,
            original_locator="https://example.test/source-access-fallback",
            canonical_url="https://example.test/source-access-fallback",
            workspace_id="delta_subject",
            citation_hint="Fixture fallback source access updated",
            first_seen_at=NEWER_TIMESTAMP,
            last_seen_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT provenance_event_ref, citation_hint, first_seen_at, last_seen_at
            FROM source_access
            WHERE source_access_id=?
            """,
            (first.row_id,),
        ).fetchone()
        row_count = conn.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM source_access
            WHERE original_locator=?
              AND workspace_id=?
            """,
            ("https://example.test/source-access-fallback", "delta_subject"),
        ).fetchone()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert second.row_id == first.row_id
    assert row_count is not None
    assert int(row_count["row_count"]) == 1
    assert row["provenance_event_ref"] == second_provenance.event_key
    assert row["citation_hint"] == "Fixture fallback source access updated"
    assert row["first_seen_at"] == FIXED_TIMESTAMP
    assert row["last_seen_at"] == NEWER_TIMESTAMP


def test_source_access_integrity_fallback_handles_null_work_id(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-004-null-work",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-null-work",
        )
        baseline = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            original_locator="https://example.test/source-access-null-work",
            canonical_url="https://example.test/source-access-null-work",
            citation_hint="Fixture null-work source access",
            first_seen_at=NEWER_TIMESTAMP,
            last_seen_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        proxied = SourceAccessIntegrityProxy(conn)
        replay = canonical_store.record_source_access(
            proxied,
            provenance_event_ref=provenance.event_key,
            original_locator="https://example.test/source-access-null-work",
            canonical_url="https://example.test/source-access-null-work",
            citation_hint="Fixture null-work source access updated",
            first_seen_at=OLDER_TIMESTAMP,
            last_seen_at=OLDER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT citation_hint, first_seen_at, last_seen_at, record_last_updated
            FROM source_access
            WHERE source_access_id=?
            """,
            (baseline.row_id,),
        ).fetchone()
        row_count = conn.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM source_access
            WHERE original_locator=?
              AND work_id IS NULL
            """,
            ("https://example.test/source-access-null-work",),
        ).fetchone()
    finally:
        conn.close()

    assert baseline.created is True
    assert replay.created is False
    assert replay.row_id == baseline.row_id
    assert row_count is not None
    assert int(row_count["row_count"]) == 1
    assert row["citation_hint"] == "Fixture null-work source access"
    assert row["first_seen_at"] == OLDER_TIMESTAMP
    assert row["last_seen_at"] == NEWER_TIMESTAMP
    assert row["record_last_updated"] == NEWER_TIMESTAMP


def test_write_api_preserves_record_last_updated_monotonicity_on_replay(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        claim_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="monotonic-claim",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:monotonic-claim",
        )
        claim_first = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=claim_prov.event_key,
            source_claim_key_v1="claim:monotonic",
            about_object_ref="work:monotonic",
            claim_text="Monotonic claim.",
            claim_type="fixture_claim",
            workspace_id="monotonic_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        claim_second = canonical_store.record_source_claim(
            conn,
            provenance_event_ref=claim_prov.event_key,
            source_claim_key_v1="claim:monotonic",
            about_object_ref="work:monotonic",
            claim_text="Monotonic claim.",
            claim_type="fixture_claim",
            workspace_id="monotonic_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )

        capture_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="monotonic-capture",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:monotonic-capture",
        )
        capture_first = canonical_store.record_capture_event(
            conn,
            provenance_event_ref=capture_prov.event_key,
            original_locator="https://example.test/monotonic-capture",
            captured_at=NEWER_TIMESTAMP,
            capture_method="fixture_capture",
            content_hash="c" * 64,
            workspace_id="monotonic_subject",
            record_last_updated=NEWER_TIMESTAMP,
        )
        capture_second = canonical_store.record_capture_event(
            conn,
            provenance_event_ref=capture_prov.event_key,
            original_locator="https://example.test/monotonic-capture",
            captured_at=NEWER_TIMESTAMP,
            capture_method="fixture_capture",
            content_hash="c" * 64,
            workspace_id="monotonic_subject",
            record_last_updated=OLDER_TIMESTAMP,
        )

        extraction_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="monotonic-extraction",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:monotonic-extraction",
        )
        extraction_first = canonical_store.record_extraction_record(
            conn,
            provenance_event_ref=extraction_prov.event_key,
            capture_event_id=capture_first.row_id,
            extraction_method="fixture_extract",
            extraction_status="completed",
            input_hash="d" * 64,
            output_hash="e" * 64,
            byte_count_in=10,
            byte_count_out=5,
            workspace_id="monotonic_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        extraction_second = canonical_store.record_extraction_record(
            conn,
            provenance_event_ref=extraction_prov.event_key,
            capture_event_id=capture_first.row_id,
            extraction_method="fixture_extract",
            extraction_status="completed",
            input_hash="d" * 64,
            output_hash="e" * 64,
            byte_count_in=10,
            byte_count_out=5,
            workspace_id="monotonic_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )

        entity_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="monotonic-entity",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:monotonic-entity",
        )
        entity_first = canonical_store.record_extraction_detected_entity(
            conn,
            provenance_event_ref=entity_prov.event_key,
            extraction_id=extraction_first.row_id,
            capture_event_id=capture_first.row_id,
            entity_label="Monotonic Entity",
            normalized_label="monotonic entity",
            entity_type="person",
            workspace_id="monotonic_subject",
            record_last_updated=NEWER_TIMESTAMP,
        )
        entity_second = canonical_store.record_extraction_detected_entity(
            conn,
            provenance_event_ref=entity_prov.event_key,
            extraction_id=extraction_first.row_id,
            capture_event_id=capture_first.row_id,
            entity_label="Monotonic Entity",
            normalized_label="monotonic entity",
            entity_type="person",
            workspace_id="monotonic_subject",
            record_last_updated=OLDER_TIMESTAMP,
        )

        relationship_prov = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="monotonic-relationship",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=NEWER_TIMESTAMP,
            provenance_event_key_v1="prov:monotonic-relationship",
        )
        relationship_first = canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=relationship_prov.event_key,
            from_object_ref="work:monotonic",
            predicate="mentions",
            to_object_ref="entity:monotonic",
            workspace_id="monotonic_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=NEWER_TIMESTAMP,
        )
        relationship_second = canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=relationship_prov.event_key,
            from_object_ref="work:monotonic",
            predicate="mentions",
            to_object_ref="entity:monotonic",
            workspace_id="monotonic_subject",
            created_at=NEWER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )
        conn.commit()
        claim_row = conn.execute(
            "SELECT record_last_updated FROM source_claim WHERE source_claim_id=?",
            (claim_first.row_id,),
        ).fetchone()
        capture_row = conn.execute(
            "SELECT record_last_updated FROM capture_event WHERE capture_event_id=?",
            (capture_first.row_id,),
        ).fetchone()
        extraction_row = conn.execute(
            "SELECT record_last_updated FROM extraction_record WHERE extraction_id=?",
            (extraction_first.row_id,),
        ).fetchone()
        entity_row = conn.execute(
            "SELECT record_last_updated FROM extraction_detected_entity WHERE detected_entity_id=?",
            (entity_first.row_id,),
        ).fetchone()
        relationship_row = conn.execute(
            "SELECT record_last_updated FROM source_relationship WHERE source_relationship_id=?",
            (relationship_first.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert claim_first.created is True
    assert claim_second.created is False
    assert capture_first.created is True
    assert capture_second.created is False
    assert extraction_first.created is True
    assert extraction_second.created is False
    assert entity_first.created is True
    assert entity_second.created is False
    assert relationship_first.created is True
    assert relationship_second.created is False
    assert claim_row["record_last_updated"] == NEWER_TIMESTAMP
    assert capture_row["record_last_updated"] == NEWER_TIMESTAMP
    assert extraction_row["record_last_updated"] == NEWER_TIMESTAMP
    assert entity_row["record_last_updated"] == NEWER_TIMESTAMP
    assert relationship_row["record_last_updated"] == NEWER_TIMESTAMP


def test_write_api_preserves_immutable_capture_and_extraction_fields_on_replay(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            provenance_event = canonical_store.record_provenance_event(
                conn,
                object_namespace="fixture_ingest",
                object_id="immutable-row",
                event_type="fixture_ingest",
                tool_name="pytest",
                tool_version="1.0",
                run_id="immutable-run",
                event_timestamp=NEWER_TIMESTAMP,
                note_text="immutable row fixture",
                provenance_event_key_v1="prov:immutable-row",
            )
            capture_first = canonical_store.record_capture_event(
                conn,
                provenance_event_ref=provenance_event.event_key,
                original_locator="https://example.test/immutable",
                captured_at=NEWER_TIMESTAMP,
                capture_method="fixture_capture",
                content_hash="c" * 64,
                byte_count=128,
                mime_type="text/plain",
                workspace_id="immutable_subject",
                record_last_updated=NEWER_TIMESTAMP,
            )
            extraction_first = canonical_store.record_extraction_record(
                conn,
                provenance_event_ref=provenance_event.event_key,
                capture_event_id=capture_first.row_id,
                extraction_method="fixture_extract",
                extraction_status="completed",
                extractor_name="pytest",
                extractor_version="1.0",
                input_hash="d" * 64,
                output_hash="e" * 64,
                byte_count_in=128,
                byte_count_out=64,
                encoding_handling="utf8",
                truncation_status="not_truncated",
                workspace_id="immutable_subject",
                created_at=NEWER_TIMESTAMP,
                record_last_updated=NEWER_TIMESTAMP,
            )
            capture_second = canonical_store.record_capture_event(
                conn,
                provenance_event_ref=provenance_event.event_key,
                original_locator="https://example.test/immutable",
                captured_at=NEWER_TIMESTAMP,
                capture_method="fixture_capture",
                content_hash="c" * 64,
                byte_count=999,
                mime_type="application/pdf",
                workspace_id="immutable_subject",
                record_last_updated=OLDER_TIMESTAMP,
            )
            extraction_second = canonical_store.record_extraction_record(
                conn,
                provenance_event_ref=provenance_event.event_key,
                capture_event_id=capture_first.row_id,
                extraction_method="fixture_extract",
                extraction_status="completed",
                extractor_name="pytest",
                extractor_version="2.0",
                input_hash="d" * 64,
                output_hash="e" * 64,
                byte_count_in=256,
                byte_count_out=128,
                encoding_handling="latin1",
                truncation_status="truncated",
                workspace_id="immutable_subject",
                created_at=NEWER_TIMESTAMP,
                record_last_updated=OLDER_TIMESTAMP,
            )
            capture_row = conn.execute(
                """
                SELECT original_locator, captured_at, capture_method, content_hash, byte_count,
                       mime_type, provenance_event_ref, record_last_updated
                FROM capture_event
                WHERE capture_event_id=?
                """,
                (capture_first.row_id,),
            ).fetchone()
            extraction_row = conn.execute(
                """
                SELECT capture_event_id, extraction_method, input_hash, output_hash,
                       byte_count_in, byte_count_out, created_at, record_last_updated
                FROM extraction_record
                WHERE extraction_id=?
                """,
                (extraction_first.row_id,),
            ).fetchone()
    finally:
        conn.close()

    assert capture_first.created is True
    assert capture_second.created is False
    assert extraction_first.created is True
    assert extraction_second.created is False
    assert capture_second.row_id == capture_first.row_id
    assert extraction_second.row_id == extraction_first.row_id
    assert capture_row["original_locator"] == "https://example.test/immutable"
    assert capture_row["captured_at"] == NEWER_TIMESTAMP
    assert capture_row["capture_method"] == "fixture_capture"
    assert capture_row["content_hash"] == "c" * 64
    assert capture_row["byte_count"] == 128
    assert capture_row["provenance_event_ref"] == provenance_event.event_key
    assert capture_row["record_last_updated"] == NEWER_TIMESTAMP
    assert extraction_row["capture_event_id"] == capture_first.row_id
    assert extraction_row["extraction_method"] == "fixture_extract"
    assert extraction_row["input_hash"] == "d" * 64
    assert extraction_row["output_hash"] == "e" * 64
    assert extraction_row["byte_count_in"] == 128
    assert extraction_row["byte_count_out"] == 64
    assert extraction_row["created_at"] == NEWER_TIMESTAMP
    assert extraction_row["record_last_updated"] == NEWER_TIMESTAMP


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


def test_source_access_work_id_replay_does_not_blend_distinct_leads(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture_ingest",
            object_id="fixture-006",
            event_type="fixture_ingest",
            tool_name="pytest",
            event_timestamp=FIXED_TIMESTAMP,
            provenance_event_key_v1="prov:fixture-source-access-work-id-replay",
        )
        work = canonical_store.upsert_work(
            conn,
            work_key_v1="work:fixture-source-access-work-id",
            provenance_event_ref=provenance.event_key,
            work_type="article",
            title="Fixture Source Access Work",
            workspace_id="theta_subject",
            first_seen_at=OLDER_TIMESTAMP,
            last_seen_at=OLDER_TIMESTAMP,
            created_at=OLDER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )
        first = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            work_id=work.row_id,
            source_lead_id="lead-alpha",
            source_locus_id="locus-alpha",
            original_locator="https://example.test/source-access-work-id",
            canonical_url="https://example.test/source-alpha",
            citation_hint="Fixture source access alpha",
            workspace_id="theta_subject",
            first_seen_at=OLDER_TIMESTAMP,
            last_seen_at=OLDER_TIMESTAMP,
            record_last_updated=OLDER_TIMESTAMP,
        )
        second = canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            work_id=work.row_id,
            source_lead_id="lead-beta",
            source_locus_id="locus-beta",
            original_locator="https://example.test/source-access-work-id",
            canonical_url="https://example.test/source-beta",
            citation_hint="Fixture source access beta",
            workspace_id="theta_subject",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        row_count = conn.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM source_access
            WHERE work_id=? AND original_locator=?
            """,
            (work.row_id, "https://example.test/source-access-work-id"),
        ).fetchone()
        row = conn.execute(
            """
            SELECT source_lead_id, source_locus_id, canonical_url, citation_hint,
                   first_seen_at, last_seen_at, record_last_updated
            FROM source_access
            WHERE source_access_id=?
            """,
            (first.row_id,),
        ).fetchone()
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert int(row_count["row_count"]) == 1
    assert first.row_id == second.row_id
    assert str(row["source_lead_id"]) == "lead-alpha"
    assert str(row["source_locus_id"]) == "locus-alpha"
    assert str(row["canonical_url"]) == "https://example.test/source-alpha"
    assert str(row["citation_hint"]) == "Fixture source access alpha"
    assert row["first_seen_at"] == OLDER_TIMESTAMP
    assert row["last_seen_at"] == FIXED_TIMESTAMP
    assert row["record_last_updated"] == FIXED_TIMESTAMP


def test_record_review_state_history_uses_single_lookup_for_existing_rows(
    tmp_path: Path,
) -> None:
    class CountingConnection(sqlite3.Connection):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.executed_sql: list[str] = []

        def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:  # type: ignore[override]
            self.executed_sql.append(sql)
            return super().execute(sql, parameters)

    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.canonical_store",
    )
    conn = sqlite3.connect(db_path, factory=CountingConnection)
    conn.row_factory = sqlite3.Row
    try:
        first = canonical_store.record_review_state_history(
            conn,
            target_namespace="source_claim",
            target_id="claim:1",
            previous_state="proposed",
            new_state="needs_review",
            changed_by="pytest",
            changed_at=FIXED_TIMESTAMP,
            source_tool="pytest",
            source_run_id="history-run",
        )
        second = canonical_store.record_review_state_history(
            conn,
            target_namespace="source_claim",
            target_id="claim:1",
            previous_state="proposed",
            new_state="needs_review",
            changed_by="pytest",
            changed_at=FIXED_TIMESTAMP,
            source_tool="pytest",
            source_run_id="history-run",
        )
    finally:
        executed_sql = getattr(conn, "executed_sql", [])
        conn.close()

    assert first.created is True
    assert second.created is False
    assert first.row_id == second.row_id
    rowid_lookups = [
        sql
        for sql in executed_sql
        if sql.strip().startswith("SELECT rowid FROM review_state_history")
    ]
    assert len(rowid_lookups) == 0
