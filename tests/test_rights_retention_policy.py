from __future__ import annotations

from tools.source_db_tools import rights_retention


def test_rights_retention_registry_loads() -> None:
    registry = rights_retention.load_policy_registry()

    assert registry["schema_version"] == "rights-retention-policy.v1"
    assert "quote_limited" in registry["rights_postures"]
    assert registry["storage_policy_classes"]["payload"]["external_later"]["byte_retention_status"] == "not_retained_local"
    assert registry["refetchability_by_input_family"]["remote_url_manifest"] == "configured_remote_manifest"


def test_rights_retention_derives_export_and_quote_policy() -> None:
    facts = rights_retention.derive_adapter_policy_facts(
        {
            "payload_storage_policy_class": "external_later",
            "metadata_storage_policy_class": "tracked_derived",
            "rights_posture": "quote_limited",
        },
        input_family="remote_url_manifest",
    )

    assert facts["byte_retention_status"] == "not_retained_local"
    assert facts["refetchability_status"] == "configured_remote_manifest"
    assert facts["public_export_eligibility"] == "metadata_only"
    assert facts["quote_eligibility"] == "limited_excerpt"
    assert facts["public_export_blockers"] == []


def test_rights_retention_validates_record_policy_statuses() -> None:
    result = rights_retention.validate_record_policy(
        {
            "rights_posture": "unknown_review_required",
            "source_access": {"refetchability_status": "uncertain"},
            "capture_event": {"byte_retention_status": "temporary_processing_input"},
            "extraction_record": {"full_text_retention_status": "temporary_processing_input"},
        }
    )

    assert result["errors"] == []
    assert result["warnings"] == []
