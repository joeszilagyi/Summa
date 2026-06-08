#!/usr/bin/env python3
"""Validate scheduler-failure-state-reconciliation JSON artifacts."""

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
        is_rfc3339_datetime,
        render_text_report,
        resolve_report_root,
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
        resolve_report_root,
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.scheduler_failure_reconciliation_contract import (  # noqa: E402
    DERIVED_STATUSES,
    ENTRY_SCHEMA_VERSION,
    RECOMMENDATIONS,
    SCHEMA_VERSION,
)

VALIDATOR_NAME = "scheduler_failure_state_reconciliation"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/scheduler_failure_state_reconciliation.schema.json"
FAILURE_STATE_KEYS = {
    "status",
    "attempt_count",
    "last_failure_at",
    "next_retry_at",
    "last_failure_reason",
    "blocked_reason",
}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate one scheduler-failure-state-reconciliation JSON artifact.")
    parser.add_argument("target", help="Path to the reconciliation JSON artifact.")
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


def validate_nonnegative_int(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        add_error(errors, code=code, message=f"{field_name} must be an integer >= 0")
        return None
    return value


def validate_timestamp(value: Any, *, field_name: str, errors: list[dict[str, Any]], code: str, allow_null: bool = False) -> None:
    if value is None and allow_null:
        return
    if not isinstance(value, str) or not is_rfc3339_datetime(value):
        add_error(errors, code=code, message=f"{field_name} must be an RFC3339 timestamp")


def validate_failure_state(
    value: Any,
    *,
    field_name: str,
    errors: list[dict[str, Any]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        add_error(errors, code="INVALID_FAILURE_STATE", message=f"{field_name} must be an object or null")
        return
    unknown_keys = sorted(set(value) - FAILURE_STATE_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FAILURE_STATE_KEY", message=f"{field_name} has unexpected key: {key}")
    status = validate_nonblank_string(value.get("status"), field_name=f"{field_name}.status", errors=errors, code="INVALID_FAILURE_STATE")
    if status is not None and status not in DERIVED_STATUSES:
        add_error(errors, code="INVALID_FAILURE_STATUS", message=f"{field_name}.status must be one of: {', '.join(sorted(DERIVED_STATUSES))}")
    validate_nonnegative_int(
        value.get("attempt_count"),
        field_name=f"{field_name}.attempt_count",
        errors=errors,
        code="INVALID_FAILURE_STATE",
    )
    validate_timestamp(
        value.get("last_failure_at"),
        field_name=f"{field_name}.last_failure_at",
        errors=errors,
        code="INVALID_FAILURE_STATE_TIMESTAMP",
        allow_null=True,
    )
    validate_timestamp(
        value.get("next_retry_at"),
        field_name=f"{field_name}.next_retry_at",
        errors=errors,
        code="INVALID_FAILURE_STATE_TIMESTAMP",
        allow_null=True,
    )
    if value.get("last_failure_reason") is not None:
        validate_nonblank_string(
            value.get("last_failure_reason"),
            field_name=f"{field_name}.last_failure_reason",
            errors=errors,
            code="INVALID_FAILURE_STATE",
        )
    if value.get("blocked_reason") is not None:
        validate_nonblank_string(
            value.get("blocked_reason"),
            field_name=f"{field_name}.blocked_reason",
            errors=errors,
            code="INVALID_FAILURE_STATE",
        )


def validate_entries(payload: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        add_error(errors, code="ENTRIES_NOT_ARRAY", message="entries must be an array")
        return
    changed_count = 0
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, dict):
            add_error(errors, code="ENTRY_NOT_OBJECT", message=f"{label} must be an object")
            continue
        if entry.get("schema_version") != ENTRY_SCHEMA_VERSION:
            add_error(errors, code="INVALID_ENTRY_SCHEMA_VERSION", message=f"{label}.schema_version must equal {ENTRY_SCHEMA_VERSION}")
        validate_nonblank_string(entry.get("workspace_id"), field_name=f"{label}.workspace_id", errors=errors, code="INVALID_ENTRY_FIELD")
        validate_nonblank_string(entry.get("ledger_path"), field_name=f"{label}.ledger_path", errors=errors, code="INVALID_ENTRY_FIELD")
        validate_nonnegative_int(entry.get("ledger_event_count"), field_name=f"{label}.ledger_event_count", errors=errors, code="INVALID_ENTRY_FIELD")
        validate_nonnegative_int(entry.get("terminal_run_count"), field_name=f"{label}.terminal_run_count", errors=errors, code="INVALID_ENTRY_FIELD")
        validate_failure_state(entry.get("registry_failure_state"), field_name=f"{label}.registry_failure_state", errors=errors)
        validate_failure_state(entry.get("derived_failure_state"), field_name=f"{label}.derived_failure_state", errors=errors)
        recommendation = validate_nonblank_string(
            entry.get("recommendation"),
            field_name=f"{label}.recommendation",
            errors=errors,
            code="INVALID_ENTRY_FIELD",
        )
        if recommendation is not None:
            if recommendation not in RECOMMENDATIONS:
                add_error(errors, code="INVALID_RECOMMENDATION", message=f"{label}.recommendation must be one of: {', '.join(sorted(RECOMMENDATIONS))}")
            elif recommendation == "replace":
                changed_count += 1
        reasons = entry.get("reasons")
        if not isinstance(reasons, list):
            add_error(errors, code="INVALID_REASONS", message=f"{label}.reasons must be an array")
        else:
            for reason_index, reason in enumerate(reasons):
                validate_nonblank_string(
                    reason,
                    field_name=f"{label}.reasons[{reason_index}]",
                    errors=errors,
                    code="INVALID_REASONS",
                )
        validate_timestamp(
            entry.get("latest_success_at"),
            field_name=f"{label}.latest_success_at",
            errors=errors,
            code="INVALID_ENTRY_TIMESTAMP",
            allow_null=True,
        )
        validate_timestamp(
            entry.get("latest_failure_at"),
            field_name=f"{label}.latest_failure_at",
            errors=errors,
            code="INVALID_ENTRY_TIMESTAMP",
            allow_null=True,
        )

    workspace_count = payload.get("workspace_count")
    if isinstance(workspace_count, int) and workspace_count != len(entries):
        add_error(errors, code="WORKSPACE_COUNT_MISMATCH", message="workspace_count must equal len(entries)")
    if isinstance(payload.get("changed_count"), int) and payload.get("changed_count") != changed_count:
        add_error(errors, code="CHANGED_COUNT_MISMATCH", message="changed_count does not match replace recommendations")
    unchanged_count = payload.get("unchanged_count")
    if (
        isinstance(unchanged_count, int)
        and isinstance(workspace_count, int)
        and isinstance(payload.get("changed_count"), int)
        and unchanged_count != workspace_count - payload["changed_count"]
    ):
        add_error(errors, code="UNCHANGED_COUNT_MISMATCH", message="unchanged_count must equal workspace_count - changed_count")


def validate_scheduler_failure_state_reconciliation(target: Path) -> tuple[dict[str, Any], int]:
    payload, errors, exit_code = load_json_object(target)
    if payload is None:
        report = {
            "validator_name": VALIDATOR_NAME,
            "contract_version": CONTRACT_VERSION,
            "schema_path": SCHEMA_PATH,
            "target": display_path(str(target)) or str(target),
            "status": "input_unavailable" if exit_code == EXIT_INPUT_UNAVAILABLE else "invalid",
            "errors": errors,
        }
        return report, exit_code

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", message=f"schema_version must equal {SCHEMA_VERSION}")
    validate_timestamp(payload.get("generated_at"), field_name="generated_at", errors=errors, code="INVALID_GENERATED_AT")
    validate_nonblank_string(payload.get("registry_path"), field_name="registry_path", errors=errors, code="INVALID_REGISTRY_PATH")
    validate_nonnegative_int(payload.get("workspace_count"), field_name="workspace_count", errors=errors, code="INVALID_COUNT")
    validate_nonnegative_int(payload.get("changed_count"), field_name="changed_count", errors=errors, code="INVALID_COUNT")
    validate_nonnegative_int(payload.get("unchanged_count"), field_name="unchanged_count", errors=errors, code="INVALID_COUNT")
    output_registry = payload.get("updated_registry_path")
    if output_registry is not None:
        validate_nonblank_string(output_registry, field_name="updated_registry_path", errors=errors, code="INVALID_UPDATED_REGISTRY_PATH")
    validate_entries(payload, errors)

    report = {
        "validator_name": VALIDATOR_NAME,
        "contract_version": CONTRACT_VERSION,
        "schema_path": SCHEMA_PATH,
        "target": display_path(str(target)) or str(target),
        "status": "pass" if not errors else "invalid",
        "errors": errors,
    }
    return report, EXIT_PASS if not errors else EXIT_VALIDATION_FAILED


def main(argv: list[str] | None = None) -> int:
    args = parse_args()
    target = Path(args.target)
    report, exit_code = validate_scheduler_failure_state_reconciliation(target)
    report_root = resolve_report_root(target, report_root=args.report_root)
    rendered = render_text_report(report)
    if args.report_json:
        write_json(Path(args.report_json), report, root=report_root)
    if args.report_text:
        write_text(Path(args.report_text), rendered, root=report_root)
    sys.stdout.write(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
