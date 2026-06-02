"""Shared correction-ledger contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "correction-ledger.v1"

CORRECTION_ACTIONS = {
    "dedupe",
    "merge",
    "split",
    "supersede",
}

OBJECT_REF_PREFIXES = {
    "authority",
    "authority_identifier",
    "claim",
    "detected_entity",
    "highlight",
    "lead",
    "relationship",
    "source_access",
    "topic_extension",
    "work",
    "work_identifier",
    "work_subject",
}

REVIEW_QUEUE_REF_PREFIXES = OBJECT_REF_PREFIXES

PROVENANCE_EVENT_REF_PREFIX = "prov:"
EVIDENCE_LOCATOR_REF_PREFIX = "evl:"
FIELD_REVIEW_ENTRY_REF_PREFIX = "frs:"
