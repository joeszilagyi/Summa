"""Shared candidate-feedback planning contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "candidate-feedback-plan.v1"

PROPOSAL_KINDS = {
    "update_candidate",
    "relationship_candidate",
    "review_task",
}

APPEND_ONLY_TARGETS = {
    "field_review_state",
    "review_queue",
}

PENDING_REVIEW_STATES = {
    "",
    "unreviewed",
    "machine_extracted",
    "proposed",
    "needs_review",
    "ambiguous",
    "demoted",
}

ACCEPTED_REVIEW_STATES = {
    "accepted",
    "approved",
    "curated",
    "reviewed",
}

TARGET_RECORD_FAMILIES = {
    "entity",
    "relationship",
    "assertion",
}

