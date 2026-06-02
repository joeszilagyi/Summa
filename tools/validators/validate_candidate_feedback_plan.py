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
    APPEND_ONLY_TARGETS,
    PROPOSAL_KINDS,
    SCHEMA_VERSION,
    TARGET_RECORD_FAMILIES,
)


VALIDATOR_NAME = "candidate_feedback_plan"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/candidate_feedback_plan.schema.json"

OBJECT_REF_PATTERN = re.compile(r"^[a-z_]+:[0-9]+$")
PROVENANCE_REF_PATTERN = re.compile(r"^prov:[a-z0-9-]+$")
EVIDENCE_REF_PATTERN = re.compile(r"^evl:[a-z0-9][a-z0-9._:-]*$")
PROPOSAL_ID_PATTERN = re.compile(r"^cfp:[a-z0-9][a-z0-9._:-]*$")

REQUIRED_KEYS = {
    "schema_version",
    "generated_at",
    "source",
    "counts",
    "proposals",
    "skipped",
    "warnings",
    "errors",
}
SOURCE_REQUIRED_KEYS = {
    "database_name",
    "schema_version",
    "correction_ledger_applied",
    "field_review_state_count",
    "evidence_locator_count",
    "dry_run",
}
COUNT_REQUIRED_KEYS = {
    "earlier_candidates_considered",
    "later_discoveries_considered",
    "proposals_emitted",
    "skipped_targets",
}
PROPOSAL_REQUIRED_KEYS = {
    "proposal_id",
    "rank",
    "proposal_kind",
    "target_record_family",
    "target_object_ref",
    "source_object_refs",
    "append_only_target",
    "score",
    "rationale",
    "preserved_target_provenance_refs",
    "preserved_source_provenance_refs",
    "evidence_locator_refs",
    "evidence_summaries",
    "proposed_changes",
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


def validate_source(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    source = payload.get("source")
    if not isinstance(source, dict):
        add_error(errors, code="SOURCE_NOT_OBJECT", message="source must be an object")
        return
    for key in sorted(SOURCE_REQUIRED_KEYS - set(source)):
        add_error(errors, code="MISSING_SOURCE_KEY", message=f"missing required source key: {key}")
    validate_nonblank_string(source.get("database_name"), field_name="source.database_name", errors=errors, code="INVALID_SOURCE_FIELD")
    if source.get("schema_version") is not None and not isinstance(source.get("schema_version"), int):
        add_error(errors, code="INVALID_SOURCE_FIELD", message="source.schema_version must be null or an integer")
    for field in ("correction_ledger_applied", "dry_run"):
        if not isinstance(source.get(field), bool):
            add_error(errors, code="INVALID_SOURCE_FIELD", message=f"source.{field} must be a boolean")
    for field in ("field_review_state_count", "evidence_locator_count"):
        value = source.get(field)
        if not isinstance(value, int) or value < 0:
            add_error(errors, code="INVALID_SOURCE_FIELD", message=f"source.{field} must be a non-negative integer")


def validate_counts(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        add_error(errors, code="COUNTS_NOT_OBJECT", message="counts must be an object")
        return
    for key in sorted(COUNT_REQUIRED_KEYS - set(counts)):
        add_error(errors, code="MISSING_COUNT_KEY", message=f"missing required counts key: {key}")
    for key in sorted(COUNT_REQUIRED_KEYS):
        value = counts.get(key)
        if not isinstance(value, int) or value < 0:
            add_error(errors, code="INVALID_COUNT_VALUE", message=f"counts.{key} must be a non-negative integer")


def validate_ref_list(
    value: Any,
    *,
    field_name: str,
    pattern: re.Pattern[str],
    errors: list[dict[str, Any]],
    allow_empty: bool = True,
) -> list[str]:
    items = validate_string_list(value, field_name=field_name, errors=errors, code="INVALID_REF_LIST")
    if not allow_empty and not items:
        add_error(errors, code="INVALID_REF_LIST", message=f"{field_name} must not be empty")
    accepted: list[str] = []
    for item in items:
        if not pattern.fullmatch(item):
            add_error(errors, code="INVALID_REF_VALUE", message=f"{field_name} contains invalid reference: {item}")
            continue
        accepted.append(item)
    return accepted


def validate_proposals(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        add_error(errors, code="PROPOSALS_NOT_ARRAY", message="proposals must be an array")
        return
    previous_score: float | None = None
    seen_ids: set[str] = set()
    expected_rank = 1
    for index, proposal in enumerate(proposals):
        label = f"proposals[{index}]"
        if not isinstance(proposal, dict):
            add_error(errors, code="PROPOSAL_NOT_OBJECT", message=f"{label} must be an object")
            continue
        for key in sorted(PROPOSAL_REQUIRED_KEYS - set(proposal)):
            add_error(errors, code="MISSING_PROPOSAL_KEY", message=f"missing required {label} key: {key}")
        proposal_id = validate_nonblank_string(proposal.get("proposal_id"), field_name=f"{label}.proposal_id", errors=errors, code="INVALID_PROPOSAL_ID")
        if proposal_id is not None:
            if not PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
                add_error(errors, code="INVALID_PROPOSAL_ID", message=f"{label}.proposal_id must match ^cfp:[a-z0-9][a-z0-9._:-]*$")
            elif proposal_id in seen_ids:
                add_error(errors, code="DUPLICATE_PROPOSAL_ID", message=f"duplicate proposal_id: {proposal_id}")
            else:
                seen_ids.add(proposal_id)
        rank = proposal.get("rank")
        if not isinstance(rank, int) or rank != expected_rank:
            add_error(errors, code="INVALID_PROPOSAL_RANK", message=f"{label}.rank must equal {expected_rank}")
        expected_rank += 1
        kind = proposal.get("proposal_kind")
        if not isinstance(kind, str) or kind not in PROPOSAL_KINDS:
            add_error(errors, code="INVALID_PROPOSAL_KIND", message=f"{label}.proposal_kind must be one of: {', '.join(sorted(PROPOSAL_KINDS))}")
        family = proposal.get("target_record_family")
        if not isinstance(family, str) or family not in TARGET_RECORD_FAMILIES:
            add_error(errors, code="INVALID_TARGET_RECORD_FAMILY", message=f"{label}.target_record_family must be one of: {', '.join(sorted(TARGET_RECORD_FAMILIES))}")
        target_ref = proposal.get("target_object_ref")
        if not isinstance(target_ref, str) or not OBJECT_REF_PATTERN.fullmatch(target_ref):
            add_error(errors, code="INVALID_OBJECT_REF", message=f"{label}.target_object_ref must match ^[a-z_]+:[0-9]+$")
        validate_ref_list(proposal.get("source_object_refs"), field_name=f"{label}.source_object_refs", pattern=OBJECT_REF_PATTERN, errors=errors, allow_empty=False)
        append_only_target = proposal.get("append_only_target")
        if not isinstance(append_only_target, str) or append_only_target not in APPEND_ONLY_TARGETS:
            add_error(errors, code="INVALID_APPEND_ONLY_TARGET", message=f"{label}.append_only_target must be one of: {', '.join(sorted(APPEND_ONLY_TARGETS))}")
        score = proposal.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= float(score) <= 1.0:
            add_error(errors, code="INVALID_SCORE", message=f"{label}.score must be a number between 0.0 and 1.0")
        else:
            current_score = float(score)
            if previous_score is not None and current_score > previous_score + 1e-9:
                add_error(errors, code="PROPOSAL_ORDER_INVALID", message="proposals must be sorted by descending score")
            previous_score = current_score
        validate_nonblank_string(proposal.get("rationale"), field_name=f"{label}.rationale", errors=errors, code="INVALID_RATIONALE")
        validate_ref_list(proposal.get("preserved_target_provenance_refs"), field_name=f"{label}.preserved_target_provenance_refs", pattern=PROVENANCE_REF_PATTERN, errors=errors)
        validate_ref_list(proposal.get("preserved_source_provenance_refs"), field_name=f"{label}.preserved_source_provenance_refs", pattern=PROVENANCE_REF_PATTERN, errors=errors)
        validate_ref_list(proposal.get("evidence_locator_refs"), field_name=f"{label}.evidence_locator_refs", pattern=EVIDENCE_REF_PATTERN, errors=errors)
        validate_string_list(proposal.get("evidence_summaries"), field_name=f"{label}.evidence_summaries", errors=errors, code="INVALID_EVIDENCE_SUMMARIES")
        if not isinstance(proposal.get("proposed_changes"), dict):
            add_error(errors, code="INVALID_PROPOSED_CHANGES", message=f"{label}.proposed_changes must be an object")


def validate_skipped(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    skipped = payload.get("skipped")
    if not isinstance(skipped, list):
        add_error(errors, code="SKIPPED_NOT_ARRAY", message="skipped must be an array")
        return
    for index, item in enumerate(skipped):
        label = f"skipped[{index}]"
        if not isinstance(item, dict):
            add_error(errors, code="SKIPPED_NOT_OBJECT", message=f"{label} must be an object")
            continue
        target_ref = item.get("target_object_ref")
        if not isinstance(target_ref, str) or not OBJECT_REF_PATTERN.fullmatch(target_ref):
            add_error(errors, code="INVALID_OBJECT_REF", message=f"{label}.target_object_ref must match ^[a-z_]+:[0-9]+$")
        validate_nonblank_string(item.get("reason"), field_name=f"{label}.reason", errors=errors, code="INVALID_SKIPPED_REASON")


def validate_candidate_feedback_plan(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code
    counts["inspected"] = 1

    unknown_keys = sorted(set(payload) - REQUIRED_KEYS - {"validator", "contract_version", "target", "status", "output_artifacts", "scenario"})
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(REQUIRED_KEYS - set(payload)):
        add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not is_rfc3339_datetime(generated_at):
        add_error(errors, code="INVALID_GENERATED_AT", message="generated_at must be an RFC3339 datetime")
    validate_source(payload, errors)
    validate_counts(payload, errors)
    validate_proposals(payload, errors)
    validate_skipped(payload, errors)
    for array_field in ("warnings", "errors"):
        validate_string_list(payload.get(array_field), field_name=array_field, errors=errors, code="INVALID_MESSAGE_ARRAY")

    payload_counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    proposals = payload.get("proposals") if isinstance(payload.get("proposals"), list) else []
    skipped = payload.get("skipped") if isinstance(payload.get("skipped"), list) else []
    if isinstance(payload_counts, dict):
        if payload_counts.get("proposals_emitted") != len(proposals):
            add_error(errors, code="COUNT_MISMATCH", message="counts.proposals_emitted must equal len(proposals)")
        if payload_counts.get("skipped_targets") != len(skipped):
            add_error(errors, code="COUNT_MISMATCH", message="counts.skipped_targets must equal len(skipped)")

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED
    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_candidate_feedback_plan(target)
    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "target": args.target_id or (display_path(args.target) or args.target),
        "status": "pass" if exit_code == EXIT_PASS else "fail",
        "counts": result["counts"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "output_artifacts": {
            "report_json": display_path(args.report_json) if args.report_json else None,
            "report_text": display_path(args.report_text) if args.report_text else None,
        },
        "scenario": args.scenario,
    }
    text_report = render_text_report(report)
    write_json(args.report_json, report)
    write_text(args.report_text, text_report)
    sys.stdout.write(text_report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
