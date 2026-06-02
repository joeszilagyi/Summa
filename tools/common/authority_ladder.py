"""Shared authority-ladder contract helpers for public export validation."""

from __future__ import annotations


PUBLIC_EXPORT_PROFILES = {
    "public_preview",
    "public_release",
}

PUBLICATION_VISIBLE_STATES = {
    "previewable",
    "public_safe",
    "published",
}

AUTHORITY_CONTENT_CLASSES = {
    "metadata_only",
    "supporting_context",
    "authoritative_claims",
}

BLOCKING_FIELD_REVIEW_STATES = {
    "demoted",
    "disputed",
    "superseded",
    "unreviewed",
}

ALLOWED_FIELD_REVIEW_STATES = {"reviewed"} | BLOCKING_FIELD_REVIEW_STATES


def is_public_export_profile(export_profile: str | None) -> bool:
    return export_profile in PUBLIC_EXPORT_PROFILES


def is_visible_publication_state(publication_state: str | None) -> bool:
    return publication_state in PUBLICATION_VISIBLE_STATES
