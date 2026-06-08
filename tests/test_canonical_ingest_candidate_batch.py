from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_canonical_dedup_and_contradiction import (
    build_batch,
    relationship_candidate,
    source_lead_candidate,
    structured_claim_candidate,
)
from tools.source_db_tools import canonical_ingest, canonical_reconciliation, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_BATCH = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
FIXTURE_PROMPT = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "rendered-prompt.txt"
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


class ProvenanceLookupCountingProxy:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.provenance_lookup_count = 0
        self.prior_state_like_count = 0
        self.prior_state_count_query_count = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)

    def __enter__(self) -> ProvenanceLookupCountingProxy:
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object:
        return self._conn.__exit__(exc_type, exc, tb)

    def execute(self, sql: str, params: object = ()) -> object:
        if isinstance(sql, str):
            normalized_sql = " ".join(sql.split()).upper()
            if normalized_sql.startswith("SELECT PROVENANCE_EVENT_ID FROM PROVENANCE_EVENT"):
                self.provenance_lookup_count += 1
            if normalized_sql.startswith("SELECT COUNT(*) AS COUNT"):
                self.prior_state_count_query_count += 1
            if "NOTE_TEXT LIKE" in normalized_sql:
                self.prior_state_like_count += 1
        return self._conn.execute(sql, params)


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


def test_candidate_batch_ingest_resolves_provenance_event_once(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    batch, batch_hash = load_fixture_batch()
    conn = canonical_store.connect_canonical_store(db_path)
    proxy = ProvenanceLookupCountingProxy(conn)
    try:
        with proxy:
            report = canonical_ingest.ingest_candidate_batch(
                proxy,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert proxy.provenance_lookup_count == 1


def test_candidate_batch_ingest_passes_incremental_reconciliation_work_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    payload = build_batch(
        [
            structured_claim_candidate(
                "cand:claim.incremental",
                payload={
                    "claim_type": "quantity",
                    "about_object_ref": "work:incremental",
                    "value": 10,
                },
            ),
            relationship_candidate(
                "cand:relationship.incremental",
                from_object_ref="authority:person-a",
                predicate="taught_by",
                to_object_ref="authority:person-b",
            ),
        ],
        run_id="incremental-reconciliation",
    )
    captured: dict[str, object] = {}

    def fake_run_reconciliation_pass_for_ingest(
        conn: sqlite3.Connection, **kwargs: object
    ) -> dict[str, int]:
        assert conn is not None
        captured.update(kwargs)
        return {
            "work_deduped": 0,
            "authority_reconciled": 0,
            "authority_merged": 0,
            "claims_contradicted": 0,
            "relationships_contradicted": 0,
            "relational_constraints_checked": 0,
            "relational_constraints_skipped": 0,
        }

    monkeypatch.setattr(
        canonical_reconciliation,
        "run_reconciliation_pass_for_ingest",
        fake_run_reconciliation_pass_for_ingest,
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                payload,
                batch_path=tmp_path / "batch.json",
                batch_hash=batch_hash(payload),
                db_path=db_path,
            )
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert captured["provenance_event_ref"] == report["provenance_event"]["event_key"]
    assert captured["source_run_id"] == "incremental-reconciliation"
    assert captured["claim_work_items"] == [("fixture_subject", "work:incremental", "quantity")]
    assert captured["relationship_work_items"] == [
        ("fixture_subject", "authority:person-a", "taught_by", "authority:person-b")
    ]


def test_candidate_batch_ingest_skips_reconciliation_when_no_work_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    payload = build_batch(
        [
            source_lead_candidate(
                "cand:lead.only",
                original_locator="https://example.test/source.pdf",
                canonical_url="https://example.test/source.pdf",
                source_lead_id="lead:only",
            )
        ],
        run_id="source-lead-only",
    )
    called = False

    def fake_run_reconciliation_pass_for_ingest(
        conn: sqlite3.Connection, **kwargs: object
    ) -> dict[str, int]:
        assert conn is not None
        nonlocal called
        called = True
        return {
            "work_deduped": 0,
            "authority_reconciled": 0,
            "authority_merged": 0,
            "claims_contradicted": 0,
            "relationships_contradicted": 0,
            "relational_constraints_checked": 0,
            "relational_constraints_skipped": 0,
        }

    monkeypatch.setattr(
        canonical_reconciliation,
        "run_reconciliation_pass_for_ingest",
        fake_run_reconciliation_pass_for_ingest,
    )

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                payload,
                batch_path=tmp_path / "source-lead-only.json",
                batch_hash=batch_hash(payload),
                db_path=db_path,
            )
    finally:
        conn.close()

    assert report["status"] == "completed"
    assert called is False
    assert report["transaction_status"] == "committed"
    assert report["counts"]["reconciled"] == {}
    assert report["counts"]["contradicted"] == {}
    assert report["counts"]["deduped"] == {}


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


@pytest.mark.parametrize(
    "candidate_type",
    ["raw_candidate_text", "open_question", "timeline_item"],
)
def test_prose_only_candidate_claim_text_is_bounded(
    tmp_path: Path, candidate_type: str
) -> None:
    db_path = bootstrap_db(tmp_path)
    prose = (
        "Line one should be retained as the bounded claim text. "
        "This remainder should not be stored verbatim in source_claim.claim_text.\n"
        + ("additional prose " * 40)
    )
    batch = build_batch(
        [
            {
                "candidate_id": f"cand:{candidate_type}.1",
                "candidate_type": candidate_type,
                "origin": "llm_proposed",
                "persistence_status": "workspace_run_only",
                "review_status": "unverified",
                "text": prose,
            }
        ],
        run_id=f"bounded-{candidate_type}",
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=tmp_path / f"{candidate_type}.json",
                batch_hash=batch_hash(batch),
                db_path=db_path,
            )
        claim_row = conn.execute(
            "SELECT claim_text, claim_type FROM source_claim ORDER BY source_claim_id"
        ).fetchone()
    finally:
        conn.close()

    expected_claim_text = " ".join(prose.splitlines()[0].split())[:240] or "claim-fallback-empty"
    assert report["counts"]["inserted"]["source_claim"] == 1
    assert claim_row["claim_type"] == f"candidate_{candidate_type}"
    assert claim_row["claim_text"] == expected_claim_text
    assert claim_row["claim_text"] != prose


def test_candidate_ingested_high_confidence_entity_is_visible_to_prior_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    proxy = ProvenanceLookupCountingProxy(conn)
    try:
        with proxy:
            canonical_ingest.ingest_candidate_batch(
                proxy,
                tuned_batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        proxy.prior_state_count_query_count = 0
        proxy.prior_state_like_count = 0
        monkeypatch.setattr(canonical_store, "validate_existing_store", lambda *args, **kwargs: None)
        prior_state = canonical_store.load_gather_prior_state(proxy, subject_id=subject_id)
    finally:
        conn.close()

    assert prior_state["record_counts"]["entities"]["total"] >= 1
    assert prior_state["record_counts"]["entities"]["selected"] >= 1
    assert {
        record["entity_label"] for record in prior_state["records"]["entities"]
    } >= {"Alpha Example"}
    assert proxy.prior_state_like_count == 0
    assert proxy.prior_state_count_query_count == 0


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
        with pytest.raises(
            canonical_ingest.CanonicalIngestError, match="synthetic source access failure"
        ), conn:
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


def test_load_validated_candidate_batch_streams_without_read_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_payload = json.loads(FIXTURE_BATCH.read_text(encoding="utf-8"))
    valid_payload["prompt"]["rendered_prompt"] = FIXTURE_PROMPT.read_text(encoding="utf-8")
    valid_payload["prompt"]["rendered_prompt_hash"] = hashlib.sha256(
        FIXTURE_PROMPT.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    valid_payload["run_id"] = "run-id-valid"
    batch_path = tmp_path / "gather-candidate-batch.json"
    local_prompt_path = batch_path.parent / "rendered-prompt.txt"
    local_prompt_path.write_text(FIXTURE_PROMPT.read_text(encoding="utf-8"), encoding="utf-8")
    valid_payload["prompt"]["rendered_prompt_path"] = "rendered-prompt.txt"

    valid_text = json.dumps(valid_payload, ensure_ascii=False, sort_keys=True) + "\n"
    batch_path.write_text(valid_text, encoding="utf-8")

    original_read_text = canonical_ingest.Path.read_text

    def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == batch_path:
            raise AssertionError("candidate batch loading should stream via Path.open()")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(canonical_ingest.Path, "read_text", fail_read_text)

    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)

    assert batch["run_id"] == "run-id-valid"
    assert batch_hash == hashlib.sha256(valid_text.encode("utf-8")).hexdigest()


def test_candidate_structured_payload_rejects_duplicate_json_keys() -> None:
    candidate = {
        "text": '{"candidate_id":"w1","candidate_type":"work","text":"body","from_object_ref":"a","predicate":"b","candidate_id":"w2"}'
    }

    assert canonical_ingest._candidate_structured_payload(candidate) is None


@pytest.mark.parametrize(
    "candidate_type",
    ["raw_candidate_text", "open_question", "timeline_item"],
)
def test_candidate_structured_payload_skips_raw_candidate_text_prose(
    candidate_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = {
        "candidate_type": candidate_type,
        "text": "This is plain prose, not JSON.",
    }

    def fail_json_loads(*args: object, **kwargs: object) -> object:
        raise AssertionError("raw candidate prose should not be parsed as JSON")

    monkeypatch.setattr(canonical_ingest.json, "loads", fail_json_loads)

    assert canonical_ingest._candidate_structured_payload(candidate) is None


def test_candidate_structured_payload_rejects_non_standard_json_constants() -> None:
    candidate = {
        "text": '{"candidate_id":"w1","candidate_type":"work","value": NaN}'
    }

    assert canonical_ingest._candidate_structured_payload(candidate) is None


def test_safe_json_text_rejects_non_standard_numbers() -> None:
    candidate = {"candidate_type": "work", "value": float("nan")}

    with pytest.raises(ValueError, match="Out of range"):
        canonical_ingest._safe_json_text(candidate)
