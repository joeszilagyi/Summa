from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "tools" / "scripts" / "build_candidate_feedback_plan.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_candidate_feedback_plan.py"

spec = importlib.util.spec_from_file_location("candidate_feedback_validator_for_tests", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def create_feedback_db(tmp_path: Path) -> Path:
    db = tmp_path / "feedback.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            PRAGMA user_version=5;
            CREATE TABLE extraction_detected_entity (
              detected_entity_id INTEGER PRIMARY KEY,
              entity_label TEXT,
              entity_type TEXT,
              review_state TEXT,
              authority_record_id INTEGER,
              confidence_score REAL,
              record_last_updated TEXT,
              provenance_event_ref TEXT
            );
            CREATE TABLE authority_record (
              authority_record_id INTEGER PRIMARY KEY,
              preferred_label TEXT,
              authority_type TEXT,
              review_state TEXT,
              confidence_score REAL,
              record_last_updated TEXT,
              provenance_event_ref TEXT
            );
            CREATE TABLE source_relationship (
              source_relationship_id INTEGER PRIMARY KEY,
              from_object_ref TEXT,
              to_object_ref TEXT,
              predicate TEXT,
              review_state TEXT,
              confidence_score REAL,
              record_last_updated TEXT,
              provenance_event_ref TEXT,
              evidence_locator_ref TEXT
            );
            CREATE TABLE source_claim (
              source_claim_id INTEGER PRIMARY KEY,
              about_object_ref TEXT,
              claim_text TEXT,
              claim_type TEXT,
              review_state TEXT,
              confidence_score REAL,
              record_last_updated TEXT,
              provenance_event_ref TEXT,
              evidence_locator_ref TEXT
            );
            INSERT INTO extraction_detected_entity (
              detected_entity_id, entity_label, entity_type, review_state,
              authority_record_id, confidence_score, record_last_updated, provenance_event_ref
            ) VALUES
              (1, 'Alpha Subject', 'person', 'proposed', NULL, 0.41, '2026-06-01T09:00:00Z', 'prov:11111111-1111-1111-1111-111111111111'),
              (2, 'Beta Subject', 'person', 'proposed', NULL, 0.35, '2026-06-01T08:00:00Z', 'prov:22222222-2222-2222-2222-222222222222'),
              (3, 'Gamma Subject', 'person', 'proposed', NULL, 0.33, '2026-06-01T07:00:00Z', 'prov:33333333-3333-3333-3333-333333333333'),
              (4, 'Superseded Subject', 'person', 'proposed', NULL, 0.20, '2026-06-01T06:00:00Z', 'prov:44444444-4444-4444-4444-444444444444');
            INSERT INTO authority_record (
              authority_record_id, preferred_label, authority_type, review_state,
              confidence_score, record_last_updated, provenance_event_ref
            ) VALUES
              (10, 'Alpha Subject', 'person', 'reviewed', 0.96, '2026-06-02T10:00:00Z', 'prov:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'),
              (20, 'Beta Subject', 'person', 'reviewed', 0.84, '2026-06-02T09:00:00Z', 'prov:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'),
              (21, 'Beta Subject', 'person', 'reviewed', 0.82, '2026-06-02T09:05:00Z', 'prov:cccccccc-cccc-cccc-cccc-cccccccccccc'),
              (50, 'Superseded Subject', 'person', 'reviewed', 0.70, '2026-06-02T08:00:00Z', 'prov:dddddddd-dddd-dddd-dddd-dddddddddddd');
            INSERT INTO source_relationship (
              source_relationship_id, from_object_ref, to_object_ref, predicate,
              review_state, confidence_score, record_last_updated, provenance_event_ref, evidence_locator_ref
            ) VALUES
              (30, 'detected_entity:1', 'authority:10', 'same_as', 'reviewed', 0.88, '2026-06-02T11:00:00Z', 'prov:eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', 'evl:alpha.same_as');
            INSERT INTO source_claim (
              source_claim_id, about_object_ref, claim_text, claim_type,
              review_state, confidence_score, record_last_updated, provenance_event_ref, evidence_locator_ref
            ) VALUES
              (40, 'detected_entity:3', 'Gamma Subject', 'preferred_label', 'reviewed', 0.81, '2026-06-02T12:00:00Z', 'prov:ffffffff-ffff-ffff-ffff-ffffffffffff', 'evl:gamma.label');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db


def create_correction_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "correction_ledger.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "correction-ledger.v1",
                "workspace_id": "alpha_subject",
                "events": [
                    {
                        "event_id": "cle:detected-entity-supersede.1",
                        "action": "supersede",
                        "changed_at": "2026-06-02T13:00:00Z",
                        "changed_by": "pytest",
                        "rationale": "Fixture supersession",
                        "source_object_refs": ["detected_entity:4"],
                        "result_object_refs": ["authority:50"],
                        "review_queue_refs": ["detected_entity:4"],
                        "provenance_event_refs": ["prov:99999999-9999-9999-9999-999999999999"],
                        "evidence_locator_refs": [],
                        "field_review_entry_refs": [],
                        "note": None
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def create_field_review_state(tmp_path: Path) -> Path:
    path = tmp_path / "field_review_state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "field-review-state.v1",
                "record_locator": {
                    "workspace_id": "alpha_subject",
                    "record_family": "detected_entity",
                    "relative_path": "feedback/runtime.jsonl",
                    "source_filename": "runtime.jsonl",
                    "structured_format": "jsonl",
                    "record_locator": "line:1"
                },
                "field_reviews": [
                    {
                        "entry_id": "frs:gamma-label.1",
                        "field_path": "preferred_label",
                        "state": "disputed",
                        "reviewed_by": "pytest",
                        "reviewed_at": "2026-06-02T12:30:00Z",
                        "value_fingerprint": "sha256:00c6cab00c61badcfd555eb1b1e6da41e8b7a5877f8446e5b4c074d109f0f68c",
                        "evidence_ref": {
                            "evidence_type": "operator_note",
                            "reference": "pytest",
                            "excerpt_locator": None,
                            "evidence_note": "Disputed candidate label",
                            "evidence_locator_ref": "evl:gamma.label"
                        },
                        "supersedes_entry_id": None,
                        "demotes_entry_id": None,
                        "note": "Do not auto-promote this label without review.",
                        "tags": ["feedback-loop"]
                    }
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def create_evidence_locator(tmp_path: Path, *, evidence_id: str, summary: str) -> Path:
    path = tmp_path / f"{evidence_id.replace(':', '_')}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "evidence-locator.v1",
                "evidence_locator_id": evidence_id,
                "record_locator": {
                    "workspace_id": "alpha_subject",
                    "record_family": "source_lead",
                    "relative_path": "records/feedback.jsonl",
                    "source_filename": "feedback.jsonl",
                    "structured_format": "jsonl",
                    "record_locator": "line:1"
                },
                "span": {
                    "span_kind": "metadata_only",
                    "page_start": None,
                    "page_end": None,
                    "line_start": None,
                    "line_end": None,
                    "byte_start": None,
                    "byte_end": None,
                    "field_path": None,
                    "metadata_fields": ["summary"],
                    "locator_note": "Fixture evidence."
                },
                "highlight": {
                    "highlight_kind": "metadata_note",
                    "rights_posture": "redistributable",
                    "quote_eligibility": "eligible",
                    "redaction_posture": "public_summary_only",
                    "operator_excerpt_text": None,
                    "public_excerpt_text": None,
                    "public_summary": summary,
                    "highlight_note": "Fixture evidence note."
                }
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_candidate_feedback_plan_emits_ranked_append_only_proposals(tmp_path: Path) -> None:
    db = create_feedback_db(tmp_path)
    correction_ledger = create_correction_ledger(tmp_path)
    field_review_state = create_field_review_state(tmp_path)
    alpha_evidence = create_evidence_locator(tmp_path, evidence_id="evl:alpha.same_as", summary="Relationship evidence supports linking Alpha Subject.")
    gamma_evidence = create_evidence_locator(tmp_path, evidence_id="evl:gamma.label", summary="Later claim suggests Gamma Subject as the preferred label.")
    output_json = tmp_path / "candidate_feedback_plan.json"

    result = run_builder(
        "--db",
        str(db),
        "--correction-ledger",
        str(correction_ledger),
        "--field-review-state",
        str(field_review_state),
        "--evidence-locator",
        str(alpha_evidence),
        "--evidence-locator",
        str(gamma_evidence),
        "--output-json",
        str(output_json),
        "--generated-at",
        "2026-06-02T15:00:00Z",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report, exit_code = validator.validate_candidate_feedback_plan(output_json)
    assert exit_code == validator.EXIT_PASS, report

    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "candidate-feedback-plan.v1"
    assert payload["source"] == {
        "database_name": "feedback.sqlite",
        "schema_version": 5,
        "correction_ledger_applied": True,
        "field_review_state_count": 1,
        "evidence_locator_count": 2,
        "dry_run": True,
    }
    assert payload["counts"] == {
        "earlier_candidates_considered": 4,
        "later_discoveries_considered": 6,
        "proposals_emitted": 4,
        "skipped_targets": 2,
    }
    assert [proposal["proposal_kind"] for proposal in payload["proposals"]] == [
        "update_candidate",
        "relationship_candidate",
        "review_task",
        "review_task",
    ]
    assert [proposal["rank"] for proposal in payload["proposals"]] == [1, 2, 3, 4]
    assert [proposal["target_object_ref"] for proposal in payload["proposals"]] == [
        "detected_entity:1",
        "detected_entity:1",
        "detected_entity:3",
        "detected_entity:2",
    ]
    top = payload["proposals"][0]
    assert top["append_only_target"] == "field_review_state"
    assert top["source_object_refs"] == ["authority:10"]
    assert top["proposed_changes"]["proposed_value"] == "authority:10"
    relationship = payload["proposals"][1]
    assert relationship["proposal_kind"] == "relationship_candidate"
    assert relationship["evidence_locator_refs"] == ["evl:alpha.same_as"]
    assert relationship["evidence_summaries"] == ["Relationship evidence supports linking Alpha Subject.", "Fixture evidence note."]
    disputed = payload["proposals"][2]
    assert disputed["proposal_kind"] == "review_task"
    assert disputed["evidence_locator_refs"] == ["evl:gamma.label"]
    assert disputed["proposed_changes"]["review_task_reason"] == "disputed_field_value"
    ambiguous = payload["proposals"][3]
    assert ambiguous["proposal_kind"] == "review_task"
    assert ambiguous["source_object_refs"] == ["authority:20", "authority:21"]
    assert ambiguous["proposed_changes"]["review_task_reason"] == "ambiguous_authority_match"
    assert {"target_object_ref": "detected_entity:4", "reason": "superseded_by_correction_ledger"} in payload["skipped"]
    assert {"target_object_ref": "detected_entity:3", "reason": "no_later_reviewed_authority_match"} in payload["skipped"]


def test_candidate_feedback_plan_text_output_is_stable(tmp_path: Path) -> None:
    db = create_feedback_db(tmp_path)
    correction_ledger = create_correction_ledger(tmp_path)
    field_review_state = create_field_review_state(tmp_path)
    alpha_evidence = create_evidence_locator(tmp_path, evidence_id="evl:alpha.same_as", summary="Relationship evidence supports linking Alpha Subject.")
    gamma_evidence = create_evidence_locator(tmp_path, evidence_id="evl:gamma.label", summary="Later claim suggests Gamma Subject as the preferred label.")

    result = run_builder(
        "--db",
        str(db),
        "--correction-ledger",
        str(correction_ledger),
        "--field-review-state",
        str(field_review_state),
        "--evidence-locator",
        str(alpha_evidence),
        "--evidence-locator",
        str(gamma_evidence),
        "--generated-at",
        "2026-06-02T15:00:00Z",
        "--format",
        "text",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_version=candidate-feedback-plan.v1" in result.stdout
    assert "proposal[0]=update_candidate target=detected_entity:1" in result.stdout
