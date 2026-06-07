from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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


def test_validate_profile_mappings_caches_table_columns_for_repeated_tables() -> None:
    profile = standards_profiles.load_profile("dcmi.v1")
    mapping = dict(profile["field_mappings"][0])
    duplicate = dict(mapping)
    duplicate["mapping_id"] = f"{mapping['mapping_id']}-duplicate"
    profile["field_mappings"] = [mapping, duplicate]
    profile["required_fields"] = []
    profile["optional_fields"] = []

    table_name = str(mapping["summa_source"]["table"])
    field_name = str(mapping["summa_source"]["field"])

    class FakeResult:
        def __init__(self, rows: list[dict[str, str]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[dict[str, str]]:
            return self._rows

    class FakeConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeResult:
            del params
            normalized = " ".join(sql.split())
            self.queries.append(normalized)
            if "FROM sqlite_master" in normalized:
                return FakeResult([{"name": table_name}])
            if normalized == f"PRAGMA table_info({table_name})":
                return FakeResult([{"name": field_name}])
            raise AssertionError(f"unexpected SQL: {sql}")

    fake_conn = FakeConnection()

    assert standards_profiles.validate_profile_mappings(fake_conn, profile) == []
    assert fake_conn.queries.count(f"PRAGMA table_info({table_name})") == 1


def test_public_conditions_for_table_reuses_schema_cache() -> None:
    table_name = "work"

    class FakeResult:
        def __init__(self, rows: list[dict[str, str]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[dict[str, str]]:
            return self._rows

    class FakeConnection:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeResult:
            del params
            normalized = " ".join(sql.split())
            self.queries.append(normalized)
            if normalized == f"PRAGMA table_info({table_name})":
                return FakeResult(
                    [
                        {"name": "public_blocker"},
                        {"name": "publication_state"},
                    ]
                )
            raise AssertionError(f"unexpected SQL: {sql}")

    fake_conn = FakeConnection()
    schema_cache: dict[str, set[str]] = {}

    first = standards_profiles.public_conditions_for_table(
        fake_conn, table_name, alias="w", schema_cache=schema_cache
    )
    second = standards_profiles.public_conditions_for_table(
        fake_conn, table_name, alias="w", schema_cache=schema_cache
    )

    assert first == second
    assert fake_conn.queries.count(f"PRAGMA table_info({table_name})") == 1
    assert first == [
        "COALESCE(w.public_blocker, '') = ''",
        "COALESCE(w.publication_state, 'public_safe') NOT IN ('blocked', 'draft', 'local_only', 'private', 'private_working', 'restricted')",
    ]


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


def test_rico_export_streams_rows_without_fetchall(monkeypatch) -> None:
    profile = standards_profiles.load_profile("rico.v1")

    class FakeCursor:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

        def fetchall(self):
            raise AssertionError("RiC export should not fetchall() rows")

    class FakeConnection:
        def execute(self, sql: str, params: tuple[Any, ...] = ()):  # noqa: ARG002
            if sql.startswith("SELECT * FROM work"):
                return FakeCursor(
                    [
                        {
                            "work_id": 1,
                            "title": "Public work",
                            "work_key_v1": "work:public",
                            "work_type": "web_page",
                            "first_seen_at": FIXED_TIMESTAMP,
                            "rights_posture": "metadata only",
                            "provenance_event_ref": "prov:public",
                            "public_blocker": "",
                            "publication_state": "public_safe",
                        },
                        {
                            "work_id": 2,
                            "title": "Private work",
                            "work_key_v1": "work:private",
                            "work_type": "web_page",
                            "first_seen_at": FIXED_TIMESTAMP,
                            "rights_posture": "metadata only",
                            "provenance_event_ref": "prov:private",
                            "public_blocker": "private_fixture",
                            "publication_state": "private_working",
                        },
                    ]
                )
            if sql.startswith("SELECT * FROM authority_record"):
                return FakeCursor(
                    [
                        {
                            "authority_record_id": 11,
                            "preferred_label": "Public authority",
                            "authority_type": "person",
                            "public_blocker": "",
                            "publication_state": "public_safe",
                        },
                        {
                            "authority_record_id": 12,
                            "preferred_label": "Private authority",
                            "authority_type": "person",
                            "public_blocker": "private_fixture",
                            "publication_state": "private_working",
                        },
                    ]
                )
            if sql.startswith("SELECT * FROM source_relationship"):
                return FakeCursor(
                    [
                        {
                            "source_relationship_id": 21,
                            "predicate": "about",
                            "from_object_ref": "work:1",
                            "to_object_ref": "authority_record:11",
                            "target_label": "Public authority",
                            "public_blocker": "",
                            "publication_state": "public_safe",
                        },
                        {
                            "source_relationship_id": 22,
                            "predicate": "about",
                            "from_object_ref": "work:2",
                            "to_object_ref": "authority_record:12",
                            "target_label": "Private authority",
                            "public_blocker": "private_fixture",
                            "publication_state": "private_working",
                        },
                    ]
                )
            if sql.startswith("SELECT provenance_event_id, event_type, event_timestamp FROM provenance_event"):
                return FakeCursor(
                    [
                        {
                            "provenance_event_id": 31,
                            "event_type": "fixture",
                            "event_timestamp": FIXED_TIMESTAMP,
                        }
                    ]
                )
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self) -> None:
            return None

    payload, report_bits = standards_profiles.build_rico_export(
        FakeConnection(),
        profile,
        subject_id="rebuildability",
        base_uri="https://example.org/summa",
        include_private=False,
        generated_at=FIXED_TIMESTAMP,
        work_id=None,
    )

    graph = payload["rico_profile_json"]
    assert len(graph["nodes"]) == 3
    assert len(graph["relations"]) == 1
    assert {entry["table"] for entry in report_bits["privacy_exclusions"]} == {
        "work",
        "authority_record",
        "source_relationship",
    }
    assert all(entry["excluded_count"] == 1 for entry in report_bits["privacy_exclusions"])


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


def test_nara_readiness_report_uses_one_capture_event_aggregate_query() -> None:
    profile = standards_profiles.load_profile("nara_preservation_readiness.v1")

    class FakeResult:
        def __init__(self, row: tuple[int, ...]) -> None:
            self._row = row

        def fetchone(self) -> tuple[int, ...]:
            return self._row

    class FakeConnection:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeResult:
            normalized = " ".join(sql.split())
            self.queries.append((normalized, params))
            if "FROM capture_event" in normalized:
                assert "SUM(CASE WHEN content_hash IS NOT NULL" in normalized
                assert "SUM(CASE WHEN captured_at IS NOT NULL" in normalized
                assert "SUM(CASE WHEN mime_type IS NOT NULL" in normalized
                assert "SUM(CASE WHEN payload_storage_policy_class IS NOT NULL" in normalized
                return FakeResult((3, 3, 3, 2, 1))
            if "FROM provenance_event" in normalized:
                return FakeResult((4,))
            if "FROM review_state_history" in normalized:
                return FakeResult((1,))
            raise AssertionError(f"unexpected SQL: {sql}")

        def close(self) -> None:
            return None

    fake_conn = FakeConnection()
    payload, report_bits = standards_profiles.build_nara_readiness_report(
        fake_conn,
        profile,
        subject_id="standards_subject",
        include_private=False,
        generated_at=FIXED_TIMESTAMP,
    )

    capture_queries = [sql for sql, _ in fake_conn.queries if "FROM capture_event" in sql]
    assert len(capture_queries) == 1
    assert "SUM(CASE WHEN content_hash IS NOT NULL" in capture_queries[0]
    assert payload["readiness_report"]["summary"] == {
        "capture_event_count": 3,
        "fixity_count": 3,
        "provenance_event_count": 4,
        "review_state_history_count": 1,
        "claim": "readiness report only; not a NARA transfer package",
    }
    checks = {item["check_id"]: item for item in payload["readiness_report"]["checks"]}
    assert checks["fixity_present"]["status"] == "pass"
    assert checks["capture_timestamps"]["status"] == "pass"
    assert checks["format_recorded"]["status"] == "pass"
    assert checks["raw_payload_policy_recorded"]["status"] == "pass"
    assert checks["actions_recorded"]["status"] == "pass"
    assert checks["review_audit_present"]["status"] == "pass"


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


def test_export_profile_does_not_recount_privacy_tables_after_build(tmp_path: Path, monkeypatch) -> None:
    fixture = build_fixture_store(tmp_path)

    def fail_privacy_exclusions_for_table(*_args, **_kwargs):
        raise AssertionError("privacy_exclusions_for_table should not be called after export build")

    monkeypatch.setattr(standards_profiles, "privacy_exclusions_for_table", fail_privacy_exclusions_for_table)

    result = standards_profiles.export_profile(
        db_path=fixture["db_path"],
        profile_id="dcmi.v1",
        subject_id="standards_subject",
        generated_at=FIXED_TIMESTAMP,
    )

    assert result.conformance_report["privacy_exclusions"]


def test_has_private_sentinel_detects_nested_strings_without_json_serialization(monkeypatch) -> None:
    def fail_dumps(*_args, **_kwargs):
        raise AssertionError("json.dumps should not be used by has_private_sentinel")

    monkeypatch.setattr(standards_profiles.json, "dumps", fail_dumps)

    payload = {"outer": [{"inner": {"leaf": PRIVATE_SENTINEL}}]}

    assert standards_profiles.has_private_sentinel(payload) is True


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


def test_export_profile_uses_atomic_json_writer_for_output_and_report(tmp_path: Path) -> None:
    fixture = build_fixture_store(tmp_path)
    output_path = tmp_path / "export.json"
    report_path = tmp_path / "report.json"

    wrote: list[tuple[Path, object]] = []

    def fake_atomic_write(path: Path, payload: object) -> None:
        wrote.append((path, payload))

    def reject_direct_write(_self: object, *args: object, **kwargs: object) -> None:
        raise AssertionError("direct write_text should not be used")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(standards_profiles.Path, "write_text", reject_direct_write)
    monkeypatch.setattr(standards_profiles, "atomic_write_json", fake_atomic_write)
    try:
        standards_profiles.export_profile(
            db_path=fixture["db_path"],
            profile_id="dcmi.v1",
            output_path=output_path,
            conformance_report_path=report_path,
            generated_at=FIXED_TIMESTAMP,
        )
    finally:
        monkeypatch.undo()

    assert len(wrote) == 2
    called_paths = {entry[0] for entry in wrote}
    assert output_path.resolve() in called_paths
    assert report_path.resolve() in called_paths


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
