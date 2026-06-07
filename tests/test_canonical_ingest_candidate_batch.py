from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.source_db_tools import canonical_ingest, canonical_store


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_BATCH = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
EXPORT_SCRIPT = REPO_ROOT / "tools" / "scripts" / "build_knowledge_tree_export.py"
FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.candidate_ingest",
    )
    return db_path


def load_fixture_batch() -> tuple[dict[str, object], str]:
    return canonical_ingest.load_validated_candidate_batch(FIXTURE_BATCH)


def batch_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_batch_payload() -> dict[str, object]:
    return {
        "schema_version": "gather-candidate-batch.v1",
        "run_id": "relationship-confidence",
        "created_at": FIXED_TIMESTAMP,
        "candidates": [
            {
                "candidate_id": "cand:entity-a",
                "candidate_type": "person",
                "origin": "llm_proposed",
                "persistence_status": "workspace_run_only",
                "review_status": "unverified",
                "text": json.dumps(
                    {
                        "from_object_ref": "authority:person-a",
                        "to_object_ref": "authority:person-b",
                        "predicate": "mentions",
                        "target_label": "Person B",
                        "evidence_note": "Derived from test candidate.",
                        "review_state": "accepted",
                        "confidence_score": 0.93,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        ],
    }


def test_candidate_batch_ingest_writes_reviewable_rows_and_provenance(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        counts = canonical_store.canonical_family_counts(conn)
        work_states = {
            row["review_state"]
            for row in conn.execute("SELECT review_state FROM work").fetchall()
        }
        claim_states = {
            row["review_state"]
            for row in conn.execute("SELECT review_state FROM source_claim").fetchall()
        }
        entity_states = {
            row["review_state"]
            for row in conn.execute("SELECT review_state FROM extraction_detected_entity").fetchall()
        }
        relationship_states = {
            row["review_state"]
            for row in conn.execute("SELECT review_state FROM source_relationship").fetchall()
        }
        provenance_refs = {
            row[0]
            for row in conn.execute(
                """
                SELECT provenance_event_ref FROM work
                UNION
                SELECT provenance_event_ref FROM source_claim
                UNION
                SELECT provenance_event_ref FROM extraction_detected_entity
                UNION
                SELECT provenance_event_ref FROM source_relationship
                """
            ).fetchall()
        }
        weird_claim = conn.execute(
            "SELECT claim_text, review_state FROM source_claim WHERE claim_text LIKE '%before Alpha Example was born%'"
        ).fetchone()
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert counts["provenance_event"] == 1
    assert counts["work"] >= 1
    assert counts["source_access"] >= 2
    assert counts["source_claim"] >= 2
    assert counts["extraction_detected_entity"] >= 1
    assert counts["source_relationship"] >= 1
    assert work_states == {"needs_review"}
    assert claim_states == {"proposed"}
    assert entity_states == {"proposed"}
    assert relationship_states == {"proposed"}
    assert provenance_refs == {report["provenance_event"]["event_key"]}
    assert weird_claim["review_state"] == "proposed"

    output_path = tmp_path / "knowledge_tree_export.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(EXPORT_SCRIPT),
            "--db",
            str(db_path),
            "--output",
            str(output_path),
            "--generated-at",
            FIXED_TIMESTAMP,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert output_path.is_file()


def test_candidate_batch_validation_happens_before_write(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    invalid_path = tmp_path / "invalid-gather-candidate-batch.json"
    payload = json.loads(FIXTURE_BATCH.read_text(encoding="utf-8"))
    payload["prompt"]["rendered_prompt_hash"] = "0" * 64
    invalid_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(canonical_ingest.CanonicalIngestError, match="validation failed"):
        canonical_ingest.load_validated_candidate_batch(invalid_path)

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        assert canonical_store.canonical_family_counts(conn) == {
            "provenance_event": 0,
            "work": 0,
            "source_access": 0,
            "source_claim": 0,
            "capture_event": 0,
            "extraction_record": 0,
            "extraction_detected_entity": 0,
            "source_relationship": 0,
        }
    finally:
        conn.close()


def test_candidate_batch_ingest_is_idempotent_for_same_fixture(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            first = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        counts_after_first = canonical_store.canonical_family_counts(conn)
        with conn:
            second = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        counts_after_second = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert counts_after_first == counts_after_second
    assert first["counts"]["inserted"]["work"] >= 1
    assert second["counts"]["updated"]["work"] >= 1
    assert second["counts"]["updated"]["source_claim"] >= 1


def test_candidate_batch_unknown_candidate_is_preserved_with_warning(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        warning_messages = [warning["message"] for warning in report["warnings"]]
    finally:
        conn.close()

    assert "unknown candidate type preserved as a source claim" in warning_messages


def test_candidate_ingested_high_confidence_entity_is_visible_to_prior_state(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    subject_id = str(batch["subject"]["subject_id"])
    tuned_candidates: list[dict[str, object]] = []
    for candidate in batch["candidates"]:
        candidate_copy = dict(candidate)
        if candidate_copy.get("candidate_type") == "person":
            structured = json.loads(str(candidate_copy["text"]))
            structured["confidence_score"] = 0.91
            candidate_copy["text"] = json.dumps(
                structured, ensure_ascii=False, separators=(",", ":")
            )
        tuned_candidates.append(candidate_copy)
    tuned_batch = dict(batch)
    tuned_batch["candidates"] = tuned_candidates

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                tuned_batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        prior_state = canonical_store.load_gather_prior_state(conn, subject_id=subject_id)
    finally:
        conn.close()

    assert prior_state["record_counts"]["entities"]["total"] >= 1
    assert prior_state["record_counts"]["entities"]["selected"] >= 1
    assert {
        record["entity_label"] for record in prior_state["records"]["entities"]
    } >= {"Alpha Example"}


def test_candidate_batch_dry_run_reports_intended_writes_without_mutation(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        report = canonical_ingest.ingest_candidate_batch(
            conn,
            batch,
            batch_path=FIXTURE_BATCH,
            batch_hash=batch_hash,
            dry_run=True,
            db_path=db_path,
        )
        counts = canonical_store.canonical_family_counts(conn)
    finally:
        conn.close()

    assert report["status"] == "dry_run"
    assert report["counts"]["intended"]["work"] >= 1
    assert report["counts"]["intended"]["source_claim"] >= 1
    assert report["provenance_event"] is None
    assert all(count == 0 for count in counts.values())


def test_candidate_batch_ingest_rolls_back_on_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    conn = canonical_store.connect_canonical_store(db_path)
    original_record_source_access = canonical_store.record_source_access

    def fail_after_first_access(*args: object, **kwargs: object) -> canonical_store.CanonicalWriteResult:
        raise canonical_ingest.CanonicalIngestError("synthetic source access failure")

    monkeypatch.setattr(canonical_store, "record_source_access", fail_after_first_access)
    try:
        with pytest.raises(canonical_ingest.CanonicalIngestError, match="synthetic source access failure"):
            with conn:
                canonical_ingest.ingest_candidate_batch(
                    conn,
                    batch,
                    batch_path=FIXTURE_BATCH,
                    batch_hash=batch_hash,
                    db_path=db_path,
                )
        counts = canonical_store.canonical_family_counts(conn)
    finally:
        monkeypatch.setattr(canonical_store, "record_source_access", original_record_source_access)
        conn.close()

    assert all(count == 0 for count in counts.values())


def test_candidate_batch_relationship_keeps_confidence_and_review_state(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_batch_payload()
    batch_path = tmp_path / "relationship-batch.json"
    batch_path.write_text(json.dumps(batch, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=batch_path,
                batch_hash=batch_hash(batch),
                db_path=db_path,
            )
        relationship = conn.execute(
            "SELECT review_state, confidence_score FROM source_relationship"
        ).fetchone()
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert relationship["review_state"] == "accepted"
    assert relationship["confidence_score"] == pytest.approx(0.93)


def build_work_batch_payload() -> dict[str, object]:
    return {
        "schema_version": "gather-candidate-batch.v1",
        "run_id": "work-identifier-confidence",
        "created_at": FIXED_TIMESTAMP,
        "candidates": [
            {
                "candidate_id": "cand:work.1",
                "candidate_type": "work",
                "origin": "llm_proposed",
                "persistence_status": "workspace_run_only",
                "review_status": "unverified",
                "text": json.dumps(
                    {
                        "work_key": "work.example.1",
                        "title": "Work Without Identifier Confidence",
                        "work_type": "article",
                        "identifier_scheme": "doi",
                        "identifier_value": "10.1000/xyz",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        ],
    }


def test_work_identifier_does_not_default_to_high_confidence(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch = build_work_batch_payload()
    batch_path = tmp_path / "work-confidence-batch.json"
    batch_path.write_text(json.dumps(batch, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=batch_path,
                batch_hash=batch_hash(batch),
                db_path=db_path,
            )
        identifier = conn.execute("SELECT confidence_score FROM work_identifier").fetchone()
    finally:
        conn.close()

    assert identifier is not None
    assert identifier["confidence_score"] is None


def test_load_validated_candidate_batch_uses_single_candidate_payload_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    valid_payload = json.loads(FIXTURE_BATCH.read_text(encoding="utf-8"))
    valid_payload["run_id"] = "run-id-valid"
    valid_text = json.dumps(valid_payload, ensure_ascii=False, sort_keys=True) + "\n"

    mutated_payload = json.loads(valid_text)
    mutated_payload["run_id"] = "run-id-mutated"
    mutated_text = json.dumps(mutated_payload, ensure_ascii=False, sort_keys=True) + "\n"

    batch_path = tmp_path / "gather-candidate-batch.json"
    batch_path.write_text(valid_text, encoding="utf-8")

    original_read_text = canonical_ingest.Path.read_text
    calls = {"count": 0}

    def read_text_side_effect(self: Path, *args: object, **kwargs: object) -> str:
        if self == batch_path:
            calls["count"] += 1
            if calls["count"] == 1:
                return valid_text
            return mutated_text
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(canonical_ingest.Path, "read_text", read_text_side_effect)

    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)

    assert batch["run_id"] == "run-id-valid"
    assert batch_hash == hashlib.sha256(valid_text.encode("utf-8")).hexdigest()
