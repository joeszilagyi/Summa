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


def compact_candidate_record_payload(
    *,
    candidate_type: str,
    raw_output: str,
    locator: Any | None = None,
    confidence: Any | None = None,
    reason: str = "llm_proposed",
    source_span: Any | None = None,
) -> dict[str, Any]:
    """Return the compact candidate record stored in gather batches.

    The record keeps the machine-facing fields bounded and stable while the
    raw engine transcript remains available separately in the batch artifact.
    """

    first_line = str(raw_output or "").splitlines()[0] if raw_output else ""
    bounded_claim = " ".join(first_line.split())[:240] or "claim-fallback-empty"
    return {
        "candidate_type": candidate_type,
        "locator": locator,
        "claim": bounded_claim,
        "confidence": confidence,
        "reason": reason,
        "source_span": source_span,
    }


def compact_prior_state_prompt_payload(
    prior_state: Mapping[str, Any],
    *,
    cycle_depth: int,
) -> dict[str, Any]:
    """Return the compact prior-state object rendered into gather prompts.

    The prompt-facing object keeps the selected canonical IDs and short review
    facts while omitting the verbose audit payload and rendered context text
    that the store keeps for validation and history.
    """

    def compact_selected_counts() -> dict[str, dict[str, Any]]:
        counts = prior_state.get("record_counts")
        compacted: dict[str, dict[str, Any]] = {}
        if not isinstance(counts, Mapping):
            return compacted
        for family, family_counts in counts.items():
            if not isinstance(family_counts, Mapping):
                continue
            compacted[str(family)] = {
                "selected": family_counts.get("selected"),
                "total": family_counts.get("total"),
            }
        return compacted

    def compact_record_list(
        records: Any,
        *,
        field_names: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        if not isinstance(records, list):
            return compacted
        for record in records:
            if not isinstance(record, Mapping):
                continue
            compact_record: dict[str, Any] = {}
            for field_name in field_names:
                value = record.get(field_name)
                if value is not None:
                    compact_record[field_name] = value
            compacted.append(compact_record)
        return compacted

    source = prior_state.get("source")
    compact_source: dict[str, Any] = {}
    if isinstance(source, Mapping):
        for key in ("kind", "subject_id", "schema_version", "subject_scope"):
            value = source.get(key)
            if value is not None:
                compact_source[key] = value

    limits = prior_state.get("limits")
    compact_limits: dict[str, Any] = {}
    if isinstance(limits, Mapping):
        for key in ("per_family_limit", "max_prior_cycles", "high_confidence_threshold"):
            value = limits.get(key)
            if value is not None:
                compact_limits[key] = value

    records = prior_state.get("records")
    record_map = records if isinstance(records, Mapping) else {}
    previous_run_ids = prior_state.get("previous_run_ids")
    compact_payload = {
        "source": compact_source,
        "policy": prior_state.get("policy"),
        "cycle_depth": cycle_depth,
        "previous_run_ids": list(previous_run_ids) if isinstance(previous_run_ids, list) else [],
        "limits": compact_limits,
        "record_counts": compact_selected_counts(),
        "records": {
            "works": compact_record_list(
                record_map.get("works"), field_names=("work_id", "review_state", "confidence_score")
            ),
            "entities": compact_record_list(
                record_map.get("entities"),
                field_names=("detected_entity_id", "review_state", "confidence_score"),
            ),
            "source_claims": compact_record_list(
                record_map.get("source_claims"),
                field_names=("source_claim_id", "review_state", "confidence_score"),
            ),
            "source_access": compact_record_list(
                record_map.get("source_access"),
                field_names=(
                    "source_access_id",
                    "work_id",
                    "source_lead_id",
                    "review_state",
                    "authority_level",
                ),
            ),
            "relationships": compact_record_list(
                record_map.get("relationships"),
                field_names=(
                    "source_relationship_id",
                    "from_object_ref",
                    "to_object_ref",
                    "predicate",
                    "review_state",
                    "confidence_score",
                ),
            ),
            "extraction_summaries": compact_record_list(
                record_map.get("extraction_summaries"),
                field_names=(
                    "extraction_id",
                    "capture_event_id",
                    "review_state",
                    "extraction_status",
                ),
            ),
        },
    }
    schema_version = prior_state.get("schema_version")
    if schema_version is not None:
        compact_payload["schema_version"] = schema_version
    truncated = prior_state.get("truncated")
    if truncated is not None:
        compact_payload["truncated"] = truncated
    return compact_payload
