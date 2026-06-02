"""Shared local-search contract constants and helpers."""

from __future__ import annotations

from typing import Any


PROJECTION_SCHEMA_VERSION = "local-search-projection.v1"
RESULTS_SCHEMA_VERSION = "local-search-results.v1"

VISIBILITY_PROFILES = {
    "local",
    "public_preview",
    "public_release",
}

PUBLIC_VISIBILITY_PROFILES = {
    "public_preview",
    "public_release",
}

SEARCH_OBJECT_TYPES = {
    "authority",
    "claim",
    "controlled_subject",
    "provenance_event",
    "relationship",
    "source_access",
    "topic_extension",
    "work",
}

INDEXED_FIELD_POLICIES = {
    "public",
    "local_only",
}

LINEAGE_STATES = {
    "current",
    "superseded",
}

SEARCHABLE_REVIEW_STATES = {
    "accepted",
    "approved",
    "curated",
    "reviewed",
}

PUBLICATION_STATES = {
    "blocked",
    "local_only",
    "private_working",
    "public_preview",
    "public_release_allowed",
    "public_release_candidate",
}

PUBLIC_SEARCHABLE_PUBLICATION_STATES = {
    "public_preview",
    "public_release_allowed",
    "public_release_candidate",
}

PUBLICATION_STATE_ALIASES = {
    "draft": "private_working",
    "previewable": "public_preview",
    "public_safe": "public_release_allowed",
    "published": "public_release_allowed",
}


def is_public_profile(profile: str | None) -> bool:
    return profile in PUBLIC_VISIBILITY_PROFILES


def is_searchable_review_state(review_state: str | None) -> bool:
    return (review_state or "").strip().lower() in SEARCHABLE_REVIEW_STATES


def normalize_publication_state(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "local_only"
    normalized = value.strip().lower()
    normalized = PUBLICATION_STATE_ALIASES.get(normalized, normalized)
    if normalized in PUBLICATION_STATES:
        return normalized
    return "local_only"
