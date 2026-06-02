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
    "entity": {"authority_record", "extraction_detected_entity"},
    "relationship": {"source_relationship"},
    "assertion": {"source_claim"},
    "provenance_event": {"provenance_event"},
}
