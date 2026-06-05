#!/usr/bin/env python3
"""Validate correction-ledger JSON documents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
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

from tools.common.correction_ledger_contract import (  # noqa: E402
    CORRECTION_ACTIONS,
    EVIDENCE_LOCATOR_REF_PREFIX,
    FIELD_REVIEW_ENTRY_REF_PREFIX,
    OBJECT_REF_PREFIXES,
    PROVENANCE_EVENT_REF_PREFIX,
    REVIEW_QUEUE_REF_PREFIXES,
    SCHEMA_VERSION,
)


VALIDATOR_NAME = "correction_ledger"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/correction_ledger.schema.json"
FIXTURE_PATH = "tests/fixtures/validators/correction_ledger/valid_lineage/inputs/correction_ledger.json"

EVENT_ID_PATTERN = re.compile(r"^cle:[a-z0-9][a-z0-9._:-]*$")
OBJECT_REF_PATTERN = re.compile(r"^(?P<prefix>[a-z_]+):(?P<id>[0-9]+)$")
PROVENANCE_REF_PATTERN = re.compile(r"^prov:[a-z0-9-]+$")
EVIDENCE_LOCATOR_REF_PATTERN = re.compile(r"^evl:[a-z0-9][a-z0-9._:-]*$")
FIELD_REVIEW_REF_PATTERN = re.compile(r"^frs:[a-z0-9][a-z0-9._:-]*$")

REQUIRED_KEYS = {"schema_version", "workspace_id", "events"}
EVENT_REQUIRED_KEYS = {
    "event_id",
    "action",
    "changed_at",
    "changed_by",
    "rationale",
    "source_object_refs",
    "result_object_refs",
    "review_queue_refs",
    "provenance_event_refs",
    "evidence_locator_refs",
    "field_review_entry_refs",
    "note",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one correction-ledger JSON document.",
        epilog=(
            "Reads the target file and writes validation output to stdout.\n"
            "Optional --report-json/--report-text paths are created atomically.\n\n"
            f"Schema: {SCHEMA_PATH}\n"
            f"Example:\n  python3 tools/validators/validate_correction_ledger.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the correction-ledger JSON document to validate.")
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


def normalize_timestamp(value: str) -> datetime:
    parseable = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(parseable)


def validate_object_ref(value: str, *, field_label: str, errors: list[dict[str, Any]], code: str) -> bool:
    match = OBJECT_REF_PATTERN.fullmatch(value)
    if match is None:
        add_error(errors, code=code, message=f"{field_label} must match ^[a-z_]+:[0-9]+$")
        return False
    if match.group("prefix") not in OBJECT_REF_PREFIXES:
        add_error(
            errors,
            code=code,
            message=f"{field_label} uses unsupported object namespace: {match.group('prefix')}",
        )
        return False
    return True


def validate_review_queue_ref(value: str, *, field_label: str, errors: list[dict[str, Any]]) -> bool:
    match = OBJECT_REF_PATTERN.fullmatch(value)
    if match is None:
        add_error(errors, code="INVALID_REVIEW_QUEUE_REF", message=f"{field_label} must match ^[a-z_]+:[0-9]+$")
        return False
    if match.group("prefix") not in REVIEW_QUEUE_REF_PREFIXES:
        add_error(
            errors,
            code="INVALID_REVIEW_QUEUE_REF",
            message=f"{field_label} uses unsupported review queue namespace: {match.group('prefix')}",
        )
        return False
    return True


def validate_string_array(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    allow_empty: bool = True,
    item_validator: callable | None = None,
    item_code: str = "INVALID_ARRAY_ITEM",
) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        add_error(errors, code=item_code, message=f"{field} must be an array")
        return []
    if not allow_empty and not value:
        add_error(errors, code=item_code, message=f"{field} must not be empty")
        return []
    accepted: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            add_error(errors, code=item_code, message=f"{field}[{index}] must be a non-blank string")
            continue
        if item in seen:
            add_error(errors, code="DUPLICATE_ARRAY_ITEM", message=f"{field} contains duplicate value: {item}")
            continue
        seen.add(item)
        if item_validator is not None and not item_validator(item, field_label=f"{field}[{index}]", errors=errors):
            continue
        accepted.append(item)
    return accepted


def validate_event_shape(event: Any, index: int, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        add_error(errors, code="EVENT_NOT_OBJECT", message=f"events[{index}] must be an object")
        return None
    unknown_keys = sorted(set(event) - EVENT_REQUIRED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_EVENT_FIELD", message=f"unexpected events[{index}] field: {key}")
    for key in sorted(EVENT_REQUIRED_KEYS):
        if key not in event:
            add_error(errors, code="MISSING_EVENT_KEY", message=f"missing required events[{index}] key: {key}")

    event_id = validate_nonblank_string(event, "event_id", errors, code="INVALID_EVENT_ID")
    if event_id is not None and not EVENT_ID_PATTERN.fullmatch(event_id):
        add_error(errors, code="INVALID_EVENT_ID", message=f"events[{index}].event_id must match ^cle:[a-z0-9][a-z0-9._:-]*$")
    action = validate_nonblank_string(event, "action", errors, code="INVALID_ACTION")
    if action is not None and action not in CORRECTION_ACTIONS:
        add_error(errors, code="INVALID_ACTION", message=f"events[{index}].action must be one of: {', '.join(sorted(CORRECTION_ACTIONS))}")
    changed_at = event.get("changed_at")
    if not isinstance(changed_at, str) or not is_rfc3339_datetime(changed_at):
        add_error(errors, code="INVALID_CHANGED_AT", message=f"events[{index}].changed_at must be an RFC3339 datetime")
        changed_at = None
    validate_nonblank_string(event, "changed_by", errors, code="INVALID_CHANGED_BY")
    validate_nonblank_string(event, "rationale", errors, code="INVALID_RATIONALE")
    note = event.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        add_error(errors, code="INVALID_NOTE", message=f"events[{index}].note must be null or a non-blank string")

    source_object_refs = validate_string_array(
        event,
        "source_object_refs",
        errors,
        allow_empty=False,
        item_validator=lambda value, field_label, errors: validate_object_ref(value, field_label=field_label, errors=errors, code="INVALID_OBJECT_REF"),
        item_code="INVALID_OBJECT_REF",
    )
    result_object_refs = validate_string_array(
        event,
        "result_object_refs",
        errors,
        allow_empty=False,
        item_validator=lambda value, field_label, errors: validate_object_ref(value, field_label=field_label, errors=errors, code="INVALID_OBJECT_REF"),
        item_code="INVALID_OBJECT_REF",
    )
    review_queue_refs = validate_string_array(
        event,
        "review_queue_refs",
        errors,
        item_validator=validate_review_queue_ref,
        item_code="INVALID_REVIEW_QUEUE_REF",
    )
    provenance_event_refs = validate_string_array(
        event,
        "provenance_event_refs",
        errors,
        item_validator=lambda value, field_label, errors: _validate_pattern(
            value,
            PROVENANCE_REF_PATTERN,
            field_label=field_label,
            errors=errors,
            code="INVALID_PROVENANCE_EVENT_REF",
            prefix=PROVENANCE_EVENT_REF_PREFIX,
        ),
        item_code="INVALID_PROVENANCE_EVENT_REF",
    )
    evidence_locator_refs = validate_string_array(
        event,
        "evidence_locator_refs",
        errors,
        item_validator=lambda value, field_label, errors: _validate_pattern(
            value,
            EVIDENCE_LOCATOR_REF_PATTERN,
            field_label=field_label,
            errors=errors,
            code="INVALID_EVIDENCE_LOCATOR_REF",
            prefix=EVIDENCE_LOCATOR_REF_PREFIX,
        ),
        item_code="INVALID_EVIDENCE_LOCATOR_REF",
    )
    field_review_entry_refs = validate_string_array(
        event,
        "field_review_entry_refs",
        errors,
        item_validator=lambda value, field_label, errors: _validate_pattern(
            value,
            FIELD_REVIEW_REF_PATTERN,
            field_label=field_label,
            errors=errors,
            code="INVALID_FIELD_REVIEW_ENTRY_REF",
            prefix=FIELD_REVIEW_ENTRY_REF_PREFIX,
        ),
        item_code="INVALID_FIELD_REVIEW_ENTRY_REF",
    )

    if action in {"merge", "dedupe", "supersede"} and len(result_object_refs) != 1:
        add_error(errors, code="INVALID_RESULT_CARDINALITY", message=f"events[{index}] action {action} requires exactly one result_object_ref")
    if action == "split" and len(source_object_refs) != 1:
        add_error(errors, code="INVALID_SOURCE_CARDINALITY", message="split actions require exactly one source_object_ref")
    if action == "split" and len(result_object_refs) < 2:
        add_error(errors, code="INVALID_RESULT_CARDINALITY", message="split actions require at least two result_object_refs")
    if set(source_object_refs) & set(result_object_refs):
        add_error(errors, code="OVERLAPPING_OBJECT_REFS", message=f"events[{index}] source_object_refs and result_object_refs must be disjoint")
    if not provenance_event_refs:
        add_error(errors, code="PROVENANCE_REF_REQUIRED", message=f"events[{index}] must preserve at least one provenance_event_ref")

    if event_id is None or action is None or changed_at is None:
        return None
    return {
        "event_id": event_id,
        "action": action,
        "changed_at": changed_at,
        "source_object_refs": source_object_refs,
        "result_object_refs": result_object_refs,
        "review_queue_refs": review_queue_refs,
        "provenance_event_refs": provenance_event_refs,
        "evidence_locator_refs": evidence_locator_refs,
        "field_review_entry_refs": field_review_entry_refs,
    }


def _validate_pattern(
    value: str,
    pattern: re.Pattern[str],
    *,
    field_label: str,
    errors: list[dict[str, Any]],
    code: str,
    prefix: str,
) -> bool:
    if not pattern.fullmatch(value):
        add_error(errors, code=code, message=f"{field_label} must start with {prefix}")
        return False
    return True


def resolve_lineage(events: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    current_refs: set[str] = set()
    superseded_by_ref: dict[str, str] = {}
    last_timestamp: datetime | None = None
    seen_event_ids: set[str] = set()

    for index, event in enumerate(events):
        event_id = event["event_id"]
        if event_id in seen_event_ids:
            add_error(errors, code="DUPLICATE_EVENT_ID", message=f"duplicate event_id: {event_id}")
            continue
        seen_event_ids.add(event_id)

        timestamp = normalize_timestamp(event["changed_at"])
        if last_timestamp is not None and timestamp < last_timestamp:
            add_error(errors, code="EVENTS_OUT_OF_ORDER", message="events must be ordered by changed_at ascending")
        last_timestamp = timestamp

        for ref in event["source_object_refs"]:
            if ref in superseded_by_ref:
                add_error(
                    errors,
                    code="SOURCE_OBJECT_NOT_CURRENT",
                    message=f"{ref} is already superseded by {superseded_by_ref[ref]} and cannot be corrected again as a current source",
                )
            current_refs.discard(ref)
            superseded_by_ref[ref] = event_id

        for ref in event["result_object_refs"]:
            if ref in superseded_by_ref:
                add_error(
                    errors,
                    code="RESULT_OBJECT_ALREADY_SUPERSEDED",
                    message=f"{ref} was already superseded by {superseded_by_ref[ref]} and cannot reappear as a current result",
                )
                continue
            current_refs.add(ref)

    return {
        "current_object_refs": sorted(current_refs),
        "superseded_object_refs": sorted(superseded_by_ref),
        "superseded_by_event_id": {key: superseded_by_ref[key] for key in sorted(superseded_by_ref)},
    }


def validate_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", message=f"missing required key: {key}")
    for key in sorted(set(payload) - REQUIRED_KEYS):
        add_error(errors, code="UNKNOWN_FIELD", message=f"unexpected field: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    validate_nonblank_string(payload, "workspace_id", errors, code="INVALID_WORKSPACE_ID")

    events_value = payload.get("events")
    validated_events: list[dict[str, Any]] = []
    if not isinstance(events_value, list):
        add_error(errors, code="EVENTS_NOT_ARRAY", message="events must be an array")
        return errors, {"current_object_refs": [], "superseded_object_refs": [], "superseded_by_event_id": {}}
    if not events_value:
        add_error(errors, code="EVENTS_EMPTY", message="events must not be empty")
    for index, event in enumerate(events_value):
        validated = validate_event_shape(event, index, errors)
        if validated is not None:
            validated_events.append(validated)

    resolution = resolve_lineage(validated_events, errors)
    return errors, resolution


def validate_correction_ledger(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, load_exit = load_json_object(target)
    resolution = {"current_object_refs": [], "superseded_object_refs": [], "superseded_by_event_id": {}}
    if payload is not None and load_exit == EXIT_PASS:
        payload_errors, resolution = validate_payload(payload)
        errors.extend(payload_errors)

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
        "resolution": resolution,
    }
    return report, exit_code


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    report, exit_code = validate_correction_ledger(target)
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
