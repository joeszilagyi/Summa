"""Shared evidence locator/highlight contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "evidence-locator.v1"
EVIDENCE_LOCATOR_ID_PREFIX = "evl:"

SPAN_KINDS = {
    "page_span",
    "line_span",
    "byte_range",
    "structured_field",
    "metadata_only",
}

HIGHLIGHT_KINDS = {
    "exact_quote",
    "summary",
    "metadata_note",
}

REDACTION_POSTURES = {
    "public_text_allowed",
    "public_summary_only",
    "private_only",
}
