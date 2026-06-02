#!/usr/bin/env python3
"""Validate static page-family query contract JSON artifacts."""

from __future__ import annotations

import argparse
import json
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
        render_text_report,
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.static_page_family_query_contract import (  # noqa: E402
    ALLOWED_INPUT_KINDS,
    ALLOWED_READER_STATES,
    CONTRACT_DOC,
    REQUIRED_PAGE_FAMILIES,
    SCHEMA_VERSION,
)


VALIDATOR_NAME = "static_page_family_query_contract"
CONTRACT_VERSION = "1"
REQUIRED_TOP_LEVEL_KEYS = {"schema_version", "contract_doc", "page_families"}
REQUIRED_FAMILY_KEYS = {
    "page_family",
    "query_contract_id",
    "description",
    "inputs",
    "sparse_state",
    "field_visibility",
    "empty_example",
    "populated_example",
}
REQUIRED_INPUT_KEYS = {
    "input_name",
    "kind",
    "upstream_contract_ref",
    "required",
    "purpose",
}
REQUIRED_VISIBILITY_KEYS = {
    "public_fields",
    "private_fields",
    "conditional_public_fields",
}
REQUIRED_STATE_KEYS = {
    "reader_state",
    "query_status",
    "visible_sections",
    "hidden_sections",
}
REQUIRED_SPARSE_KEYS = {
    "reader_state",
    "trigger_signals",
    "retained_sections",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a static page-family query contract JSON file.")
    parser.add_argument("target", help="Path to the static page-family query contract JSON file.")
    add_report_args(parser)
    return parser.parse_args()


def add_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    line: int | None = None,
    path: str | None = None,
) -> None:
    payload = {"code": code, "line": line, "message": message}
    if path is not None:
        payload["path"] = path
    errors.append(payload)


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


def validate_nonblank_string(value: Any, *, label: str, errors: list[dict[str, Any]], code: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, message=f"{label} must be a non-blank string", path=label)
        return None
    return value


def validate_string_array(value: Any, *, label: str, errors: list[dict[str, Any]], code: str, min_items: int = 0) -> list[str]:
    if not isinstance(value, list):
        add_error(errors, code=code, message=f"{label} must be an array of non-blank strings", path=label)
        return []
    accepted: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=code, message=f"{item_label} must be a non-blank string", path=item_label)
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{label} contains duplicate value: {item}", path=item_label)
            continue
        seen.add(item)
        accepted.append(item)
    if len(accepted) < min_items:
        add_error(errors, code=code, message=f"{label} must contain at least {min_items} item(s)", path=label)
    return accepted


def validate_input(item: Any, *, family_label: str, index: int, errors: list[dict[str, Any]]) -> None:
    label = f"{family_label}.inputs[{index}]"
    if not isinstance(item, dict):
        add_error(errors, code="INPUT_NOT_OBJECT", message=f"{label} must be an object", path=label)
        return
    for key in sorted(REQUIRED_INPUT_KEYS - set(item)):
        add_error(errors, code="MISSING_INPUT_KEY", message=f"missing required {label} key: {key}", path=label)
    kind = validate_nonblank_string(item.get("kind"), label=f"{label}.kind", errors=errors, code="INVALID_INPUT_FIELD")
    if kind is not None and kind not in ALLOWED_INPUT_KINDS:
        add_error(
            errors,
            code="INVALID_INPUT_KIND",
            message=f"{label}.kind must be one of: {', '.join(sorted(ALLOWED_INPUT_KINDS))}",
            path=f"{label}.kind",
        )
    validate_nonblank_string(item.get("input_name"), label=f"{label}.input_name", errors=errors, code="INVALID_INPUT_FIELD")
    validate_nonblank_string(item.get("upstream_contract_ref"), label=f"{label}.upstream_contract_ref", errors=errors, code="INVALID_INPUT_FIELD")
    validate_nonblank_string(item.get("purpose"), label=f"{label}.purpose", errors=errors, code="INVALID_INPUT_FIELD")
    if not isinstance(item.get("required"), bool):
        add_error(errors, code="INVALID_INPUT_FIELD", message=f"{label}.required must be a boolean", path=f"{label}.required")


def validate_sparse_state(value: Any, *, family_label: str, errors: list[dict[str, Any]]) -> None:
    label = f"{family_label}.sparse_state"
    if not isinstance(value, dict):
        add_error(errors, code="SPARSE_STATE_NOT_OBJECT", message=f"{label} must be an object", path=label)
        return
    for key in sorted(REQUIRED_SPARSE_KEYS - set(value)):
        add_error(errors, code="MISSING_SPARSE_STATE_KEY", message=f"missing required {label} key: {key}", path=label)
    reader_state = validate_nonblank_string(value.get("reader_state"), label=f"{label}.reader_state", errors=errors, code="INVALID_SPARSE_STATE")
    if reader_state is not None and reader_state != "sparse":
        add_error(errors, code="INVALID_SPARSE_STATE", message=f"{label}.reader_state must equal sparse", path=f"{label}.reader_state")
    validate_string_array(value.get("trigger_signals"), label=f"{label}.trigger_signals", errors=errors, code="INVALID_SPARSE_STATE", min_items=1)
    validate_string_array(value.get("retained_sections"), label=f"{label}.retained_sections", errors=errors, code="INVALID_SPARSE_STATE", min_items=1)


def validate_state_example(value: Any, *, family_label: str, example_name: str, expected_state: str, errors: list[dict[str, Any]]) -> None:
    label = f"{family_label}.{example_name}"
    if not isinstance(value, dict):
        add_error(errors, code="STATE_EXAMPLE_NOT_OBJECT", message=f"{label} must be an object", path=label)
        return
    for key in sorted(REQUIRED_STATE_KEYS - set(value)):
        add_error(errors, code="MISSING_STATE_EXAMPLE_KEY", message=f"missing required {label} key: {key}", path=label)
    reader_state = validate_nonblank_string(value.get("reader_state"), label=f"{label}.reader_state", errors=errors, code="INVALID_STATE_EXAMPLE")
    if reader_state is not None:
        if reader_state not in ALLOWED_READER_STATES:
            add_error(
                errors,
                code="INVALID_STATE_EXAMPLE",
                message=f"{label}.reader_state must be one of: {', '.join(sorted(ALLOWED_READER_STATES))}",
                path=f"{label}.reader_state",
            )
        elif reader_state != expected_state:
            add_error(
                errors,
                code="INVALID_STATE_EXAMPLE",
                message=f"{label}.reader_state must equal {expected_state}",
                path=f"{label}.reader_state",
            )
    validate_nonblank_string(value.get("query_status"), label=f"{label}.query_status", errors=errors, code="INVALID_STATE_EXAMPLE")
    validate_string_array(value.get("visible_sections"), label=f"{label}.visible_sections", errors=errors, code="INVALID_STATE_EXAMPLE", min_items=1)
    validate_string_array(value.get("hidden_sections"), label=f"{label}.hidden_sections", errors=errors, code="INVALID_STATE_EXAMPLE")


def validate_field_visibility(value: Any, *, family_label: str, errors: list[dict[str, Any]]) -> None:
    label = f"{family_label}.field_visibility"
    if not isinstance(value, dict):
        add_error(errors, code="FIELD_VISIBILITY_NOT_OBJECT", message=f"{label} must be an object", path=label)
        return
    for key in sorted(REQUIRED_VISIBILITY_KEYS - set(value)):
        add_error(errors, code="MISSING_FIELD_VISIBILITY_KEY", message=f"missing required {label} key: {key}", path=label)
    public_fields = set(
        validate_string_array(value.get("public_fields"), label=f"{label}.public_fields", errors=errors, code="INVALID_FIELD_VISIBILITY", min_items=1)
    )
    private_fields = set(
        validate_string_array(value.get("private_fields"), label=f"{label}.private_fields", errors=errors, code="INVALID_FIELD_VISIBILITY", min_items=1)
    )
    conditional_public_fields = set(
        validate_string_array(
            value.get("conditional_public_fields"),
            label=f"{label}.conditional_public_fields",
            errors=errors,
            code="INVALID_FIELD_VISIBILITY",
        )
    )
    for overlap in sorted(public_fields & private_fields):
        add_error(
            errors,
            code="VISIBILITY_FIELD_OVERLAP",
            message=f"{label} reuses field in public_fields and private_fields: {overlap}",
            path=label,
        )
    for overlap in sorted(public_fields & conditional_public_fields):
        add_error(
            errors,
            code="VISIBILITY_FIELD_OVERLAP",
            message=f"{label} reuses field in public_fields and conditional_public_fields: {overlap}",
            path=label,
        )
    for overlap in sorted(private_fields & conditional_public_fields):
        add_error(
            errors,
            code="VISIBILITY_FIELD_OVERLAP",
            message=f"{label} reuses field in private_fields and conditional_public_fields: {overlap}",
            path=label,
        )


def validate_page_family(item: Any, *, index: int, errors: list[dict[str, Any]], seen_families: set[str], seen_query_ids: set[str]) -> None:
    label = f"page_families[{index}]"
    if not isinstance(item, dict):
        add_error(errors, code="PAGE_FAMILY_NOT_OBJECT", message=f"{label} must be an object", path=label)
        return
    for key in sorted(REQUIRED_FAMILY_KEYS - set(item)):
        add_error(errors, code="MISSING_PAGE_FAMILY_KEY", message=f"missing required {label} key: {key}", path=label)
    family = validate_nonblank_string(item.get("page_family"), label=f"{label}.page_family", errors=errors, code="INVALID_PAGE_FAMILY")
    if family is not None:
        if family in seen_families:
            add_error(errors, code="DUPLICATE_PAGE_FAMILY", message=f"duplicate page_family: {family}", path=f"{label}.page_family")
        else:
            seen_families.add(family)
    query_contract_id = validate_nonblank_string(
        item.get("query_contract_id"),
        label=f"{label}.query_contract_id",
        errors=errors,
        code="INVALID_PAGE_FAMILY_FIELD",
    )
    if query_contract_id is not None:
        if query_contract_id in seen_query_ids:
            add_error(errors, code="DUPLICATE_QUERY_CONTRACT_ID", message=f"duplicate query_contract_id: {query_contract_id}", path=f"{label}.query_contract_id")
        else:
            seen_query_ids.add(query_contract_id)
    validate_nonblank_string(item.get("description"), label=f"{label}.description", errors=errors, code="INVALID_PAGE_FAMILY_FIELD")
    inputs = item.get("inputs")
    if not isinstance(inputs, list):
        add_error(errors, code="INPUTS_NOT_ARRAY", message=f"{label}.inputs must be an array", path=f"{label}.inputs")
    else:
        if not inputs:
            add_error(errors, code="INPUTS_EMPTY", message=f"{label}.inputs must contain at least one input", path=f"{label}.inputs")
        for input_index, input_item in enumerate(inputs):
            validate_input(input_item, family_label=label, index=input_index, errors=errors)
    validate_sparse_state(item.get("sparse_state"), family_label=label, errors=errors)
    validate_field_visibility(item.get("field_visibility"), family_label=label, errors=errors)
    validate_state_example(item.get("empty_example"), family_label=label, example_name="empty_example", expected_state="empty", errors=errors)
    validate_state_example(item.get("populated_example"), family_label=label, example_name="populated_example", expected_state="ready", errors=errors)


def validate_static_page_family_query_contract_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for key in sorted(REQUIRED_TOP_LEVEL_KEYS - set(payload)):
        add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required top-level key: {key}", path=key)
    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="SCHEMA_VERSION_MISMATCH", message=f"schema_version must equal {SCHEMA_VERSION}", path="schema_version")
    if payload.get("contract_doc") != CONTRACT_DOC:
        add_error(errors, code="CONTRACT_DOC_MISMATCH", message=f"contract_doc must equal {CONTRACT_DOC}", path="contract_doc")
    page_families = payload.get("page_families")
    seen_families: set[str] = set()
    seen_query_ids: set[str] = set()
    if not isinstance(page_families, list):
        add_error(errors, code="PAGE_FAMILIES_NOT_ARRAY", message="page_families must be an array", path="page_families")
    else:
        for index, item in enumerate(page_families):
            validate_page_family(item, index=index, errors=errors, seen_families=seen_families, seen_query_ids=seen_query_ids)
    for family in sorted(REQUIRED_PAGE_FAMILIES - seen_families):
        add_error(errors, code="REQUIRED_PAGE_FAMILY_MISSING", message=f"missing required page_family: {family}", path="page_families")
    return errors


def validate_static_page_family_query_contract(target: Path) -> tuple[dict[str, Any], int]:
    payload, load_errors, exit_code = load_json_object(target)
    errors = list(load_errors)
    if payload is not None:
        errors.extend(validate_static_page_family_query_contract_payload(payload))
    status = "pass" if not errors and exit_code == EXIT_PASS else "fail"
    if errors and exit_code == EXIT_PASS:
        exit_code = EXIT_VALIDATION_FAILED
    report = {
        "validator": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "target": display_path(str(target)),
        "status": status,
        "counts": {
            "inspected": 0 if payload is None and exit_code == EXIT_INPUT_UNAVAILABLE else 1,
            "accepted": 1 if status == "pass" else 0,
            "rejected": 0 if status == "pass" else 1,
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
    report, exit_code = validate_static_page_family_query_contract(Path(args.target))
    report["scenario"] = args.scenario
    if args.target_id:
        report["target"] = args.target_id
    report["output_artifacts"] = {
        "report_json": display_path(args.report_json) if args.report_json else None,
        "report_text": display_path(args.report_text) if args.report_text else None,
    }
    text_report = render_text_report(report)
    write_json(args.report_json, report)
    write_text(args.report_text, text_report)
    sys.stdout.write(text_report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
