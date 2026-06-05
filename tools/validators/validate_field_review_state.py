#!/usr/bin/env python3
"""Validate field-level review-state JSON documents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    EXIT_INPUT_UNAVAILABLE,
    EXIT_PASS,
    EXIT_VALIDATION_FAILED,
    add_report_args,
    display_path,
    emit_report,
    is_rfc3339_datetime,
    render_text_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.field_review_state_contract import (  # noqa: E402
    EVIDENCE_LOCATOR_REF_PREFIX,
    EVIDENCE_TYPES,
    FIELD_REVIEW_STATES,
    RECORD_REVIEW_STATES,
    SCHEMA_VERSION,
)
from tools.common.source_adapter_contract import STRUCTURED_DATA_FORMATS  # noqa: E402


VALIDATOR_NAME = "field_review_state"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/field_review_state.schema.json"
FIXTURE_PATH = (
    "tests/fixtures/validators/field_review_state/valid_all_states/inputs/field_review_state.json"
)

ENTRY_ID_PATTERN = re.compile(r"^frs:[a-z0-9][a-z0-9._:-]*$")

REQUIRED_KEYS = {"schema_version", "record_locator", "field_reviews"}
OPTIONAL_KEYS = {"record_review_context"}

RECORD_LOCATOR_REQUIRED_KEYS = {
    "record_family",
    "relative_path",
    "source_filename",
    "structured_format",
    "record_locator",
}
RECORD_LOCATOR_OPTIONAL_KEYS = {"workspace_id"}

RECORD_REVIEW_CONTEXT_REQUIRED_KEYS = {"review_state", "reviewed_by", "reviewed_at"}

FIELD_REVIEW_REQUIRED_KEYS = {
    "entry_id",
    "field_path",
    "state",
    "reviewed_by",
    "reviewed_at",
    "value_fingerprint",
    "evidence_ref",
    "supersedes_entry_id",
    "demotes_entry_id",
    "note",
    "tags",
}

EVIDENCE_REF_REQUIRED_KEYS = {"evidence_type", "reference", "excerpt_locator", "evidence_note"}
EVIDENCE_REF_OPTIONAL_KEYS = {"evidence_locator_ref"}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one field-review-state JSON document.",
        epilog=(
            "Reads the target file and writes validation output to stdout.\n"
            "Optional --report-json/--report-text paths are created atomically.\n\n"
            f"Schema: {SCHEMA_PATH}\n"
            "Example:\n"
            f"  python3 tools/validators/validate_field_review_state.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the field-review-state JSON document to validate.")
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
        payload = json.loads(
            raw_text,
            object_pairs_hook=no_duplicate_object_pairs,
            parse_constant=reject_json_constant,
        )
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


def validate_nonblank_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str = "INVALID_STRING",
) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")


def validate_nullable_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    code: str = "INVALID_NULLABLE_STRING",
) -> None:
    value = payload.get(field)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        add_error(errors, code=code, message=f"{field} must be null or a non-blank string")


def validate_enum(
    payload: dict[str, Any],
    field: str,
    allowed_values: set[str],
    errors: list[dict[str, Any]],
    *,
    code: str,
) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or value not in allowed_values:
        add_error(errors, code=code, message=f"{field} must be one of: {', '.join(sorted(allowed_values))}")


def validate_string_array(payload: dict[str, Any], field: str, errors: list[dict[str, Any]]) -> None:
    value = payload.get(field)
    if not isinstance(value, list):
        add_error(errors, code="FIELD_NOT_ARRAY", message=f"{field} must be an array")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code="INVALID_ARRAY_ITEM", message=f"{field}[{index}] must be a non-blank string")
            return


def parse_timestamp(value: str) -> datetime:
    parseable = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(parseable)


def validate_record_locator(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    locator = payload.get("record_locator")
    if not isinstance(locator, dict):
        add_error(errors, code="RECORD_LOCATOR_NOT_OBJECT", message="record_locator must be an object")
        return
    unknown_keys = sorted(set(locator) - (RECORD_LOCATOR_REQUIRED_KEYS | RECORD_LOCATOR_OPTIONAL_KEYS))
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_RECORD_LOCATOR_FIELD", message=f"unexpected record_locator field: {key}")
    for key in sorted(RECORD_LOCATOR_REQUIRED_KEYS):
        if key not in locator:
            add_error(errors, code="MISSING_RECORD_LOCATOR_KEY", message=f"missing required record_locator key: {key}")
    for key in sorted(RECORD_LOCATOR_REQUIRED_KEYS | RECORD_LOCATOR_OPTIONAL_KEYS):
        if key in locator:
            validate_nonblank_string(locator, key, errors)
    if "structured_format" in locator:
        validate_enum(locator, "structured_format", STRUCTURED_DATA_FORMATS, errors, code="INVALID_STRUCTURED_FORMAT")


def validate_record_review_context(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    context = payload.get("record_review_context")
    if context is None:
        return
    if not isinstance(context, dict):
        add_error(errors, code="RECORD_REVIEW_CONTEXT_NOT_OBJECT", message="record_review_context must be an object")
        return
    unknown_keys = sorted(set(context) - RECORD_REVIEW_CONTEXT_REQUIRED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_RECORD_REVIEW_CONTEXT_FIELD", message=f"unexpected record_review_context field: {key}")
    for key in sorted(RECORD_REVIEW_CONTEXT_REQUIRED_KEYS):
        if key not in context:
            add_error(errors, code="MISSING_RECORD_REVIEW_CONTEXT_KEY", message=f"missing required record_review_context key: {key}")
    if "review_state" in context:
        validate_enum(context, "review_state", RECORD_REVIEW_STATES, errors, code="INVALID_RECORD_REVIEW_STATE")
    if "reviewed_by" in context:
        validate_nonblank_string(context, "reviewed_by", errors)
    reviewed_at = context.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not is_rfc3339_datetime(reviewed_at):
        add_error(errors, code="INVALID_RECORD_REVIEWED_AT", message="reviewed_at must be an RFC3339 datetime")


def validate_evidence_ref(entry: dict[str, Any], errors: list[dict[str, Any]], *, index: int) -> None:
    evidence_ref = entry.get("evidence_ref")
    if not isinstance(evidence_ref, dict):
        add_error(errors, code="EVIDENCE_REF_NOT_OBJECT", message=f"field_reviews[{index}].evidence_ref must be an object")
        return
    unknown_keys = sorted(set(evidence_ref) - (EVIDENCE_REF_REQUIRED_KEYS | EVIDENCE_REF_OPTIONAL_KEYS))
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_EVIDENCE_REF_FIELD", message=f"unexpected evidence_ref field: {key}")
    for key in sorted(EVIDENCE_REF_REQUIRED_KEYS):
        if key not in evidence_ref:
            add_error(errors, code="MISSING_EVIDENCE_REF_KEY", message=f"missing required evidence_ref key: {key}")
    if "evidence_type" in evidence_ref:
        validate_enum(evidence_ref, "evidence_type", EVIDENCE_TYPES, errors, code="INVALID_EVIDENCE_TYPE")
    if "reference" in evidence_ref:
        validate_nonblank_string(evidence_ref, "reference", errors)
    if "excerpt_locator" in evidence_ref:
        validate_nullable_string(evidence_ref, "excerpt_locator", errors)
    if "evidence_note" in evidence_ref:
        validate_nullable_string(evidence_ref, "evidence_note", errors)
    if "evidence_locator_ref" in evidence_ref:
        value = evidence_ref.get("evidence_locator_ref")
        if value is not None and (not isinstance(value, str) or not value.startswith(EVIDENCE_LOCATOR_REF_PREFIX) or not value.strip()):
            add_error(
                errors,
                code="INVALID_EVIDENCE_LOCATOR_REF",
                message=f"field_reviews[{index}].evidence_ref.evidence_locator_ref must be null or start with {EVIDENCE_LOCATOR_REF_PREFIX}",
            )


def validate_field_reviews(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    field_reviews = payload.get("field_reviews")
    if not isinstance(field_reviews, list):
        add_error(errors, code="FIELD_REVIEWS_NOT_ARRAY", message="field_reviews must be an array")
        return
    if not field_reviews:
        add_error(errors, code="FIELD_REVIEWS_EMPTY", message="field_reviews must not be empty")
        return

    seen_entry_ids: dict[str, dict[str, Any]] = {}
    last_timestamp_by_field: dict[str, datetime] = {}

    for index, entry in enumerate(field_reviews):
        if not isinstance(entry, dict):
            add_error(errors, code="FIELD_REVIEW_NOT_OBJECT", message=f"field_reviews[{index}] must be an object")
            return
        unknown_keys = sorted(set(entry) - FIELD_REVIEW_REQUIRED_KEYS)
        for key in unknown_keys:
            add_error(errors, code="UNKNOWN_FIELD_REVIEW_FIELD", message=f"unexpected field_reviews field: {key}")
        for key in sorted(FIELD_REVIEW_REQUIRED_KEYS):
            if key not in entry:
                add_error(errors, code="MISSING_FIELD_REVIEW_KEY", message=f"missing required field_reviews key: {key}")
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not ENTRY_ID_PATTERN.fullmatch(entry_id):
            add_error(errors, code="INVALID_ENTRY_ID", message=f"field_reviews[{index}].entry_id must match ^frs:[a-z0-9][a-z0-9._:-]*$")
        elif entry_id in seen_entry_ids:
            add_error(errors, code="DUPLICATE_ENTRY_ID", message=f"duplicate field review entry_id: {entry_id}")
        if "field_path" in entry:
            validate_nonblank_string(entry, "field_path", errors)
        if "state" in entry:
            validate_enum(entry, "state", FIELD_REVIEW_STATES, errors, code="INVALID_FIELD_REVIEW_STATE")
        if "reviewed_by" in entry:
            validate_nonblank_string(entry, "reviewed_by", errors)
        reviewed_at = entry.get("reviewed_at")
        parsed_reviewed_at: datetime | None = None
        if not isinstance(reviewed_at, str) or not is_rfc3339_datetime(reviewed_at):
            add_error(errors, code="INVALID_REVIEWED_AT", message=f"field_reviews[{index}].reviewed_at must be an RFC3339 datetime")
        else:
            parsed_reviewed_at = parse_timestamp(reviewed_at)
        if "value_fingerprint" in entry:
            validate_nonblank_string(entry, "value_fingerprint", errors)
        if "note" in entry:
            validate_nullable_string(entry, "note", errors)
        if "supersedes_entry_id" in entry:
            validate_nullable_string(entry, "supersedes_entry_id", errors)
        if "demotes_entry_id" in entry:
            validate_nullable_string(entry, "demotes_entry_id", errors)
        if "tags" in entry:
            validate_string_array(entry, "tags", errors)
        validate_evidence_ref(entry, errors, index=index)

        if not isinstance(entry_id, str) or entry_id in seen_entry_ids:
            continue
        field_path = entry.get("field_path")
        state = entry.get("state")
        supersedes_entry_id = entry.get("supersedes_entry_id")
        demotes_entry_id = entry.get("demotes_entry_id")
        if isinstance(field_path, str) and parsed_reviewed_at is not None:
            last_timestamp = last_timestamp_by_field.get(field_path)
            if last_timestamp is not None and parsed_reviewed_at < last_timestamp:
                add_error(
                    errors,
                    code="FIELD_REVIEW_HISTORY_OUT_OF_ORDER",
                    message=f"field_reviews[{index}] reviewed_at moves backward for field_path {field_path}",
                )
            else:
                last_timestamp_by_field[field_path] = parsed_reviewed_at

        if state == "superseded":
            if not isinstance(supersedes_entry_id, str) or not supersedes_entry_id.strip():
                add_error(errors, code="SUPERSEDES_REFERENCE_REQUIRED", message="superseded field review entries must reference supersedes_entry_id")
            if demotes_entry_id is not None:
                add_error(errors, code="SUPERSEDED_DEMOTION_CONFLICT", message="superseded field review entries must not set demotes_entry_id")
        elif state == "demoted":
            if not isinstance(demotes_entry_id, str) or not demotes_entry_id.strip():
                add_error(errors, code="DEMOTION_REFERENCE_REQUIRED", message="demoted field review entries must reference demotes_entry_id")
            if supersedes_entry_id is not None:
                add_error(errors, code="DEMOTED_SUPERSESSION_CONFLICT", message="demoted field review entries must not set supersedes_entry_id")
        else:
            if supersedes_entry_id is not None:
                add_error(errors, code="UNEXPECTED_SUPERSEDES_REFERENCE", message=f"{state} field review entries must not set supersedes_entry_id")
            if demotes_entry_id is not None:
                add_error(errors, code="UNEXPECTED_DEMOTION_REFERENCE", message=f"{state} field review entries must not set demotes_entry_id")

        for ref_field, ref_value, code in (
            ("supersedes_entry_id", supersedes_entry_id, "SUPERSEDES_REFERENCE_INVALID"),
            ("demotes_entry_id", demotes_entry_id, "DEMOTION_REFERENCE_INVALID"),
        ):
            if ref_value is None:
                continue
            if not isinstance(ref_value, str) or not ref_value.strip():
                continue
            referenced = seen_entry_ids.get(ref_value)
            if referenced is None:
                add_error(errors, code=code, message=f"{ref_field} must reference an earlier field review entry_id")
                continue
            if referenced["field_path"] != field_path:
                add_error(errors, code="FIELD_REFERENCE_MISMATCH", message=f"{ref_field} must reference an earlier entry on the same field_path")
                continue
            if parsed_reviewed_at is not None and parsed_reviewed_at < referenced["reviewed_at"]:
                add_error(errors, code="REFERENCED_ENTRY_NEWER_THAN_EVENT", message=f"{ref_field} cannot point to a later reviewed_at timestamp")

        if isinstance(entry_id, str):
            seen_entry_ids[entry_id] = {
                "field_path": field_path,
                "reviewed_at": parsed_reviewed_at,
            }


def validate_field_review_state(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []

    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code

    counts["inspected"] = 1

    unknown_keys = sorted(set(payload) - (REQUIRED_KEYS | OPTIONAL_KEYS))
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")

    validate_record_locator(payload, errors)
    validate_record_review_context(payload, errors)
    validate_field_reviews(payload, errors)

    if errors:
        counts["rejected"] = 1
        return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_VALIDATION_FAILED

    counts["accepted"] = 1
    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_field_review_state(target)

    status = "pass" if exit_code == EXIT_PASS else "fail"
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts={
            "report_json": display_path(args.report_json),
            "report_text": display_path(args.report_text),
        },
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or (display_path(args.target) or args.target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    print(render_text_report(report), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
