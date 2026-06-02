"""Shared field-level review-state contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "field-review-state.v1"

FIELD_REVIEW_STATES = {
    "unreviewed",
    "reviewed",
    "disputed",
    "demoted",
    "superseded",
}

RECORD_REVIEW_STATES = {
    "accepted",
    "deprecated",
    "demoted",
    "needs_review",
    "not_applicable",
    "rejected",
    "reviewed",
    "unreviewed",
}

EVIDENCE_TYPES = {
    "field_snapshot",
    "operator_note",
    "source_excerpt",
    "structured_record_field",
}

EVIDENCE_LOCATOR_REF_PREFIX = "evl:"
