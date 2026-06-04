from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.common import standards_profiles
from tools.source_db_tools import authority_reconciliation, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXED_TIMESTAMP = "2026-06-04T09:00:00Z"
PRIVATE_SENTINEL = "PRIVATE_SENTINEL_DO_NOT_EXPORT"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.standards_profiles",
    )
    return db_path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(conn, suffix: str) -> canonical_store.ProvenanceEventRef:
    return canonical_store.record_provenance_event(
        conn,
        object_namespace="standards_fixture",
        object_id=suffix,
        event_type="standards_fixture",
        actor_type="tool",
        actor_id="pytest",
        tool_name="tests/test_standards_profile_crosswalks.py",
        run_id=f"run-{suffix}",
        event_timestamp=FIXED_TIMESTAMP,
        note_text=f"fixture event {suffix}",
        provenance_event_key_v1=f"prov:standards:{suffix}",
    )


def build_fixture_store(tmp_path: Path) -> dict[str, int | Path]:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            prov = provenance(conn, "work")
            work = canonical_store.upsert_work(
                conn,
                work_key_v1="work:standards:public",
                provenance_event_ref=prov.event_key,
                work_type="web_page",
                title="Public standards fixture work",
                rights_posture="metadata only",
                refetchability_status="refetchable",
                review_state="accepted",
                publication_state="public_safe",
                workspace_id="standards_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            private_work = canonical_store.upsert_work(
                conn,
                work_key_v1="work:standards:private",
                provenance_event_ref=prov.event_key,
                work_type="web_page",
                title=f"Private {PRIVATE_SENTINEL}",
                review_state="accepted",
                publication_state="private_working",
                public_blocker="private_fixture",
                workspace_id="standards_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            canonical_store.record_source_access(
                conn,
                work_id=work.row_id,
                provenance_event_ref=prov.event_key,
                original_locator="https://example.org/public-fixture",
                canonical_url="https://example.org/public-fixture",
                access_class="public_web",
                rights_posture="metadata",
                citation_hint="Public fixture citation",
                review_state="accepted",
                publication_state="public_safe",
                workspace_id="standards_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            canonical_store.record_source_access(
                conn,
                work_id=private_work.row_id,
                provenance_event_ref=prov.event_key,
                original_locator=f"/home/private/{PRIVATE_SENTINEL}.txt",
                citation_hint=PRIVATE_SENTINEL,
                review_state="accepted",
                publication_state="private_working",
                public_blocker="private_fixture",
                workspace_id="standards_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            capture = canonical_store.record_capture_event(
                conn,
                provenance_event_ref=prov.event_key,
                work_id=work.row_id,
                original_locator="https://example.org/public-fixture",
                captured_at=FIXED_TIMESTAMP,
                capture_method="remote_url_fixture",
                content_hash="sha256:abc123",
                byte_count=123,
                mime_type="text/html",
                payload_storage_policy_class="transient",
                review_state="accepted",
                workspace_id="standards_subject",
                record_last_updated=FIXED_TIMESTAMP,
            )
            extraction = canonical_store.record_extraction_record(
                conn,
                provenance_event_ref=prov.event_key,
                capture_event_id=capture.row_id,
                extraction_method="fixture_text",
                extraction_status="success",
                extractor_name="pytest",
                summary_short="Public extracted summary.",
                input_hash="sha256:abc123",
                output_hash="sha256:def456",
                byte_count_in=123,
                byte_count_out=45,
                encoding_handling="utf-8",
                review_state="accepted",
                workspace_id="standards_subject",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=prov.event_key,
                extraction_id=extraction.row_id,
                capture_event_id=capture.row_id,
                entity_label="Jane Public",
                normalized_label="jane public",
                entity_type="person",
                review_state="accepted",
                confidence_score=0.9,
                record_last_updated=FIXED_TIMESTAMP,
            )
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Public",
                source_namespace="pytest",
                source_id="jane-public",
                review_state="accepted",
                confidence_score=0.9,
                created_at=FIXED_TIMESTAMP,
            )
            conn.execute(
                "UPDATE extraction_detected_entity SET authority_record_id=? WHERE detected_entity_id=?",
                (authority_id, entity.row_id),
            )
            authority_reconciliation.propose_candidate(
                conn,
                detected_entity_id=entity.row_id,
                raw_label="Jane Public",
                entity_type="person",
                candidate_authority_id=authority_id,
                match_method="fixture",
                match_score=0.9,
                review_state="needs_review",
                created_at=FIXED_TIMESTAMP,
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:standards:public",
                about_object_ref=f"work:{work.row_id}",
                claim_text="Raw claim text remains internal.",
                public_summary="Public descriptive summary.",
                claim_type="description",
                review_state="accepted",
                publication_state="public_safe",
                workspace_id="standards_subject",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=prov.event_key,
                source_claim_key_v1="claim:standards:private",
                about_object_ref=f"work:{private_work.row_id}",
                claim_text=PRIVATE_SENTINEL,
                public_summary=PRIVATE_SENTINEL,
                claim_type="description",
                review_state="accepted",
                publication_state="private_working",
                public_blocker="private_fixture",
                workspace_id="standards_subject",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            relationship = canonical_store.record_source_relationship(
                conn,
                provenance_event_ref=prov.event_key,
                from_object_ref=f"work:{work.row_id}",
                to_object_ref=f"authority_record:{authority_id}",
                predicate="about",
                target_label="Jane Public",
                evidence_note="Public subject relation.",
                review_state="accepted",
                publication_state="public_safe",
                workspace_id="standards_subject",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
            canonical_store.record_review_state_history(
                conn,
                target_namespace="source_relationship",
                target_id=relationship.row_id,
                previous_state="needs_review",
                new_state="accepted",
                changed_by="pytest",
                changed_at=FIXED_TIMESTAMP,
                reason="fixture_accept",
                source_tool="tests/test_standards_profile_crosswalks.py",
                review_state_history_key_v1="review:standards:relationship",
            )
    finally:
        conn.close()
    return {
        "db_path": db_path,
        "work_id": work.row_id,
        "capture_id": capture.row_id,
        "authority_id": authority_id,
    }


def test_profile_configs_validate_and_reference_existing_fields(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        for profile_id in standards_profiles.SUPPORTED_PROFILE_IDS:
            profile = standards_profiles.load_profile(profile_id)
            assert profile["schema_version"] == "standards-profile.v1"
            assert standards_profiles.validate_profile_mappings(conn, profile) == []
    finally:
        conn.close()


def test_invalid_profile_payload_is_rejected() -> None:
    profile = standards_profiles.load_profile("dcmi.v1")
    del profile["field_mappings"][0]["external_target"]

    with pytest.raises(standards_profiles.StandardsProfileError, match="external_target"):
        standards_profiles.validate_profile_payload(profile)


def test_invalid_conformance_status_is_rejected() -> None:
    profile = standards_profiles.load_profile("dcmi.v1")
    profile["conformance_level"] = "magic"

    with pytest.raises(standards_profiles.StandardsProfileError, match="conformance"):
        standards_profiles.validate_profile_payload(profile)


def test_unknown_export_format_is_rejected() -> None:
    profile = standards_profiles.load_profile("dcmi.v1")
    profile["export_format"] = "xml_magic"

    with pytest.raises(standards_profiles.StandardsProfileError, match="export_format"):
        standards_profiles.validate_profile_payload(profile)


def test_dcmi_export_and_conformance_report(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    result = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="dcmi.v1",
        work_id=fixture["work_id"],
        generated_at=FIXED_TIMESTAMP,
    )

    metadata = result.export_payload["records"][0]["metadata"]
    assert metadata["dcterms:title"] == "Public standards fixture work"
    assert "dcterms:identifier" in metadata
    assert "Public descriptive summary." in metadata["dcterms:description"]
    assert result.conformance_report["validation_status"] == "pass"
    assert "dcmi.title" in result.conformance_report["required_fields_satisfied"]
    assert result.conformance_report["required_fields_missing"] == []


def test_premis_export_reports_object_event_fixity_and_agent_limitation(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    result = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="premis.v1",
        capture_id=fixture["capture_id"],
        generated_at=FIXED_TIMESTAMP,
    )

    premis = result.export_payload["premis"]
    assert premis["objects"][0]["fixity"]["message_digest"] == "sha256:abc123"
    assert premis["events"][0]["event_datetime"] == FIXED_TIMESTAMP
    assert any(
        "first-class preservation agent" in item["reason"]
        for item in result.conformance_report["unsupported_fields"]
    )
    assert result.conformance_report["conformance_status"] == "pass_with_warnings"


def test_rico_profile_json_uses_stable_uri_identifiers(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    result = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="rico.v1",
        subject_id="standards_subject",
        base_uri="https://example.org/summa",
        generated_at=FIXED_TIMESTAMP,
    )

    graph = result.export_payload["rico_profile_json"]
    ids = [node["id"] for node in graph["nodes"]]
    assert f"https://example.org/summa/work/{fixture['work_id']}" in ids
    assert all(
        " " not in node_id and node_id.startswith("https://example.org/summa/") for node_id in ids
    )
    assert graph["relations"]


def test_nara_readiness_report_distinguishes_present_and_missing_transfer_package(
    tmp_path: Path,
) -> None:
    fixture = build_fixture_store(tmp_path)
    result = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="nara_preservation_readiness.v1",
        subject_id="standards_subject",
        generated_at=FIXED_TIMESTAMP,
    )

    checks = {
        item["check_id"]: item for item in result.export_payload["readiness_report"]["checks"]
    }
    assert checks["fixity_present"]["status"] == "pass"
    assert checks["actions_recorded"]["status"] == "pass"
    assert checks["transfer_package_present"]["status"] == "not_applicable"
    assert result.conformance_report["conformance_status"] == "report_only"


def test_private_sentinel_is_excluded_from_public_exports(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    for profile_id in ("dcmi.v1", "premis.v1", "rico.v1", "nara_preservation_readiness.v1"):
        kwargs = {"base_uri": "https://example.org/summa"} if profile_id == "rico.v1" else {}
        result = standards_profiles.export_profile(
            db_path=fixture["db_path"],
            profile_id=profile_id,
            subject_id="standards_subject",
            generated_at=FIXED_TIMESTAMP,
            **kwargs,
        )
        assert PRIVATE_SENTINEL not in json.dumps(result.export_payload, sort_keys=True)


def test_lossy_and_unsupported_mappings_are_reported(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    result = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="dcmi.v1",
        work_id=fixture["work_id"],
        generated_at=FIXED_TIMESTAMP,
    )

    assert result.conformance_report["lossy_mappings"]
    assert result.conformance_report["unsupported_fields"]


def test_export_is_deterministic_across_repeated_runs(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    first = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="premis.v1",
        capture_id=fixture["capture_id"],
        generated_at=FIXED_TIMESTAMP,
    )
    second = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="premis.v1",
        capture_id=fixture["capture_id"],
        generated_at=FIXED_TIMESTAMP,
    )

    assert standards_profiles.stable_json(first.export_payload) == standards_profiles.stable_json(
        second.export_payload
    )
    assert standards_profiles.stable_json(
        first.conformance_report
    ) == standards_profiles.stable_json(second.conformance_report)


def test_missing_required_field_causes_conformance_failure(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            prov = provenance(conn, "missing-title")
            work = canonical_store.upsert_work(
                conn,
                work_key_v1="work:missing:title",
                provenance_event_ref=prov.event_key,
                work_type="web_page",
                title=None,
                review_state="accepted",
                publication_state="public_safe",
                workspace_id="standards_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
    finally:
        conn.close()

    result = standards_profiles.export_profile(
        db_path=db_path,
        profile_id="dcmi.v1",
        work_id=work.row_id,
        generated_at=FIXED_TIMESTAMP,
    )

    assert result.conformance_report["validation_status"] == "fail"
    assert result.conformance_report["required_fields_missing"][0]["mapping_id"] == "dcmi.title"


def test_export_does_not_mutate_canonical_store(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    db_path = fixture["db_path"]
    before = file_hash(db_path)  # type: ignore[arg-type]
    standards_profiles.export_profile(
        db_path=db_path,  # type: ignore[arg-type]
        profile_id="rico.v1",
        subject_id="standards_subject",
        base_uri="https://example.org/summa/",
        generated_at=FIXED_TIMESTAMP,
    )
    after = file_hash(db_path)  # type: ignore[arg-type]
    assert before == after
