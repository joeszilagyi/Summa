"""Shared candidate-feedback planning contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "candidate-feedback-plan.v1"
SCORING_POLICY_ID = "candidate-feedback.default.v1"

NEXT_ACTION_KINDS = {
    "facet_bootstrap",
    "facet_only",
    "facet_lead",
}

LEAD_KINDS = {
    "source_access",
    "source_claim",
    "detected_entity",
    "work",
}

DEFERRED_CANDIDATE_KINDS = {
    "facet",
    "lead",
}

LEAD_REVIEW_STATES = {
    "ambiguous",
    "machine_extracted",
    "needs_review",
    "proposed",
    "recorded",
    "unreviewed",
}

ACCEPTED_REVIEW_STATES = {
    "accepted",
    "approved",
    "curated",
    "reviewed",
}

DEFAULT_MAX_FACET_CANDIDATES = 12
DEFAULT_MAX_LEAD_CANDIDATES = 12
DEFAULT_MAX_DEFERRED_CANDIDATES = 24

DEFAULT_SCORING_WEIGHTS = {
    "productive_run": 3.0,
    "open_lead": 2.5,
    "work_yield": 2.0,
    "claim_yield": 1.5,
    "entity_yield": 1.5,
    "relationship_yield": 1.0,
    "successful_extraction": 1.25,
    "failed_extraction_penalty": 1.5,
    "zero_yield_penalty": 2.0,
    "recent_low_yield_penalty": 1.0,
    "bootstrap_bias": 0.25,
}

DEFERRED_NON_RETRYABLE_REASON_CODES = {
    "repeated_low_yield",
}


def deferred_candidate_retryable(reason: str | None) -> bool:
    reason_code = str(reason or "").strip()
    if not reason_code:
        return True
    return reason_code not in DEFERRED_NON_RETRYABLE_REASON_CODES
