"""Shared candidate-feedback planning contract constants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

CANDIDATE_FEEDBACK_SCORE_MIN = -100.0
CANDIDATE_FEEDBACK_SCORE_MAX = 100.0

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


def compact_next_action_prompt_payload(next_action: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact next-action object rendered into gather prompts.

    The prompt-facing object keeps only the machine fields the runner needs to
    communicate the selected action.  It intentionally omits the scoring
    rationale, reason codes, and other planner-only explanation fields.
    """

    return {
        "action_id": next_action.get("action_id"),
        "action_kind": next_action.get("action_kind"),
        "subject_id": next_action.get("subject_id"),
        "selected_facet": next_action.get("selected_facet"),
        "selected_prompt_bundle_id": next_action.get("selected_prompt_bundle_id"),
        "should_call_llm": next_action.get("should_call_llm"),
        "selected_object_ref": next_action.get("selected_object_ref"),
        "selected_lead_kind": next_action.get("selected_lead_kind"),
        "cycle_depth": next_action.get("cycle_depth"),
        "use_prior_state": next_action.get("use_prior_state"),
        "previous_run_ids_considered": list(next_action.get("previous_run_ids_considered") or []),
        "input_record_refs": list(next_action.get("input_record_refs") or []),
        "suggested_cli_args": list(next_action.get("suggested_cli_args") or []),
    }
