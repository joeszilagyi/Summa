from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from tools.source_db_tools import authority_reconciliation, canonical_ingest, canonical_reconciliation, canonical_store


FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.canonical_dedup",
    )
    return db_path


def batch_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_batch(
    candidates: list[dict[str, object]],
    *,
    run_id: str,
    subject_id: str = "fixture_subject",
) -> dict[str, object]:
    return {
        "schema_version": "gather-candidate-batch.v1",
        "run_id": run_id,
        "created_at": FIXED_TIMESTAMP,
        "subject": {"subject_id": subject_id},
        "candidates": candidates,
    }


def work_candidate(
    candidate_id: str,
    *,
    work_key: str | None,
    title: str,
    work_type: str = "article",
    identifier_scheme: str | None = None,
    identifier_value: str | None = None,
    canonical_url: str | None = None,
    original_locator: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": title,
        "work_type": work_type,
    }
    if work_key is not None:
        payload["work_key"] = work_key
    if identifier_scheme and identifier_value:
        payload["identifier_scheme"] = identifier_scheme
        payload["identifier_value"] = identifier_value
    if canonical_url:
        payload["canonical_url"] = canonical_url
    if original_locator:
        payload["original_locator"] = original_locator
    return {
        "candidate_id": candidate_id,
        "candidate_type": "work",
        "origin": "llm_proposed",
        "persistence_status": "workspace_run_only",
        "review_status": "unverified",
        "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }


def source_lead_candidate(
    candidate_id: str,
    *,
    original_locator: str,
    canonical_url: str | None = None,
    source_lead_id: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "original_locator": original_locator,
    }
    if canonical_url is not None:
        payload["canonical_url"] = canonical_url
    if source_lead_id is not None:
        payload["source_lead_id"] = source_lead_id
    return {
        "candidate_id": candidate_id,
        "candidate_type": "source_lead",
        "origin": "llm_proposed",
        "persistence_status": "workspace_run_only",
        "review_status": "unverified",
        "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }


def entity_candidate(
    candidate_id: str,
    *,
    label: str,
    entity_type: str = "person",
    identifiers: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "entity_label": label,
        "normalized_label": label.casefold(),
        "entity_type": entity_type,
        "confidence_score": 0.41,
    }
    if identifiers:
        payload["identifiers"] = identifiers
    return {
        "candidate_id": candidate_id,
        "candidate_type": "person",
        "origin": "llm_proposed",
        "persistence_status": "workspace_run_only",
        "review_status": "unverified",
        "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }


def structured_claim_candidate(
    candidate_id: str,
    *,
    payload: dict[str, object],
    candidate_type: str = "unknown",
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "origin": "llm_proposed",
        "persistence_status": "workspace_run_only",
        "review_status": "unverified",
        "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }


def relationship_candidate(
    candidate_id: str,
    *,
    from_object_ref: str,
    predicate: str,
    to_object_ref: str,
    evidence_note: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "from_object_ref": from_object_ref,
        "predicate": predicate,
        "to_object_ref": to_object_ref,
    }
    if evidence_note is not None:
        payload["evidence_note"] = evidence_note
    return {
        "candidate_id": candidate_id,
        "candidate_type": "relationship",
        "origin": "llm_proposed",
        "persistence_status": "workspace_run_only",
        "review_status": "unverified",
        "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
    }


def prose_claim_candidate(candidate_id: str, text: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_type": "open_question",
        "origin": "llm_proposed",
        "persistence_status": "workspace_run_only",
        "review_status": "unverified",
        "text": text,
    }


def ingest_batch(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    *,
    batch_name: str,
    db_path: Path,
) -> dict[str, object]:
    return canonical_ingest.ingest_candidate_batch(
        conn,
        payload,
        batch_path=db_path.parent / batch_name,
        batch_hash=batch_hash(payload),
        db_path=db_path,
    )


def test_exact_work_duplicate_reuses_existing_row_and_records_duplicate_event(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    first_batch = build_batch(
        [
            work_candidate(
                "cand:work.1",
                work_key="work.fixture.example-work.a",
                title="Example Work",
                identifier_scheme="doi",
                identifier_value="10.1234/example-work",
                canonical_url="https://example.test/work/example-work",
            )
        ],
        run_id="gather-work-a",
    )
    second_batch = build_batch(
        [
            work_candidate(
                "cand:work.2",
                work_key="work.fixture.example-work.b",
                title="Example Work",
                identifier_scheme="doi",
                identifier_value="10.1234/example-work",
                canonical_url="https://example.test/work/example-work",
            )
        ],
        run_id="gather-work-b",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, first_batch, batch_name="batch-a.json", db_path=db_path)
        with conn:
            report = ingest_batch(conn, second_batch, batch_name="batch-b.json", db_path=db_path)
        work_rows = conn.execute("SELECT work_id, work_key_v1 FROM work").fetchall()
        duplicate_events = conn.execute(
            "SELECT event_type FROM provenance_event WHERE event_type='work_duplicate_encountered'"
        ).fetchall()
        work_identifiers = conn.execute("SELECT scheme, value FROM work_identifier").fetchall()
    finally:
        conn.close()

    assert len(work_rows) == 1
    assert len(duplicate_events) == 1
    assert len(work_identifiers) == 1
    assert report["counts"]["deduped"]["work"] == 1


def test_same_title_different_locator_does_not_collapse(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch_one = build_batch(
        [
            work_candidate(
                "cand:similar.1",
                work_key="work.fixture.similar.one",
                title="Shared Title",
                canonical_url="https://example.test/work/one",
            )
        ],
        run_id="gather-similar-a",
    )
    batch_two = build_batch(
        [
            work_candidate(
                "cand:similar.2",
                work_key="work.fixture.similar.two",
                title="Shared Title",
                canonical_url="https://example.test/work/two",
            )
        ],
        run_id="gather-similar-b",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch_one, batch_name="similar-a.json", db_path=db_path)
        with conn:
            ingest_batch(conn, batch_two, batch_name="similar-b.json", db_path=db_path)
        count = conn.execute("SELECT COUNT(*) FROM work").fetchone()[0]
    finally:
        conn.close()

    assert count == 2


def test_work_without_explicit_key_reuses_row_across_batches_by_normalized_identity(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    first_batch = build_batch(
        [
            work_candidate(
                "cand:implicit.work.1",
                work_key=None,
                title="Logical Work Title",
                work_type="article",
                canonical_url=None,
            )
        ],
        run_id="gather-no-key-a",
    )
    second_batch = build_batch(
        [
            work_candidate(
                "cand:implicit.work.2",
                work_key=None,
                title="  logical   work   title ",
                work_type="article",
                canonical_url=None,
            )
        ],
        run_id="gather-no-key-b",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, first_batch, batch_name="implicit-work-a.json", db_path=db_path)
        with conn:
            ingest_batch(conn, second_batch, batch_name="implicit-work-b.json", db_path=db_path)
        rows = conn.execute("SELECT work_key_v1 FROM work ORDER BY work_id").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert len(set(row["work_key_v1"] for row in rows)) == 1


def test_source_lead_without_explicit_id_reuses_row_across_batches(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    first_batch = build_batch(
        [
            source_lead_candidate(
                "cand:source-lead.1",
                original_locator="https://example.test/lead/example",
            )
        ],
        run_id="gather-source-lead-a",
    )
    second_batch = build_batch(
        [
            source_lead_candidate(
                "cand:source-lead.2",
                original_locator="https://example.test/lead/example",
            )
        ],
        run_id="gather-source-lead-b",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, first_batch, batch_name="source-lead-a.json", db_path=db_path)
        with conn:
            ingest_batch(conn, second_batch, batch_name="source-lead-b.json", db_path=db_path)
        rows = conn.execute(
            "SELECT source_access_id, source_lead_id FROM source_access ORDER BY source_access_id"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["source_lead_id"] is not None


def test_claim_without_batch_scoped_key_is_stable_across_batches(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    first_batch = build_batch(
        [
            structured_claim_candidate(
                "cand:claim.1",
                candidate_type="open_question",
                payload={
                    "about_object_ref": "work:fixture-claim",
                    "claim_type": "summary",
                    "summary": "The entity is notable.",
                },
            )
        ],
        run_id="gather-claim-a",
    )
    second_batch = build_batch(
        [
            structured_claim_candidate(
                "cand:claim.2",
                candidate_type="open_question",
                payload={
                    "about_object_ref": "work:fixture-claim",
                    "claim_type": "summary",
                    "summary": "The entity is notable.",
                },
            )
        ],
        run_id="gather-claim-b",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, first_batch, batch_name="claim-a.json", db_path=db_path)
        with conn:
            ingest_batch(conn, second_batch, batch_name="claim-b.json", db_path=db_path)
        rows = conn.execute("SELECT source_claim_id FROM source_claim ORDER BY source_claim_id").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1


def test_authority_label_match_creates_reviewable_reconciliation_candidate(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="authority:jane-smith",
                review_state="accepted",
                confidence_score=1.0,
                created_at=FIXED_TIMESTAMP,
            )
            report = ingest_batch(
                conn,
                build_batch(
                    [entity_candidate("cand:entity.1", label="Jane Smith")],
                    run_id="gather-entity-review",
                ),
                batch_name="entity-review.json",
                db_path=db_path,
            )
        rec_row = conn.execute(
            """
            SELECT review_state, confidence_score
            FROM authority_reconciliation
            """
        ).fetchone()
        entity_row = conn.execute(
            "SELECT authority_record_id, review_state FROM extraction_detected_entity"
        ).fetchone()
        merge_count = conn.execute("SELECT COUNT(*) FROM authority_merge_event").fetchone()[0]
    finally:
        conn.close()

    assert rec_row["review_state"] == "needs_review"
    assert rec_row["confidence_score"] == pytest.approx(0.75)
    assert entity_row["authority_record_id"] is None
    assert entity_row["review_state"] == "proposed"
    assert merge_count == 0
    assert report["counts"]["reconciled"]["authority_reconciliation"] == 1


def test_exact_authority_identifier_match_records_merge_event_without_auto_accepting_claims(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="authority:jane-smith.orcid",
                review_state="accepted",
                confidence_score=1.0,
                created_at=FIXED_TIMESTAMP,
            )
            authority_reconciliation.add_authority_identifier(
                conn,
                authority_record_id=authority_id,
                scheme="orcid",
                value="0000-0002-1825-0097",
                is_primary=1,
                confidence_score=1.0,
                review_state="accepted",
                verified_at=FIXED_TIMESTAMP,
            )
            report = ingest_batch(
                conn,
                build_batch(
                    [
                        entity_candidate(
                            "cand:entity.2",
                            label="Jane Smith",
                            identifiers=[
                                {"scheme": "orcid", "value": "0000-0002-1825-0097"}
                            ],
                        )
                    ],
                    run_id="gather-entity-exact",
                ),
                batch_name="entity-exact.json",
                db_path=db_path,
            )
        merge_rows = conn.execute(
            "SELECT from_authority_record_id, into_authority_record_id FROM authority_merge_event"
        ).fetchall()
        linked_entity = conn.execute(
            "SELECT authority_record_id FROM extraction_detected_entity"
        ).fetchone()
        authority_rows = conn.execute(
            "SELECT authority_record_id, merged_into_authority_record_id FROM authority_record ORDER BY authority_record_id"
        ).fetchall()
    finally:
        conn.close()

    assert len(merge_rows) == 1
    assert linked_entity["authority_record_id"] == authority_id
    assert any(
        row["authority_record_id"] != authority_id
        and row["merged_into_authority_record_id"] == authority_id
        for row in authority_rows
    )
    assert report["counts"]["deduped"]["authority"] == 1


def test_structured_taught_by_impossibility_creates_contradiction_and_review_history(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    contradiction_batch = build_batch(
        [
            structured_claim_candidate(
                "cand:birth.a",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                    "public_summary": "Person A birth year 1940.",
                },
            ),
            structured_claim_candidate(
                "cand:death.b",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                    "public_summary": "Person B death year 1938.",
                },
            ),
            structured_claim_candidate(
                "cand:taught-by",
                payload={
                    "claim_type": "taught_by",
                    "about_object_ref": "authority:person-a",
                    "from_object_ref": "authority:person-a",
                    "to_object_ref": "authority:person-b",
                    "predicate": "taught_by",
                    "public_summary": "Person A was taught by Person B.",
                },
            ),
        ],
        run_id="gather-contradiction",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = ingest_batch(
                conn,
                contradiction_batch,
                batch_name="contradiction.json",
                db_path=db_path,
            )
        claims = conn.execute(
            "SELECT claim_type, review_state FROM source_claim ORDER BY source_claim_id"
        ).fetchall()
        contradiction_rows = conn.execute(
            """
            SELECT predicate, evidence_note, review_state
            FROM source_relationship
            WHERE predicate='contradicts'
            """
        ).fetchall()
        history_rows = conn.execute(
            """
            SELECT target_namespace, new_state, reason
            FROM review_state_history
            WHERE reason='structured_taught_by_impossible_life_overlap'
            ORDER BY rowid
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(claims) == 3
    assert any(row["review_state"] == "needs_review" for row in claims)
    assert len(contradiction_rows) == 1
    assert contradiction_rows[0]["review_state"] == "needs_review"
    assert "birth year 1940 is after object death year 1938" in contradiction_rows[0]["evidence_note"]
    assert history_rows
    assert report["counts"]["contradicted"]["source_claim"] >= 1
    assert report["counts"]["contradicted"]["source_relationship"] == 1
    assert {"accepted", "verified"} & {row["review_state"] for row in claims} == set()


def test_relational_taught_by_lifespan_impossibility_flags_relationship_without_deleting_facts(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.birth.a",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:rel.death.b",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            relationship_candidate(
                "cand:rel.taught-by",
                from_object_ref="authority:person-a",
                predicate="taught_by",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="gather-relational-contradiction",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = ingest_batch(conn, batch, batch_name="relational.json", db_path=db_path)
        original_relationship = conn.execute(
            """
            SELECT source_relationship_id, review_state
            FROM source_relationship
            WHERE predicate='taught_by'
            """
        ).fetchone()
        contradiction_rows = conn.execute(
            """
            SELECT from_object_ref, to_object_ref, predicate, target_label, evidence_note, review_state
            FROM source_relationship
            WHERE predicate='contradicts'
            """
        ).fetchall()
        claim_count = conn.execute("SELECT COUNT(*) FROM source_claim").fetchone()[0]
        history_rows = conn.execute(
            """
            SELECT target_namespace, target_id, new_state, reason, note
            FROM review_state_history
            WHERE reason='relational_temporal_lifespan_overlap'
            """
        ).fetchall()
    finally:
        conn.close()

    assert claim_count == 2
    assert original_relationship is not None
    assert original_relationship["review_state"] == "needs_review"
    assert len(contradiction_rows) == 1
    assert contradiction_rows[0]["from_object_ref"] == f"source_relationship:{original_relationship['source_relationship_id']}"
    assert contradiction_rows[0]["target_label"] == "relational_temporal_lifespan_overlap"
    assert "subject birth year 1940 is after object death year 1938" in contradiction_rows[0]["evidence_note"]
    assert contradiction_rows[0]["review_state"] == "needs_review"
    assert history_rows
    assert history_rows[0]["target_namespace"] == "source_relationship"
    assert report["counts"]["contradicted"]["source_relationship"] == 1


def test_relational_taught_by_with_insufficient_data_does_not_flag(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.death.insufficient",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            relationship_candidate(
                "cand:rel.taught-by.insufficient",
                from_object_ref="authority:person-a",
                predicate="taught_by",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="gather-relational-insufficient",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="relational-insufficient.json", db_path=db_path)
        contradiction_count = conn.execute(
            "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()[0]
        relationship_state = conn.execute(
            "SELECT review_state FROM source_relationship WHERE predicate='taught_by'"
        ).fetchone()["review_state"]
    finally:
        conn.close()

    assert contradiction_count == 0
    assert relationship_state == "proposed"


def test_relational_taught_by_with_overlapping_lifespans_does_not_flag(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.birth.overlap",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:rel.death.overlap",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1990,
                },
            ),
            relationship_candidate(
                "cand:rel.taught-by.overlap",
                from_object_ref="authority:person-a",
                predicate="taught_by",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="gather-relational-overlap",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="relational-overlap.json", db_path=db_path)
        contradiction_count = conn.execute(
            "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert contradiction_count == 0


def test_relational_met_non_overlap_flags_contradiction(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.birth.met",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:rel.death.met",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            relationship_candidate(
                "cand:rel.met",
                from_object_ref="authority:person-a",
                predicate="met",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="gather-relational-met",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="relational-met.json", db_path=db_path)
        contradiction = conn.execute(
            "SELECT target_label, evidence_note FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()
    finally:
        conn.close()

    assert contradiction["target_label"] == "relational_temporal_lifespan_overlap"
    assert "predicate met is impossible" in contradiction["evidence_note"]


def test_relational_influenced_is_conservative_for_posthumous_influence(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.birth.influenced",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:rel.death.influenced",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            relationship_candidate(
                "cand:rel.influenced",
                from_object_ref="authority:person-a",
                predicate="influenced",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="gather-relational-influenced",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = ingest_batch(conn, batch, batch_name="relational-influenced.json", db_path=db_path)
        contradiction_count = conn.execute(
            "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()[0]
        relationship_state = conn.execute(
            "SELECT review_state FROM source_relationship WHERE predicate='influenced'"
        ).fetchone()["review_state"]
    finally:
        conn.close()

    assert contradiction_count == 0
    assert relationship_state == "proposed"
    assert report["counts"]["contradicted"].get("source_relationship", 0) == 0


def test_relational_event_year_outside_lifespan_flags_relationship(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.birth.event",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:rel.death.event",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1990,
                },
            ),
            relationship_candidate(
                "cand:rel.taught-by.event",
                from_object_ref="authority:person-a",
                predicate="taught_by",
                to_object_ref="authority:person-b",
                evidence_note=json.dumps({"event_year": 1935}, sort_keys=True),
            ),
        ],
        run_id="gather-relational-event",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="relational-event.json", db_path=db_path)
        contradiction = conn.execute(
            "SELECT target_label, evidence_note FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()
    finally:
        conn.close()

    assert contradiction["target_label"] == "relational_temporal_event_year_outside_lifespan"
    assert "event year 1935 before birth year 1940" in contradiction["evidence_note"]


def test_relational_constraint_pass_is_idempotent(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:rel.birth.idempotent",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:rel.death.idempotent",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            relationship_candidate(
                "cand:rel.taught-by.idempotent",
                from_object_ref="authority:person-a",
                predicate="taught_by",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="gather-relational-idempotent",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="relational-idempotent.json", db_path=db_path)
        first_counts = {
            "contradictions": conn.execute(
                "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
            ).fetchone()[0],
            "history": conn.execute("SELECT COUNT(*) FROM review_state_history").fetchone()[0],
        }
        with conn:
            pass_report = canonical_reconciliation.run_relational_constraint_pass(
                conn,
                changed_at=FIXED_TIMESTAMP,
                source_run_id="manual-pass",
            )
        second_counts = {
            "contradictions": conn.execute(
                "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
            ).fetchone()[0],
            "history": conn.execute("SELECT COUNT(*) FROM review_state_history").fetchone()[0],
        }
    finally:
        conn.close()

    assert first_counts == second_counts
    assert pass_report["relational_contradictions_detected"] == 1


def test_quantity_conflict_creates_deterministic_contradiction(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:quantity.1",
                payload={
                    "claim_type": "quantity",
                    "about_object_ref": "work:example",
                    "value": 10,
                },
            ),
            structured_claim_candidate(
                "cand:quantity.2",
                payload={
                    "claim_type": "quantity",
                    "about_object_ref": "work:example",
                    "value": 12,
                },
            ),
        ],
        run_id="gather-quantity-conflict",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="quantity.json", db_path=db_path)
        contradiction_count = conn.execute(
            "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert contradiction_count == 1


def test_reconciliation_and_contradictions_are_idempotent_on_reingest(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:birth.idempotent",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:death.idempotent",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            structured_claim_candidate(
                "cand:taught-by.idempotent",
                payload={
                    "claim_type": "taught_by",
                    "about_object_ref": "authority:person-a",
                    "from_object_ref": "authority:person-a",
                    "to_object_ref": "authority:person-b",
                    "predicate": "taught_by",
                },
            ),
        ],
        run_id="gather-contradiction-idempotent",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="idempotent.json", db_path=db_path)
        first_counts = {
            "contradictions": conn.execute(
                "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
            ).fetchone()[0],
            "history": conn.execute("SELECT COUNT(*) FROM review_state_history").fetchone()[0],
        }
        with conn:
            report = ingest_batch(conn, batch, batch_name="idempotent.json", db_path=db_path)
        second_counts = {
            "contradictions": conn.execute(
                "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
            ).fetchone()[0],
            "history": conn.execute("SELECT COUNT(*) FROM review_state_history").fetchone()[0],
        }
    finally:
        conn.close()

    assert first_counts == second_counts
    assert report["counts"]["contradicted"]["source_relationship"] == 1


def test_freeform_prose_does_not_trigger_structured_contradiction_detection(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:birth.prose",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": 1940,
                },
            ),
            structured_claim_candidate(
                "cand:death.prose",
                payload={
                    "claim_type": "death_year",
                    "about_object_ref": "authority:person-b",
                    "year": 1938,
                },
            ),
            prose_claim_candidate(
                "cand:prose",
                "Person A was taught by Person B before Person A was born.",
            ),
        ],
        run_id="gather-prose-only",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            ingest_batch(conn, batch, batch_name="prose.json", db_path=db_path)
        contradiction_count = conn.execute(
            "SELECT COUNT(*) FROM source_relationship WHERE predicate='contradicts'"
        ).fetchone()[0]
        prose_claim = conn.execute(
            "SELECT review_state FROM source_claim WHERE claim_type='candidate_open_question'"
        ).fetchone()
    finally:
        conn.close()

    assert contradiction_count == 0
    assert prose_claim["review_state"] == "proposed"


def test_invalid_structured_year_rolls_back_ingest(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch(
        [
            structured_claim_candidate(
                "cand:invalid-year",
                payload={
                    "claim_type": "birth_year",
                    "about_object_ref": "authority:person-a",
                    "year": "nineteen-forty",
                },
            )
        ],
        run_id="gather-invalid-year",
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(
            canonical_ingest.CanonicalIngestError,
            match="candidate-batch reconciliation failed",
        ):
            with conn:
                ingest_batch(conn, batch, batch_name="invalid-year.json", db_path=db_path)
        counts = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert all(count == 0 for count in counts.values())
