#!/usr/bin/env python3
"""Validate standalone evidence locator/highlight JSON documents."""

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
        emit_report,
        render_text_report,
    )
except ModuleNotFoundError:
    from tools.validators.common import (  # type: ignore
        EXIT_INPUT_UNAVAILABLE,
        EXIT_PASS,
        EXIT_VALIDATION_FAILED,
        add_report_args,
        display_path,
        emit_report,
        render_text_report,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.evidence_locator_contract import (
    EVIDENCE_LOCATOR_ID_PREFIX,
    HIGHLIGHT_KINDS,
    REDACTION_POSTURES,
    SCHEMA_VERSION,
    SPAN_KINDS,
)
from tools.common.source_adapter_contract import STRUCTURED_DATA_FORMATS
from tools.source_db_tools.rights_retention import QUOTE_ELIGIBILITY_VALUES, rights_postures


VALIDATOR_NAME = "evidence_locator"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/evidence_locator.schema.json"
FIXTURE_PATH = "tests/fixtures/validators/evidence_locator/valid_page_span/inputs/evidence_locator.json"

LOCATOR_ID_PATTERN = re.compile(r"^evl:[a-z0-9][a-z0-9._:-]*$")

REQUIRED_KEYS = {"schema_version", "evidence_locator_id", "record_locator", "span", "highlight"}
RECORD_LOCATOR_REQUIRED_KEYS = {"record_family", "relative_path", "source_filename", "record_locator"}
RECORD_LOCATOR_OPTIONAL_KEYS = {"workspace_id", "structured_format"}
SPAN_REQUIRED_KEYS = {
    "span_kind",
    "page_start",
    "page_end",
    "line_start",
    "line_end",
    "byte_start",
    "byte_end",
    "field_path",
    "metadata_fields",
    "locator_note",
}
HIGHLIGHT_REQUIRED_KEYS = {
    "highlight_kind",
    "rights_posture",
    "quote_eligibility",
    "redaction_posture",
    "operator_excerpt_text",
    "public_excerpt_text",
    "public_summary",
    "highlight_note",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one evidence-locator JSON document.",
        epilog=(
            "Reads the target file and writes validation output to stdout.\n"
            "Optional --report-json/--report-text paths are created atomically.\n\n"
            f"Schema: {SCHEMA_PATH}\n"
            f"Example:\n  python3 tools/validators/validate_evidence_locator.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the evidence-locator JSON document to validate.")
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


def validate_nonblank_string(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be a non-blank string")
        return None
    return value


def validate_nullable_string(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{field} must be null or a non-blank string")
        return None
    return value


def validate_nullable_int(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str, minimum: int) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        add_error(errors, code=code, message=f"{field} must be null or an integer >= {minimum}")
        return None
    return value


def validate_string_array(payload: dict[str, Any], field: str, errors: list[dict[str, Any]], *, code: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{field} must be an array")
        return []
    accepted: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{field}[{index}] must be a non-blank string")
            continue
        accepted.append(item)
    return accepted


def validate_record_locator(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    locator = payload.get("record_locator")
    if not isinstance(locator, dict):
        add_error(errors, code="RECORD_LOCATOR_NOT_OBJECT", message="record_locator must be an object")
        return
    allowed_keys = RECORD_LOCATOR_REQUIRED_KEYS | RECORD_LOCATOR_OPTIONAL_KEYS
    for key in sorted(set(locator) - allowed_keys):
        add_error(errors, code="UNKNOWN_RECORD_LOCATOR_FIELD", message=f"unexpected record_locator field: {key}")
    for key in sorted(RECORD_LOCATOR_REQUIRED_KEYS):
        if key not in locator:
            add_error(errors, code="MISSING_RECORD_LOCATOR_KEY", message=f"missing required record_locator key: {key}")
    for key in ("workspace_id", "record_family", "relative_path", "source_filename"):
        if key in locator:
            validate_nonblank_string(locator, key, errors, code="INVALID_RECORD_LOCATOR_FIELD")
    if "record_locator" in locator:
        value = locator.get("record_locator")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            add_error(errors, code="INVALID_RECORD_LOCATOR_FIELD", message="record_locator.record_locator must be null or a non-blank string")
    if "structured_format" in locator:
        value = locator.get("structured_format")
        if value is not None and value not in STRUCTURED_DATA_FORMATS:
            add_error(
                errors,
                code="INVALID_STRUCTURED_FORMAT",
                message=f"record_locator.structured_format must be one of: {', '.join(sorted(STRUCTURED_DATA_FORMATS))}",
            )


def validate_span(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    span = payload.get("span")
    if not isinstance(span, dict):
        add_error(errors, code="SPAN_NOT_OBJECT", message="span must be an object")
        return
    for key in sorted(set(span) - SPAN_REQUIRED_KEYS):
        add_error(errors, code="UNKNOWN_SPAN_FIELD", message=f"unexpected span field: {key}")
    for key in sorted(SPAN_REQUIRED_KEYS):
        if key not in span:
            add_error(errors, code="MISSING_SPAN_KEY", message=f"missing required span key: {key}")

    span_kind = validate_nonblank_string(span, "span_kind", errors, code="INVALID_SPAN_KIND")
    if span_kind is not None and span_kind not in SPAN_KINDS:
        add_error(errors, code="INVALID_SPAN_KIND", message=f"span_kind must be one of: {', '.join(sorted(SPAN_KINDS))}")
        span_kind = None

    page_start = validate_nullable_int(span, "page_start", errors, code="INVALID_PAGE_SPAN", minimum=1)
    page_end = validate_nullable_int(span, "page_end", errors, code="INVALID_PAGE_SPAN", minimum=1)
    line_start = validate_nullable_int(span, "line_start", errors, code="INVALID_LINE_SPAN", minimum=1)
    line_end = validate_nullable_int(span, "line_end", errors, code="INVALID_LINE_SPAN", minimum=1)
    byte_start = validate_nullable_int(span, "byte_start", errors, code="INVALID_BYTE_RANGE", minimum=0)
    byte_end = validate_nullable_int(span, "byte_end", errors, code="INVALID_BYTE_RANGE", minimum=0)
    field_path = validate_nullable_string(span, "field_path", errors, code="INVALID_FIELD_PATH")
    metadata_fields = validate_string_array(span, "metadata_fields", errors, code="INVALID_METADATA_FIELDS")
    validate_nullable_string(span, "locator_note", errors, code="INVALID_LOCATOR_NOTE")

    if page_start is not None and page_end is not None and page_start > page_end:
        add_error(errors, code="INVALID_PAGE_SPAN", message="page_start must be <= page_end")
    if line_start is not None and line_end is not None and line_start > line_end:
        add_error(errors, code="INVALID_LINE_SPAN", message="line_start must be <= line_end")
    if byte_start is not None and byte_end is not None and byte_start > byte_end:
        add_error(errors, code="INVALID_BYTE_RANGE", message="byte_start must be <= byte_end")

    if span_kind == "page_span":
        if page_start is None or page_end is None:
            add_error(errors, code="PAGE_SPAN_REQUIRED", message="page_span requires page_start and page_end")
    elif span_kind == "line_span":
        if line_start is None or line_end is None:
            add_error(errors, code="LINE_SPAN_REQUIRED", message="line_span requires line_start and line_end")
    elif span_kind == "byte_range":
        if byte_start is None or byte_end is None:
            add_error(errors, code="BYTE_RANGE_REQUIRED", message="byte_range requires byte_start and byte_end")
    elif span_kind == "structured_field":
        if field_path is None:
            add_error(errors, code="FIELD_PATH_REQUIRED", message="structured_field requires field_path")
    elif span_kind == "metadata_only":
        if not metadata_fields:
            add_error(errors, code="METADATA_FIELDS_REQUIRED", message="metadata_only requires at least one metadata_fields entry")


def validate_highlight(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    highlight = payload.get("highlight")
    if not isinstance(highlight, dict):
        add_error(errors, code="HIGHLIGHT_NOT_OBJECT", message="highlight must be an object")
        return
    for key in sorted(set(highlight) - HIGHLIGHT_REQUIRED_KEYS):
        add_error(errors, code="UNKNOWN_HIGHLIGHT_FIELD", message=f"unexpected highlight field: {key}")
    for key in sorted(HIGHLIGHT_REQUIRED_KEYS):
        if key not in highlight:
            add_error(errors, code="MISSING_HIGHLIGHT_KEY", message=f"missing required highlight key: {key}")

    highlight_kind = validate_nonblank_string(highlight, "highlight_kind", errors, code="INVALID_HIGHLIGHT_KIND")
    if highlight_kind is not None and highlight_kind not in HIGHLIGHT_KINDS:
        add_error(errors, code="INVALID_HIGHLIGHT_KIND", message=f"highlight_kind must be one of: {', '.join(sorted(HIGHLIGHT_KINDS))}")

    rights_posture = validate_nonblank_string(highlight, "rights_posture", errors, code="INVALID_RIGHTS_POSTURE")
    known_rights_postures = rights_postures()
    if rights_posture is not None and rights_posture not in known_rights_postures:
        add_error(
            errors,
            code="INVALID_RIGHTS_POSTURE",
            message=f"rights_posture must be one of: {', '.join(sorted(known_rights_postures))}",
        )

    quote_eligibility = validate_nonblank_string(highlight, "quote_eligibility", errors, code="INVALID_QUOTE_ELIGIBILITY")
    if quote_eligibility is not None and quote_eligibility not in QUOTE_ELIGIBILITY_VALUES:
        add_error(
            errors,
            code="INVALID_QUOTE_ELIGIBILITY",
            message=f"quote_eligibility must be one of: {', '.join(sorted(QUOTE_ELIGIBILITY_VALUES))}",
        )

    redaction_posture = validate_nonblank_string(highlight, "redaction_posture", errors, code="INVALID_REDACTION_POSTURE")
    if redaction_posture is not None and redaction_posture not in REDACTION_POSTURES:
        add_error(
            errors,
            code="INVALID_REDACTION_POSTURE",
            message=f"redaction_posture must be one of: {', '.join(sorted(REDACTION_POSTURES))}",
        )

    operator_excerpt_text = validate_nullable_string(highlight, "operator_excerpt_text", errors, code="INVALID_EXCERPT_TEXT")
    public_excerpt_text = validate_nullable_string(highlight, "public_excerpt_text", errors, code="INVALID_EXCERPT_TEXT")
    public_summary = validate_nullable_string(highlight, "public_summary", errors, code="INVALID_PUBLIC_SUMMARY")
    validate_nullable_string(highlight, "highlight_note", errors, code="INVALID_HIGHLIGHT_NOTE")

    if public_excerpt_text is not None:
        if redaction_posture != "public_text_allowed":
            add_error(
                errors,
                code="PUBLIC_EXCERPT_REDACTION_CONFLICT",
                message="public_excerpt_text is only allowed when redaction_posture is public_text_allowed",
            )
        if quote_eligibility not in {"eligible", "limited_excerpt"}:
            add_error(
                errors,
                code="PUBLIC_EXCERPT_QUOTE_CONFLICT",
                message="public_excerpt_text requires quote_eligibility of eligible or limited_excerpt",
            )
    if redaction_posture == "public_summary_only" and public_summary is None:
        add_error(
            errors,
            code="PUBLIC_SUMMARY_REQUIRED",
            message="public_summary_only requires a non-null public_summary",
        )
    if redaction_posture == "private_only" and public_excerpt_text is not None:
        add_error(errors, code="PRIVATE_TEXT_LEAK", message="private_only evidence must not expose public_excerpt_text")
    if highlight_kind == "exact_quote" and operator_excerpt_text is None:
        add_error(errors, code="OPERATOR_EXCERPT_REQUIRED", message="exact_quote highlights require operator_excerpt_text")


def validate_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")
    for key in sorted(set(payload) - REQUIRED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    evidence_locator_id = payload.get("evidence_locator_id")
    if not isinstance(evidence_locator_id, str) or not LOCATOR_ID_PATTERN.fullmatch(evidence_locator_id):
        add_error(
            errors,
            code="INVALID_EVIDENCE_LOCATOR_ID",
            message=f"evidence_locator_id must start with {EVIDENCE_LOCATOR_ID_PREFIX} and match ^evl:[a-z0-9][a-z0-9._:-]*$",
        )

    validate_record_locator(payload, errors)
    validate_span(payload, errors)
    validate_highlight(payload, errors)
    return errors


def validate_evidence_locator(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, load_exit = load_json_object(target)
    if payload is not None and load_exit == EXIT_PASS:
        errors.extend(validate_payload(payload))

    if load_exit == EXIT_INPUT_UNAVAILABLE:
        exit_code = EXIT_INPUT_UNAVAILABLE
    elif errors:
        exit_code = EXIT_VALIDATION_FAILED
    else:
        exit_code = EXIT_PASS

    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "target": display_path(str(target)),
        "status": "pass" if exit_code == EXIT_PASS else "fail",
        "counts": {
            "inspected": 0 if load_exit == EXIT_INPUT_UNAVAILABLE else 1,
            "accepted": 1 if exit_code == EXIT_PASS else 0,
            "rejected": 1 if exit_code == EXIT_VALIDATION_FAILED else 0,
            "deferred": 0,
        },
        "errors": errors,
        "warnings": [],
        "output_artifacts": {},
        "scenario": None,
    }
    return report, exit_code


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    report, exit_code = validate_evidence_locator(target)
    report["scenario"] = args.scenario
    if args.target_id:
        report["target"] = args.target_id
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=report["counts"],
        errors=report["errors"],
        output_artifacts=report["output_artifacts"],
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=report["scenario"],
        status=report["status"],
        target=report["target"],
        validator=VALIDATOR_NAME,
        warnings=report["warnings"],
    )
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
