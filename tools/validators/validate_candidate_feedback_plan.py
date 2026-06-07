#!/usr/bin/env python3
"""Validate candidate-feedback-plan JSON artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from common import (
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        is_rfc3339_datetime,
        render_text_report,
        write_json,
        write_text,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        is_rfc3339_datetime,
        render_text_report,
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.candidate_feedback_contract import (  # noqa: E402
    DEFAULT_SCORING_WEIGHTS,
    DEFERRED_CANDIDATE_KINDS,
    LEAD_KINDS,
    NEXT_ACTION_KINDS,
    SCHEMA_VERSION,
    SCORING_POLICY_ID,
)
from tools.common.selection_explanation import (  # noqa: E402
    validate_selection_explanation,
)

VALIDATOR_NAME = "candidate_feedback_plan"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/candidate_feedback_plan.schema.json"
FACET_CANDIDATE_PATTERN = re.compile(r"^facet:[a-z0-9_][a-z0-9._-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_KEYS = {
    "schema_version",
    "generated_at",
    "subject",
    "canonical_store",
    "scoring_policy",
    "counts",
    "facet_scores",
    "lead_scores",
    "next_action",
    "deferred",
    "selection_explanation",
    "warnings",
    "errors",
}
SUBJECT_REQUIRED_KEYS = {
    "subject_id",
    "display_name",
    "domain_pack",
    "enabled_facets",
    "query_families",
}
CANONICAL_STORE_REQUIRED_KEYS = {
    "database_name",
    "schema_version",
    "current_migration_id",
    "dry_run",
}
SCORING_POLICY_REQUIRED_KEYS = {
    "policy_id",
    "cycle_depth_considered",
    "previous_run_ids_considered",
    "use_prior_state",
    "weights",
    "limits",
}
COUNT_REQUIRED_KEYS = {
    "gather_runs_considered",
    "facet_candidates",
    "lead_candidates",
    "productive_leads",
    "deferred_candidates",
}
FACET_SCORE_REQUIRED_KEYS = {
    "rank",
    "candidate_id",
    "facet",
    "prompt_bundle_id",
    "score",
    "selected",
    "supporting_facet",
    "reason_codes",
    "rationale",
    "signals",
}
LEAD_SCORE_REQUIRED_KEYS = {
    "rank",
    "candidate_id",
    "lead_kind",
    "object_ref",
    "facet",
    "review_state",
    "label",
    "score",
    "selected",
    "reason_codes",
    "rationale",
    "signals",
    "related_run_ids",
}
NEXT_ACTION_REQUIRED_KEYS = {
    "action_id",
    "action_kind",
    "subject_id",
    "selected_facet",
    "selected_prompt_bundle_id",
    "should_call_llm",
    "selection_score",
    "scoring_policy_id",
    "rationale",
    "reason_codes",
    "cycle_depth",
    "use_prior_state",
    "previous_run_ids_considered",
    "input_record_refs",
    "suggested_cli_args",
}
DEFERRED_REQUIRED_KEYS = {
    "candidate_id",
    "candidate_kind",
    "score",
    "reason",
}
SIGNAL_BUCKET_KEYS = {
    "productive_runs",
    "zero_yield_runs",
    "open_leads",
    "works",
    "claims",
    "entities",
    "relationships",
    "successful_extractions",
    "failed_extractions",
}
LEAD_SIGNAL_KEYS = {
    "open_lead",
    "related_works",
    "related_claims",
    "related_entities",
    "successful_extractions",
    "failed_extractions",
    "zero_yield_attempts",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate one candidate-feedback-plan JSON artifact.")
    parser.add_argument("target", help="Path to the candidate-feedback-plan JSON artifact.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(errors: list[dict[str, Any]], *, code: str, message: str, line: int | None = None) -> None:
    errors.append({"code": code, "line": line, "message": message})


def reject_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(f"non-standard JSON constant: {value}")


def no_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def load_json_object(target: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []
    if not target.exists():
        add_error(errors, code="INPUT_NOT_FOUND", message="input path does not exist")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    if not target.is_file():
        add_error(errors, code="INPUT_NOT_FILE", message="input path is not a file")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        add_error(errors, code="INPUT_UNREADABLE", message="input file could not be read")
        return None, errors, EXIT_INPUT_UNAVAILABLE
    try:
        payload = json.loads(raw_text, object_pairs_hook=no_duplicate_object_pairs, parse_constant=reject_json_constant)
    except DuplicateJsonKeyError as exc:
        add_error(errors, code="DUPLICATE_JSON_KEY", line=1, message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(errors, code="NON_STANDARD_JSON_CONSTANT", line=1, message=str(exc))
        return None, errors, EXIT_VALIDATION_FAILED
    except json.JSONDecodeError as exc:
        add_error(errors, code="JSON_PARSE_ERROR", line=exc.lineno, message="invalid JSON syntax")
        return None, errors, EXIT_VALIDATION_FAILED
    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", message="top-level JSON value must be an object")
        return None, errors, EXIT_VALIDATION_FAILED
    return payload, errors, EXIT_PASS


def validate_nonblank_string(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field_name} must be a non-blank string")
        return None
    return value


def validate_string_list(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str) -> list[str]:
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field_name} must be an array")
        return []
    accepted: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field_name}[{index}] must be a non-blank string")
            continue
        accepted.append(item)
    return accepted


def validate_required_object(
    payload: dict[str, Any],
    *,
    field_name: str,
    required_keys: set[str],
    errors: list[dict[str, Any]],
    missing_code: str,
    object_code: str,
) -> dict[str, Any] | None:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        add_error(errors, code=object_code, message=f"{field_name} must be an object")
        return None
    for key in sorted(required_keys - set(value)):
        add_error(errors, code=missing_code, message=f"missing required {field_name} key: {key}")
    return value


def validate_numeric_bucket(
    value: Any,
    *,
    field_name: str,
    required_keys: set[str],
    errors: list[dict[str, Any]],
) -> None:
    if not isinstance(value, dict):
        add_error(errors, code="INVALID_SIGNALS", message=f"{field_name} must be an object")
        return
    for key in sorted(required_keys - set(value)):
        add_error(errors, code="MISSING_SIGNAL_KEY", message=f"missing required {field_name} key: {key}")
    for key in sorted(required_keys):
        item = value.get(key)
        if not isinstance(item, int) or item < 0:
            add_error(errors, code="INVALID_SIGNAL_VALUE", message=f"{field_name}.{key} must be a non-negative integer")


def validate_subject(payload: dict[str, Any], errors: list[dict[str, Any]]) -> list[str]:
    subject = validate_required_object(
        payload,
        field_name="subject",
        required_keys=SUBJECT_REQUIRED_KEYS,
        errors=errors,
        missing_code="MISSING_SUBJECT_KEY",
        object_code="SUBJECT_NOT_OBJECT",
    )
    if subject is None:
        return []
    validate_nonblank_string(subject.get("subject_id"), field_name="subject.subject_id", errors=errors, code="INVALID_SUBJECT_FIELD")
    validate_nonblank_string(subject.get("display_name"), field_name="subject.display_name", errors=errors, code="INVALID_SUBJECT_FIELD")
    validate_nonblank_string(subject.get("domain_pack"), field_name="subject.domain_pack", errors=errors, code="INVALID_SUBJECT_FIELD")
    enabled_facets = validate_string_list(subject.get("enabled_facets"), field_name="subject.enabled_facets", errors=errors, code="INVALID_SUBJECT_FIELD")
    validate_string_list(subject.get("query_families"), field_name="subject.query_families", errors=errors, code="INVALID_SUBJECT_FIELD")
    return enabled_facets


def validate_canonical_store(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    source = validate_required_object(
        payload,
        field_name="canonical_store",
        required_keys=CANONICAL_STORE_REQUIRED_KEYS,
        errors=errors,
        missing_code="MISSING_CANONICAL_STORE_KEY",
        object_code="CANONICAL_STORE_NOT_OBJECT",
    )
    if source is None:
        return
    validate_nonblank_string(source.get("database_name"), field_name="canonical_store.database_name", errors=errors, code="INVALID_CANONICAL_STORE_FIELD")
    schema_version = source.get("schema_version")
    if schema_version is not None and (not isinstance(schema_version, int) or schema_version < 1):
        add_error(errors, code="INVALID_CANONICAL_STORE_FIELD", message="canonical_store.schema_version must be null or a positive integer")
    current_migration_id = source.get("current_migration_id")
    if current_migration_id is not None and not isinstance(current_migration_id, str):
        add_error(errors, code="INVALID_CANONICAL_STORE_FIELD", message="canonical_store.current_migration_id must be null or a string")
    if source.get("dry_run") is not True:
        add_error(errors, code="INVALID_CANONICAL_STORE_FIELD", message="canonical_store.dry_run must be true")


def validate_scoring_policy(payload: dict[str, Any], errors: list[dict[str, Any]]) -> list[str]:
    policy = validate_required_object(
        payload,
        field_name="scoring_policy",
        required_keys=SCORING_POLICY_REQUIRED_KEYS,
        errors=errors,
        missing_code="MISSING_SCORING_POLICY_KEY",
        object_code="SCORING_POLICY_NOT_OBJECT",
    )
    if policy is None:
        return []
    if policy.get("policy_id") != SCORING_POLICY_ID:
        add_error(errors, code="INVALID_SCORING_POLICY", message=f"scoring_policy.policy_id must equal {SCORING_POLICY_ID}")
    cycle_depth = policy.get("cycle_depth_considered")
    if not isinstance(cycle_depth, int) or cycle_depth < 1:
        add_error(errors, code="INVALID_SCORING_POLICY", message="scoring_policy.cycle_depth_considered must be a positive integer")
    previous_run_ids = validate_string_list(
        policy.get("previous_run_ids_considered"),
        field_name="scoring_policy.previous_run_ids_considered",
        errors=errors,
        code="INVALID_SCORING_POLICY",
    )
    if not isinstance(policy.get("use_prior_state"), bool):
        add_error(errors, code="INVALID_SCORING_POLICY", message="scoring_policy.use_prior_state must be a boolean")
    weights = policy.get("weights")
    if not isinstance(weights, dict):
        add_error(errors, code="INVALID_SCORING_POLICY", message="scoring_policy.weights must be an object")
    else:
        for key in DEFAULT_SCORING_WEIGHTS:
            value = weights.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                add_error(errors, code="INVALID_SCORING_WEIGHT", message=f"scoring_policy.weights.{key} must be numeric")
    limits = policy.get("limits")
    if not isinstance(limits, dict):
        add_error(errors, code="INVALID_SCORING_POLICY", message="scoring_policy.limits must be an object")
    else:
        for key in ("max_facet_candidates", "max_lead_candidates", "max_deferred_candidates"):
            value = limits.get(key)
            if not isinstance(value, int) or value < 0:
                add_error(errors, code="INVALID_SCORING_LIMIT", message=f"scoring_policy.limits.{key} must be a non-negative integer")
    return previous_run_ids


def validate_counts(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    counts = validate_required_object(
        payload,
        field_name="counts",
        required_keys=COUNT_REQUIRED_KEYS,
        errors=errors,
        missing_code="MISSING_COUNT_KEY",
        object_code="COUNTS_NOT_OBJECT",
    )
    if counts is None:
        return
    for key in sorted(COUNT_REQUIRED_KEYS):
        value = counts.get(key)
        if not isinstance(value, int) or value < 0:
            add_error(errors, code="INVALID_COUNT_VALUE", message=f"counts.{key} must be a non-negative integer")


def validate_reason_codes(value: Any, *, field_name: str, errors: list[dict[str, Any]]) -> list[str]:
    codes = validate_string_list(value, field_name=field_name, errors=errors, code="INVALID_REASON_CODES")
    if not codes:
        add_error(errors, code="INVALID_REASON_CODES", message=f"{field_name} must not be empty")
    return codes


def validate_facet_scores(payload: dict[str, Any], enabled_facets: list[str], errors: list[dict[str, Any]]) -> None:
    facet_scores = payload.get("facet_scores")
    if not isinstance(facet_scores, list):
        add_error(errors, code="FACET_SCORES_NOT_ARRAY", message="facet_scores must be an array")
        return
    selected_count = 0
    supporting_count = 0
    previous_score: float | None = None
    expected_rank = 1
    seen_facets: set[str] = set()
    enabled_set = set(enabled_facets)
    for index, item in enumerate(facet_scores):
        label = f"facet_scores[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="FACET_SCORE_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(FACET_SCORE_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_FACET_SCORE_KEY", message=f"missing required {label} key: {key}")
        rank = item.get("rank")
        if not isinstance(rank, int) or rank != expected_rank:
            add_error(errors, code="INVALID_FACET_SCORE_RANK", message=f"{label}.rank must equal {expected_rank}")
        expected_rank += 1
        candidate_id = validate_nonblank_string(item.get("candidate_id"), field_name=f"{label}.candidate_id", errors=errors, code="INVALID_FACET_SCORE")
        if candidate_id is not None and not FACET_CANDIDATE_PATTERN.fullmatch(candidate_id):
            add_error(errors, code="INVALID_FACET_SCORE", message=f"{label}.candidate_id must match ^facet:[a-z0-9_][a-z0-9._-]*$")
        facet = validate_nonblank_string(item.get("facet"), field_name=f"{label}.facet", errors=errors, code="INVALID_FACET_SCORE")
        if facet is not None:
            if enabled_set and facet not in enabled_set:
                add_error(errors, code="INVALID_FACET_SCORE", message=f"{label}.facet is not enabled for the subject: {facet}")
            if facet in seen_facets:
                add_error(errors, code="DUPLICATE_FACET_SCORE", message=f"duplicate facet score entry: {facet}")
            seen_facets.add(facet)
        validate_nonblank_string(item.get("prompt_bundle_id"), field_name=f"{label}.prompt_bundle_id", errors=errors, code="INVALID_FACET_SCORE")
        score = item.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            add_error(errors, code="INVALID_FACET_SCORE", message=f"{label}.score must be numeric")
        else:
            score_value = float(score)
            if previous_score is not None and score_value > previous_score + 1e-9:
                add_error(errors, code="FACET_SCORES_NOT_SORTED", message="facet_scores must be sorted in descending score order")
            previous_score = score_value
        if not isinstance(item.get("selected"), bool):
            add_error(errors, code="INVALID_FACET_SCORE", message=f"{label}.selected must be a boolean")
        elif item.get("selected") is True:
            selected_count += 1
        if not isinstance(item.get("supporting_facet"), bool):
            add_error(errors, code="INVALID_FACET_SCORE", message=f"{label}.supporting_facet must be a boolean")
        elif item.get("supporting_facet") is True:
            supporting_count += 1
        if item.get("selected") is True and item.get("supporting_facet") is True:
            add_error(
                errors,
                code="INVALID_FACET_SCORE",
                message=f"{label}.selected and {label}.supporting_facet must not both be true",
            )
        validate_reason_codes(item.get("reason_codes"), field_name=f"{label}.reason_codes", errors=errors)
        validate_nonblank_string(item.get("rationale"), field_name=f"{label}.rationale", errors=errors, code="INVALID_FACET_SCORE")
        validate_numeric_bucket(item.get("signals"), field_name=f"{label}.signals", required_keys=SIGNAL_BUCKET_KEYS, errors=errors)
    if selected_count > 1:
        add_error(errors, code="INVALID_FACET_SELECTION", message="facet_scores must not mark more than one selected facet")
    if supporting_count > 1:
        add_error(errors, code="INVALID_FACET_SELECTION", message="facet_scores must not mark more than one supporting facet")


def validate_lead_scores(payload: dict[str, Any], enabled_facets: list[str], errors: list[dict[str, Any]]) -> None:
    lead_scores = payload.get("lead_scores")
    if not isinstance(lead_scores, list):
        add_error(errors, code="LEAD_SCORES_NOT_ARRAY", message="lead_scores must be an array")
        return
    previous_score: float | None = None
    expected_rank = 1
    selected_count = 0
    seen_ids: set[str] = set()
    enabled_set = set(enabled_facets)
    for index, item in enumerate(lead_scores):
        label = f"lead_scores[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="LEAD_SCORE_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(LEAD_SCORE_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_LEAD_SCORE_KEY", message=f"missing required {label} key: {key}")
        rank = item.get("rank")
        if not isinstance(rank, int) or rank != expected_rank:
            add_error(errors, code="INVALID_LEAD_SCORE_RANK", message=f"{label}.rank must equal {expected_rank}")
        expected_rank += 1
        candidate_id = validate_nonblank_string(item.get("candidate_id"), field_name=f"{label}.candidate_id", errors=errors, code="INVALID_LEAD_SCORE")
        if candidate_id is not None:
            if candidate_id in seen_ids:
                add_error(errors, code="DUPLICATE_LEAD_SCORE", message=f"duplicate lead candidate_id: {candidate_id}")
            seen_ids.add(candidate_id)
        lead_kind = item.get("lead_kind")
        if lead_kind not in LEAD_KINDS:
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.lead_kind must be one of {sorted(LEAD_KINDS)}")
        validate_nonblank_string(item.get("object_ref"), field_name=f"{label}.object_ref", errors=errors, code="INVALID_LEAD_SCORE")
        facet = validate_nonblank_string(item.get("facet"), field_name=f"{label}.facet", errors=errors, code="INVALID_LEAD_SCORE")
        if facet is not None and enabled_set and facet not in enabled_set:
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.facet is not enabled for the subject: {facet}")
        validate_nonblank_string(item.get("review_state"), field_name=f"{label}.review_state", errors=errors, code="INVALID_LEAD_SCORE")
        validate_nonblank_string(item.get("label"), field_name=f"{label}.label", errors=errors, code="INVALID_LEAD_SCORE")
        score = item.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.score must be numeric")
        else:
            score_value = float(score)
            if previous_score is not None and score_value > previous_score + 1e-9:
                add_error(errors, code="LEAD_SCORES_NOT_SORTED", message="lead_scores must be sorted in descending score order")
            previous_score = score_value
        if not isinstance(item.get("selected"), bool):
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.selected must be a boolean")
        elif item.get("selected") is True:
            selected_count += 1
        validate_reason_codes(item.get("reason_codes"), field_name=f"{label}.reason_codes", errors=errors)
        validate_nonblank_string(item.get("rationale"), field_name=f"{label}.rationale", errors=errors, code="INVALID_LEAD_SCORE")
        validate_numeric_bucket(item.get("signals"), field_name=f"{label}.signals", required_keys=LEAD_SIGNAL_KEYS, errors=errors)
        related_run_ids = validate_string_list(item.get("related_run_ids"), field_name=f"{label}.related_run_ids", errors=errors, code="INVALID_LEAD_SCORE")
        if item.get("source_locus_id") is not None and not isinstance(item.get("source_locus_id"), str):
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.source_locus_id must be null or a string")
        if item.get("source_lead_id") is not None and not isinstance(item.get("source_lead_id"), str):
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.source_lead_id must be null or a string")
        if related_run_ids != sorted(dict.fromkeys(related_run_ids), reverse=False):
            # Keep deterministic but not prescriptive about chronology; require de-duplicated stable order.
            add_error(errors, code="INVALID_LEAD_SCORE", message=f"{label}.related_run_ids must be de-duplicated and stable")
    if selected_count > 1:
        add_error(errors, code="INVALID_LEAD_SELECTION", message="lead_scores must not mark more than one selected lead")


def validate_next_action(payload: dict[str, Any], enabled_facets: list[str], previous_run_ids: list[str], errors: list[dict[str, Any]]) -> None:
    next_action = validate_required_object(
        payload,
        field_name="next_action",
        required_keys=NEXT_ACTION_REQUIRED_KEYS,
        errors=errors,
        missing_code="MISSING_NEXT_ACTION_KEY",
        object_code="NEXT_ACTION_NOT_OBJECT",
    )
    if next_action is None:
        return
    validate_nonblank_string(next_action.get("action_id"), field_name="next_action.action_id", errors=errors, code="INVALID_NEXT_ACTION")
    if next_action.get("action_kind") not in NEXT_ACTION_KINDS:
        add_error(errors, code="INVALID_NEXT_ACTION", message=f"next_action.action_kind must be one of {sorted(NEXT_ACTION_KINDS)}")
    validate_nonblank_string(next_action.get("subject_id"), field_name="next_action.subject_id", errors=errors, code="INVALID_NEXT_ACTION")
    selected_facet = validate_nonblank_string(next_action.get("selected_facet"), field_name="next_action.selected_facet", errors=errors, code="INVALID_NEXT_ACTION")
    if selected_facet is not None and enabled_facets and selected_facet not in set(enabled_facets):
        add_error(errors, code="INVALID_NEXT_ACTION", message=f"next_action.selected_facet is not enabled for the subject: {selected_facet}")
    validate_nonblank_string(next_action.get("selected_prompt_bundle_id"), field_name="next_action.selected_prompt_bundle_id", errors=errors, code="INVALID_NEXT_ACTION")
    if not isinstance(next_action.get("should_call_llm"), bool):
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.should_call_llm must be a boolean")
    if next_action.get("selected_object_ref") is not None and not isinstance(next_action.get("selected_object_ref"), str):
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.selected_object_ref must be null or a string")
    if next_action.get("selected_lead_kind") not in LEAD_KINDS | {None}:
        add_error(errors, code="INVALID_NEXT_ACTION", message=f"next_action.selected_lead_kind must be null or one of {sorted(LEAD_KINDS)}")
    for field in ("selected_source_locus_id", "selected_source_lead_id", "selected_label", "selected_review_state"):
        value = next_action.get(field)
        if value is not None and not isinstance(value, str):
            add_error(errors, code="INVALID_NEXT_ACTION", message=f"next_action.{field} must be null or a string")
    score = next_action.get("selection_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.selection_score must be numeric")
    if next_action.get("scoring_policy_id") != SCORING_POLICY_ID:
        add_error(errors, code="INVALID_NEXT_ACTION", message=f"next_action.scoring_policy_id must equal {SCORING_POLICY_ID}")
    validate_nonblank_string(next_action.get("rationale"), field_name="next_action.rationale", errors=errors, code="INVALID_NEXT_ACTION")
    validate_reason_codes(next_action.get("reason_codes"), field_name="next_action.reason_codes", errors=errors)
    cycle_depth = next_action.get("cycle_depth")
    if not isinstance(cycle_depth, int) or cycle_depth < 1:
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.cycle_depth must be a positive integer")
    if not isinstance(next_action.get("use_prior_state"), bool):
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.use_prior_state must be a boolean")
    next_previous_run_ids = validate_string_list(
        next_action.get("previous_run_ids_considered"),
        field_name="next_action.previous_run_ids_considered",
        errors=errors,
        code="INVALID_NEXT_ACTION",
    )
    if next_previous_run_ids != previous_run_ids:
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.previous_run_ids_considered must match scoring_policy.previous_run_ids_considered")
    validate_string_list(next_action.get("input_record_refs"), field_name="next_action.input_record_refs", errors=errors, code="INVALID_NEXT_ACTION")
    cli_args = validate_string_list(next_action.get("suggested_cli_args"), field_name="next_action.suggested_cli_args", errors=errors, code="INVALID_NEXT_ACTION")
    if "--facet" not in cli_args:
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.suggested_cli_args must include --facet")
    if next_action.get("use_prior_state") is True and "--use-prior-state" not in cli_args:
        add_error(errors, code="INVALID_NEXT_ACTION", message="next_action.suggested_cli_args must include --use-prior-state when next_action.use_prior_state is true")
    if next_action.get("action_kind") == "facet_lead" and next_action.get("selected_object_ref") is None:
        add_error(errors, code="INVALID_NEXT_ACTION", message="facet_lead next actions must include selected_object_ref")
    if next_action.get("action_kind") != "facet_lead" and next_action.get("selected_lead_kind") is not None:
        add_error(errors, code="INVALID_NEXT_ACTION", message="non-lead next actions must not include selected_lead_kind")
    if next_action.get("action_kind") != "facet_lead" and next_action.get("selected_object_ref") is not None:
        add_error(errors, code="INVALID_NEXT_ACTION", message="non-lead next actions must not include selected_object_ref")
    if next_action.get("selected_object_ref") is None and next_action.get("should_call_llm") is not True:
        add_error(
            errors,
            code="INVALID_NEXT_ACTION",
            message="non-lead next actions must set should_call_llm=true",
        )
    if next_action.get("selected_object_ref") is not None and next_action.get("should_call_llm") is not False:
        add_error(
            errors,
            code="INVALID_NEXT_ACTION",
            message="lead next actions must set should_call_llm=false",
        )


def validate_deferred(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    deferred = payload.get("deferred")
    if not isinstance(deferred, list):
        add_error(errors, code="DEFERRED_NOT_ARRAY", message="deferred must be an array")
        return
    seen_ids: set[str] = set()
    for index, item in enumerate(deferred):
        label = f"deferred[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="DEFERRED_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(DEFERRED_REQUIRED_KEYS - set(item)):
            add_error(errors, code="MISSING_DEFERRED_KEY", message=f"missing required {label} key: {key}")
        candidate_id = validate_nonblank_string(item.get("candidate_id"), field_name=f"{label}.candidate_id", errors=errors, code="INVALID_DEFERRED")
        if candidate_id is not None:
            if candidate_id in seen_ids:
                add_error(errors, code="DUPLICATE_DEFERRED_CANDIDATE", message=f"duplicate deferred candidate_id: {candidate_id}")
            seen_ids.add(candidate_id)
        if item.get("candidate_kind") not in DEFERRED_CANDIDATE_KINDS:
            add_error(errors, code="INVALID_DEFERRED", message=f"{label}.candidate_kind must be one of {sorted(DEFERRED_CANDIDATE_KINDS)}")
        score = item.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            add_error(errors, code="INVALID_DEFERRED", message=f"{label}.score must be numeric")
        validate_nonblank_string(item.get("reason"), field_name=f"{label}.reason", errors=errors, code="INVALID_DEFERRED")


def validate_selection_explanation_contract(
    payload: dict[str, Any], errors: list[dict[str, Any]]
) -> None:
    explanation = payload.get("selection_explanation")
    if not isinstance(explanation, dict):
        add_error(
            errors,
            code="SELECTION_EXPLANATION_NOT_OBJECT",
            message="selection_explanation must be an object",
        )
        return
    for message in validate_selection_explanation(explanation):
        add_error(errors, code="INVALID_SELECTION_EXPLANATION", message=message)
    next_action = payload.get("next_action")
    if not isinstance(next_action, dict):
        return
    selected = explanation.get("selected_candidate")
    if not isinstance(selected, dict):
        return
    selected_object_ref = next_action.get("selected_object_ref")
    metadata = selected.get("metadata") if isinstance(selected.get("metadata"), dict) else {}
    if selected_object_ref is not None and metadata.get("object_ref") != selected_object_ref:
        add_error(
            errors,
            code="INVALID_SELECTION_EXPLANATION",
            message="selection_explanation selected lead must match next_action.selected_object_ref",
        )
    if selected_object_ref is None and metadata.get("facet") != next_action.get("selected_facet"):
        add_error(
            errors,
            code="INVALID_SELECTION_EXPLANATION",
            message="selection_explanation selected facet must match next_action.selected_facet",
        )


def validate_warning_error_lists(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    for field_name in ("warnings", "errors"):
        validate_string_list(payload.get(field_name), field_name=field_name, errors=errors, code="INVALID_TEXT_LIST")


def validate_candidate_feedback_plan(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        return {
            "validator": VALIDATOR_NAME,
            "contract_version": CONTRACT_VERSION,
            "schema_path": SCHEMA_PATH,
            "target": display_path(target),
            "valid": False,
            "errors": errors,
            "warnings": [],
            "stats": {"error_count": len(errors), "warning_count": 0},
        }, exit_code

    for key in sorted(REQUIRED_KEYS - set(payload)):
        add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not is_rfc3339_datetime(generated_at):
        add_error(errors, code="INVALID_GENERATED_AT", message="generated_at must be an RFC3339 date-time")

    enabled_facets = validate_subject(payload, errors)
    validate_canonical_store(payload, errors)
    previous_run_ids = validate_scoring_policy(payload, errors)
    validate_counts(payload, errors)
    validate_facet_scores(payload, enabled_facets, errors)
    validate_lead_scores(payload, enabled_facets, errors)
    validate_next_action(payload, enabled_facets, previous_run_ids, errors)
    validate_deferred(payload, errors)
    validate_selection_explanation_contract(payload, errors)
    validate_warning_error_lists(payload, errors)

    next_action = payload.get("next_action")
    facet_scores = payload.get("facet_scores")
    if isinstance(next_action, dict) and isinstance(facet_scores, list):
        selected_facets = [item for item in facet_scores if isinstance(item, dict) and item.get("selected") is True]
        supporting_facets = [
            item for item in facet_scores if isinstance(item, dict) and item.get("supporting_facet") is True
        ]
        selected_object_ref = next_action.get("selected_object_ref")
        if selected_object_ref is None:
            if len(selected_facets) != 1:
                add_error(
                    errors,
                    code="NEXT_ACTION_MISMATCH",
                    message="next_action.selected_facet must correspond to exactly one selected facet_scores entry",
                )
            if supporting_facets:
                add_error(
                    errors,
                    code="NEXT_ACTION_MISMATCH",
                    message="facet-only next actions must not mark supporting facet_scores entries",
                )
            if len(selected_facets) == 1:
                selected_facet = selected_facets[0].get("facet")
                if next_action.get("selected_facet") != selected_facet:
                    add_error(errors, code="NEXT_ACTION_MISMATCH", message="next_action.selected_facet must match the selected facet_scores entry")
        else:
            if selected_facets:
                add_error(
                    errors,
                    code="NEXT_ACTION_MISMATCH",
                    message="facet-lead next actions must not mark selected facet_scores entries",
                )
            if len(supporting_facets) != 1:
                add_error(
                    errors,
                    code="NEXT_ACTION_MISMATCH",
                    message="facet-lead next actions must mark exactly one supporting facet_scores entry",
                )
            if len(supporting_facets) == 1:
                supporting_facet = supporting_facets[0].get("facet")
                if next_action.get("selected_facet") != supporting_facet:
                    add_error(errors, code="NEXT_ACTION_MISMATCH", message="next_action.selected_facet must match the supporting facet_scores entry")
        if next_action.get("action_kind") == "facet_bootstrap" and payload.get("counts", {}).get("gather_runs_considered") not in (0, None):
            add_error(errors, code="NEXT_ACTION_MISMATCH", message="facet_bootstrap next actions require zero prior gather runs")

    counts = payload.get("counts")
    if isinstance(counts, dict):
        if isinstance(payload.get("facet_scores"), list) and counts.get("facet_candidates") != len(payload["facet_scores"]):
            add_error(errors, code="COUNT_MISMATCH", message="counts.facet_candidates must equal len(facet_scores)")
        if isinstance(payload.get("lead_scores"), list) and counts.get("lead_candidates") != len(payload["lead_scores"]):
            add_error(errors, code="COUNT_MISMATCH", message="counts.lead_candidates must equal len(lead_scores)")
        if isinstance(payload.get("deferred"), list) and counts.get("deferred_candidates") != len(payload["deferred"]):
            add_error(errors, code="COUNT_MISMATCH", message="counts.deferred_candidates must equal len(deferred)")
        if isinstance(payload.get("lead_scores"), list):
            productive_count = sum(
                1
                for item in payload["lead_scores"]
                if isinstance(item, dict)
                and isinstance(item.get("score"), (int, float))
                and not isinstance(item.get("score"), bool)
                and float(item["score"]) > 0.0
            )
            if counts.get("productive_leads") != productive_count:
                add_error(errors, code="COUNT_MISMATCH", message="counts.productive_leads must equal the number of lead_scores with score > 0")

    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "schema_path": SCHEMA_PATH,
        "target": display_path(target),
        "valid": not errors,
        "errors": errors,
        "warnings": [],
        "stats": {
            "error_count": len(errors),
            "warning_count": 0,
            "facet_score_count": len(payload.get("facet_scores", [])) if isinstance(payload.get("facet_scores"), list) else 0,
            "lead_score_count": len(payload.get("lead_scores", [])) if isinstance(payload.get("lead_scores"), list) else 0,
            "deferred_count": len(payload.get("deferred", [])) if isinstance(payload.get("deferred"), list) else 0,
        },
    }
    return report, EXIT_PASS if not errors else EXIT_VALIDATION_FAILED


def main() -> int:
    args = parse_args()
    target = Path(args.target).expanduser()
    report, exit_code = validate_candidate_feedback_plan(target)
    if getattr(args, "report_json", None):
        write_json(Path(args.report_json), report)
    if getattr(args, "report_text", None):
        write_text(Path(args.report_text), render_text_report(report))
    if getattr(args, "format", "json") == "text":
        sys.stdout.write(render_text_report(report))
    else:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
