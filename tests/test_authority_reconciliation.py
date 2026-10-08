from __future__ import annotations

import pytest

from tools.source_db_tools import (
    authority_reconciliation,
    canonical_reconciliation,
    canonical_store,
)

FIXED_TIMESTAMP = "2026-06-05T10:20:30Z"


def bootstrap_db(tmp_path):
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.authority_reconciliation",
    )
    return canonical_store.connect_canonical_store(db_path)


def test_local_authority_creation_records_stable_provenance(tmp_path) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        authority_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="local-authority-provenance",
            created_at=FIXED_TIMESTAMP,
        )
        replay_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="local-authority-provenance",
            created_at="2026-06-06T10:20:30Z",
        )
        row = conn.execute(
            "SELECT provenance_event_ref FROM authority_record WHERE authority_record_id=?",
            (authority_id,),
        ).fetchone()
        events = conn.execute(
            """
            SELECT provenance_event_key_v1, object_namespace, event_type,
                   source_object_namespace, source_object_id
            FROM provenance_event
            WHERE object_namespace='authority_record'
            """
        ).fetchall()
    finally:
        conn.close()

    assert replay_id == authority_id
    assert len(events) == 1
    assert row["provenance_event_ref"] == events[0]["provenance_event_key_v1"]
    assert events[0]["event_type"] == "local_authority_creation"
    assert events[0]["source_object_namespace"] == "pytest"
    assert events[0]["source_object_id"] == "local-authority-provenance"


def test_readding_authority_identifier_cannot_demote_primary_status(tmp_path) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        authority_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="primary-identifier-replay",
            created_at=FIXED_TIMESTAMP,
        )
        first_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=authority_id,
            scheme="orcid",
            value="0000-0002-1825-0097",
            is_primary=1,
            review_state="accepted",
            verified_at=FIXED_TIMESTAMP,
        )
        replay_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=authority_id,
            scheme="orcid",
            value="0000-0002-1825-0097",
            review_state="accepted",
            verified_at="2026-06-06T10:20:30Z",
        )
        primary_flag = conn.execute(
            "SELECT is_primary FROM authority_identifier WHERE authority_identifier_id=?",
            (first_id,),
        ).fetchone()[0]
        secondary_authority_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="John Smith",
            source_namespace="pytest",
            source_id="secondary-identifier-promotion",
            created_at=FIXED_TIMESTAMP,
        )
        secondary_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=secondary_authority_id,
            scheme="local",
            value="secondary-id",
            is_primary=0,
            review_state="accepted",
            verified_at=FIXED_TIMESTAMP,
        )
        promoted_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=secondary_authority_id,
            scheme="local",
            value="secondary-id",
            is_primary=1,
            review_state="accepted",
            verified_at="2026-06-06T10:20:30Z",
        )
        promoted_flag = conn.execute(
            "SELECT is_primary FROM authority_identifier WHERE authority_identifier_id=?",
            (secondary_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert replay_id == first_id
    assert primary_flag == 1
    assert promoted_id == secondary_id
    assert promoted_flag == 1


@pytest.mark.parametrize(
    ("initial_state", "replay_state", "expected_state", "expected_score"),
    [
        ("accepted", "proposed", "accepted", 0.95),
        ("accepted", "accepted", "accepted", 0.95),
        ("curated", "needs_review", "curated", 0.95),
        ("proposed", "accepted", "accepted", 0.10),
    ],
)
def test_local_authority_replay_preserves_reviewed_state_and_score(
    tmp_path,
    initial_state: str,
    replay_state: str,
    expected_state: str,
    expected_score: float,
) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        first_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="local-authority-replay",
            review_state=initial_state,
            confidence_score=0.95,
            created_at=FIXED_TIMESTAMP,
        )
        replay_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="local-authority-replay",
            review_state=replay_state,
            confidence_score=0.10,
            created_at="2026-06-06T10:20:30Z",
        )
        row = conn.execute(
            "SELECT review_state, confidence_score FROM authority_record "
            "WHERE authority_record_id=?",
            (first_id,),
        ).fetchone()
    finally:
        conn.close()

    assert replay_id == first_id
    assert row["review_state"] == expected_state
    assert row["confidence_score"] == expected_score


@pytest.mark.parametrize(
    ("initial_state", "replay_state", "expected_state", "expected_score"),
    [
        ("accepted", "proposed", "accepted", 0.95),
        ("accepted", "accepted", "accepted", 0.95),
        ("curated", "needs_review", "curated", 0.95),
        ("proposed", "accepted", "accepted", 0.10),
    ],
)
def test_identifier_replay_preserves_reviewed_state_and_score(
    tmp_path,
    initial_state: str,
    replay_state: str,
    expected_state: str,
    expected_score: float,
) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        authority_id = authority_reconciliation.create_local_authority(
            conn,
            authority_type="person",
            preferred_label="Jane Smith",
            source_namespace="pytest",
            source_id="identifier-review-replay",
            created_at=FIXED_TIMESTAMP,
        )
        first_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=authority_id,
            scheme="orcid",
            value="0000-0002-1825-0097",
            confidence_score=0.95,
            review_state=initial_state,
            verified_at=FIXED_TIMESTAMP,
        )
        replay_id = authority_reconciliation.add_authority_identifier(
            conn,
            authority_record_id=authority_id,
            scheme="orcid",
            value="0000-0002-1825-0097",
            confidence_score=0.10,
            review_state=replay_state,
            verified_at="2026-06-06T10:20:30Z",
        )
        row = conn.execute(
            "SELECT review_state, confidence_score FROM authority_identifier "
            "WHERE authority_identifier_id=?",
            (first_id,),
        ).fetchone()
    finally:
        conn.close()

    assert replay_id == first_id
    assert row["review_state"] == expected_state
    assert row["confidence_score"] == expected_score


@pytest.mark.parametrize(
    "initial_state",
    ["machine_extracted", "needs_review", "proposed", "recorded", "unreviewed"],
)
def test_accept_candidate_promotes_pending_authority_linked_entity_state(
    tmp_path, initial_state: str
) -> None:
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
def test_accept_candidate_preserves_terminal_entity_review_state(
    tmp_path, initial_state: str
) -> None:
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


@pytest.mark.parametrize(
    ("existing_state", "replay_state"),
    [
        ("accepted", "needs_review"),
        ("accepted", "accepted"),
        ("rejected", "needs_review"),
        ("curated", "needs_review"),
    ],
)
def test_record_authority_reconciliation_preserves_established_review_state_on_replay(
    tmp_path,
    existing_state: str,
    replay_state: str,
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
                review_state=replay_state,
                created_at="2026-06-05T10:21:30Z",
            )
            row = conn.execute(
                """
                SELECT review_state, confidence_score, match_score, evidence_context
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
    assert row["confidence_score"] == 0.99
    assert row["match_score"] == 0.99
    assert row["evidence_context"] == "before"


def test_reconciliation_separates_match_methods_without_duplicating_legacy_replay(
    tmp_path,
) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        with conn:
            provenance = canonical_store.record_provenance_event(
                conn,
                object_namespace="authority-reconciliation-tests",
                object_id="match-method-identity",
                event_type="authority_reconciliation",
                tool_name="pytest",
                event_timestamp=FIXED_TIMESTAMP,
                provenance_event_key_v1="prov:match-method-identity",
            )
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="match-method-identity",
                created_at=FIXED_TIMESTAMP,
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=provenance.event_key,
                entity_label="Jane Smith",
                entity_type="person",
                record_last_updated=FIXED_TIMESTAMP,
            )

            def record(match_method: str):
                return canonical_reconciliation.record_authority_reconciliation(
                    conn,
                    detected_entity_id=entity.row_id,
                    raw_label="Jane Smith",
                    entity_type="person",
                    candidate_authority_record_id=authority_id,
                    method="candidate_match",
                    match_method=match_method,
                    confidence_score=0.8,
                    evidence_context=f"Evidence for {match_method}",
                    review_state="proposed",
                    created_at=FIXED_TIMESTAMP,
                )

            baseline = record("exact_name")
            legacy_key = canonical_store.stable_write_key(
                "authrec", entity.row_id, authority_id, "candidate_match"
            )
            conn.execute(
                "UPDATE authority_reconciliation SET reconciliation_key_v1=? "
                "WHERE authority_reconciliation_id=?",
                (legacy_key, baseline.row_id),
            )
            legacy_replay = record("exact_name")
            different_method = record("authority_identifier")
            different_replay = record("authority_identifier")
            rows = conn.execute(
                "SELECT reconciliation_key_v1, match_method FROM authority_reconciliation "
                "ORDER BY authority_reconciliation_id"
            ).fetchall()
    finally:
        conn.close()

    assert baseline.created is True
    assert legacy_replay.created is False
    assert legacy_replay.row_id == baseline.row_id
    assert legacy_replay.key == legacy_key
    assert different_method.created is True
    assert different_method.row_id != baseline.row_id
    assert different_replay.created is False
    assert different_replay.row_id == different_method.row_id
    assert [(row["reconciliation_key_v1"], row["match_method"]) for row in rows] == [
        (legacy_key, "exact_name"),
        (different_method.key, "authority_identifier"),
    ]


def test_record_authority_merge_event_is_idempotent_without_rewriting_timestamp(
    tmp_path,
) -> None:
    conn = bootstrap_db(tmp_path)
    try:
        with conn:
            winner_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith Winner",
                source_namespace="pytest",
                source_id="jane-smith-winner",
                review_state="accepted",
                confidence_score=1.0,
                created_at=FIXED_TIMESTAMP,
            )
            loser_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith Loser",
                source_namespace="pytest",
                source_id="jane-smith-loser",
                review_state="accepted",
                confidence_score=1.0,
                created_at=FIXED_TIMESTAMP,
            )
            first = canonical_reconciliation.record_authority_merge_event(
                conn,
                from_authority_record_id=loser_id,
                into_authority_record_id=winner_id,
                merge_reason="review_decision_accept_merge",
                evidence_note="first merge pass",
                merged_by="operator",
                merged_at=FIXED_TIMESTAMP,
            )
            second = canonical_reconciliation.record_authority_merge_event(
                conn,
                from_authority_record_id=loser_id,
                into_authority_record_id=winner_id,
                merge_reason="review_decision_accept_merge",
                evidence_note="replayed merge pass",
                merged_by="operator",
                merged_at="2026-06-07T00:00:00Z",
            )
            loser = conn.execute(
                """
                SELECT merged_into_authority_record_id, reconciliation_status, record_last_updated
                FROM authority_record
                WHERE authority_record_id=?
                """,
                (loser_id,),
            ).fetchone()
            merge_row = conn.execute(
                """
                SELECT merge_reason, evidence_note, merged_at, merged_by
                FROM authority_merge_event
                WHERE from_authority_record_id=? AND into_authority_record_id=?
                """,
                (loser_id, winner_id),
            ).fetchone()
            merge_count = conn.execute("SELECT COUNT(*) FROM authority_merge_event").fetchone()[0]
    finally:
        conn.close()

    assert first.created is True
    assert second.created is False
    assert first.row_id == second.row_id
    assert loser["merged_into_authority_record_id"] == winner_id
    assert loser["reconciliation_status"] == "merged"
    assert loser["record_last_updated"] == FIXED_TIMESTAMP
    assert merge_count == 1
    assert merge_row["merge_reason"] == "review_decision_accept_merge"
    assert merge_row["evidence_note"] == "first merge pass"
    assert merge_row["merged_at"] == FIXED_TIMESTAMP
    assert merge_row["merged_by"] == "operator"
