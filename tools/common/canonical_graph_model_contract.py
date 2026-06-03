"""Shared canonical-graph model outline contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "canonical-graph-model-outline.v1"
CONTRACT_DOC = "docs/project/CANONICAL_GRAPH_MODEL.md"

REQUIRED_RECORD_FAMILIES = {
    "entity",
    "relationship",
    "assertion",
    "provenance_event",
    "confidence_assessment",
    "review_annotation",
}

REQUIRED_SIDECARS = {
    "correction_ledger",
    "evidence_locator",
    "field_review_state",
}

REQUIRED_SQLITE_TABLE_MAPPINGS = {
    "entity": {"authority_record", "extraction_detected_entity", "work", "work_subject"},
    "relationship": {"source_relationship", "work_subject"},
    "assertion": {"source_claim", "topic_extension"},
    "provenance_event": {"provenance_event", "capture_event", "extraction_record"},
    "confidence_assessment": {"authority_record", "source_relationship", "source_claim", "topic_extension"},
    "review_annotation": {"review_state_history", "authority_reconciliation"},
}

REQUIRED_SUPPORTING_SQLITE_TABLES = {
    "authority_identifier",
    "authority_merge_event",
    "source_access",
    "work_identifier",
    "work_metadata",
    "work_url",
}

REQUIRED_SCHEMA_METADATA_TABLES = {
    "schema_version",
    "schema_migration_history",
}

REQUIRED_NONCANONICAL_STAGING_TABLES = {
    "source_locus",
    "source_query_execution_simulation",
    "simulated_source_lead_candidate",
}

DOCUMENTED_EXPECTED_SQLITE_TABLES = {
    "authority_record",
    "extraction_detected_entity",
    "work",
    "work_subject",
    "source_relationship",
    "source_claim",
    "provenance_event",
    "capture_event",
    "extraction_record",
    "review_state_history",
    "authority_reconciliation",
}
