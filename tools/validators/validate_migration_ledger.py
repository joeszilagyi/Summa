#!/usr/bin/env python3
"""Validate append-only migration-ledger JSONL documents."""

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
        emit_report,
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
        emit_report,
        is_rfc3339_datetime,
        render_text_report,
        write_json,
        write_text,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.common.migration_ledger import MIGRATION_TYPES, SCHEMA_VERSION


VALIDATOR_NAME = "migration_ledger"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/migration_ledger.schema.json"
FIXTURE_PATH = "tests/fixtures/validators/migration_ledger/valid_append_only/inputs/migration_ledger.jsonl"

EVENT_ID_PATTERN = re.compile(r"^mle:[a-z0-9][a-z0-9._:-]*$")
MIGRATION_ID_PATTERN = re.compile(r"^mig:[a-z0-9][a-z0-9._:-]*$")
WORKSPACE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ALLOWED_KEYS = {
    "schema_version",
    "event_id",
    "workspace_id",
    "migration_id",
    "migration_type",
    "subject_ref",
    "tool_surface",
    "tool_version",
    "input_version",
    "output_version",
    "input_artifact_refs",
    "output_artifact_refs",
    "run_id",
    "backup_ref",
    "snapshot_ref",
    "rollback_of_event_id",
    "occurred_at",
    "note",
}
REQUIRED_KEYS = ALLOWED_KEYS - {"run_id", "backup_ref", "snapshot_ref", "rollback_of_event_id"}


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one migration-ledger JSONL document.",
        epilog=(
            "Reads the target file and writes validation output to stdout.\n"
            "Optional --report-json/--report-text paths are created atomically.\n\n"
            f"Schema: {SCHEMA_PATH}\n"
            f"Example:\n  python3 tools/validators/validate_migration_ledger.py {FIXTURE_PATH}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="Path to the migration-ledger JSONL document to validate.")
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


def normalize_timestamp(value: str) -> datetime:
    parseable = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(parseable)


def validate_nonblank_string(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    line_number: int,
    code: str,
) -> str | None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        add_error(errors, code=code, line=line_number, message=f"{field} must be a non-blank string")
        return None
    return value


def validate_artifact_refs(
    payload: dict[str, Any],
    field: str,
    errors: list[dict[str, Any]],
    *,
    line_number: int,
) -> list[dict[str, str]]:
    value = payload.get(field)
    if not isinstance(value, list) or not value:
        add_error(errors, code="INVALID_ARTIFACT_REFS", line=line_number, message=f"{field} must be a non-empty array")
        return []
    validated: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            add_error(errors, code="INVALID_ARTIFACT_REF", line=line_number, message=f"{field}[{index}] must be an object")
            continue
        unknown = sorted(set(item) - {"role", "path", "version"})
        if unknown:
            add_error(errors, code="UNKNOWN_ARTIFACT_REF_FIELD", line=line_number, message=f"{field}[{index}] has unexpected fields: {', '.join(unknown)}")
        role = item.get("role")
        path = item.get("path")
        version = item.get("version")
        if not isinstance(role, str) or not role.strip():
            add_error(errors, code="INVALID_ARTIFACT_REF", line=line_number, message=f"{field}[{index}].role must be a non-blank string")
            continue
        if not isinstance(path, str) or not path.strip():
            add_error(errors, code="INVALID_ARTIFACT_REF", line=line_number, message=f"{field}[{index}].path must be a non-blank string")
            continue
        ref = {"role": role, "path": path}
        if version is not None:
            if not isinstance(version, str) or not version.strip():
                add_error(errors, code="INVALID_ARTIFACT_REF", line=line_number, message=f"{field}[{index}].version must be null or a non-blank string")
                continue
            ref["version"] = version
        validated.append(ref)
    return validated


def validate_line(
    payload: Any,
    *,
    line_number: int,
    errors: list[dict[str, Any]],
    seen_event_ids: set[str],
    seen_migration_ids: set[str],
    previous_timestamp: datetime | None,
) -> tuple[dict[str, Any] | None, datetime | None]:
    if not isinstance(payload, dict):
        add_error(errors, code="OBJECT_REQUIRED", line=line_number, message="each non-empty JSONL line must be a JSON object")
        return None, previous_timestamp

    unknown_keys = sorted(set(payload) - ALLOWED_KEYS)
    for key in unknown_keys:
        add_error(errors, code="UNKNOWN_FIELD", line=line_number, message=f"unexpected field: {key}")
    for key in sorted(REQUIRED_KEYS):
        if key not in payload:
            add_error(errors, code="MISSING_REQUIRED_KEY", line=line_number, message=f"missing required key: {key}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        add_error(errors, code="INVALID_SCHEMA_VERSION", line=line_number, message=f"schema_version must be {SCHEMA_VERSION}")

    event_id = validate_nonblank_string(payload, "event_id", errors, line_number=line_number, code="INVALID_EVENT_ID")
    if event_id is not None:
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            add_error(errors, code="INVALID_EVENT_ID", line=line_number, message="event_id must match ^mle:[a-z0-9][a-z0-9._:-]*$")
        elif event_id in seen_event_ids:
            add_error(errors, code="DUPLICATE_EVENT_ID", line=line_number, message=f"event_id is duplicated: {event_id}")
        else:
            seen_event_ids.add(event_id)

    migration_id = validate_nonblank_string(payload, "migration_id", errors, line_number=line_number, code="INVALID_MIGRATION_ID")
    if migration_id is not None:
        if not MIGRATION_ID_PATTERN.fullmatch(migration_id):
            add_error(errors, code="INVALID_MIGRATION_ID", line=line_number, message="migration_id must match ^mig:[a-z0-9][a-z0-9._:-]*$")
        elif migration_id in seen_migration_ids:
            add_error(errors, code="DUPLICATE_MIGRATION_ID", line=line_number, message=f"migration_id is duplicated: {migration_id}")
        else:
            seen_migration_ids.add(migration_id)

    workspace_id = validate_nonblank_string(payload, "workspace_id", errors, line_number=line_number, code="INVALID_WORKSPACE_ID")
    if workspace_id is not None and not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        add_error(errors, code="INVALID_WORKSPACE_ID", line=line_number, message="workspace_id must match ^[a-z0-9][a-z0-9._-]*$")

    migration_type = validate_nonblank_string(payload, "migration_type", errors, line_number=line_number, code="INVALID_MIGRATION_TYPE")
    if migration_type is not None and migration_type not in MIGRATION_TYPES:
        add_error(errors, code="INVALID_MIGRATION_TYPE", line=line_number, message=f"migration_type must be one of: {', '.join(sorted(MIGRATION_TYPES))}")

    validate_nonblank_string(payload, "subject_ref", errors, line_number=line_number, code="INVALID_SUBJECT_REF")
    validate_nonblank_string(payload, "tool_surface", errors, line_number=line_number, code="INVALID_TOOL_SURFACE")
    validate_nonblank_string(payload, "tool_version", errors, line_number=line_number, code="INVALID_TOOL_VERSION")

    input_version = validate_nonblank_string(payload, "input_version", errors, line_number=line_number, code="INVALID_INPUT_VERSION")
    output_version = validate_nonblank_string(payload, "output_version", errors, line_number=line_number, code="INVALID_OUTPUT_VERSION")
    if input_version is not None and output_version is not None and input_version == output_version:
        add_error(errors, code="UNCHANGED_VERSION", line=line_number, message="input_version and output_version must differ")

    validate_artifact_refs(payload, "input_artifact_refs", errors, line_number=line_number)
    validate_artifact_refs(payload, "output_artifact_refs", errors, line_number=line_number)

    run_id = payload.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        add_error(errors, code="INVALID_RUN_ID", line=line_number, message="run_id must be null or a non-blank string")

    backup_ref = payload.get("backup_ref")
    if backup_ref is not None and (not isinstance(backup_ref, str) or not backup_ref.strip()):
        add_error(errors, code="INVALID_BACKUP_REF", line=line_number, message="backup_ref must be null or a non-blank string")
        backup_ref = None

    snapshot_ref = payload.get("snapshot_ref")
    if snapshot_ref is not None and (not isinstance(snapshot_ref, str) or not snapshot_ref.strip()):
        add_error(errors, code="INVALID_SNAPSHOT_REF", line=line_number, message="snapshot_ref must be null or a non-blank string")
        snapshot_ref = None

    rollback_of_event_id = payload.get("rollback_of_event_id")
    if rollback_of_event_id is not None:
        if not isinstance(rollback_of_event_id, str) or not EVENT_ID_PATTERN.fullmatch(rollback_of_event_id):
            add_error(errors, code="INVALID_ROLLBACK_REF", line=line_number, message="rollback_of_event_id must match ^mle:[a-z0-9][a-z0-9._:-]*$")
        elif rollback_of_event_id not in seen_event_ids or rollback_of_event_id == event_id:
            add_error(errors, code="UNKNOWN_ROLLBACK_REF", line=line_number, message=f"rollback_of_event_id does not reference an earlier event: {rollback_of_event_id}")

    occurred_at = payload.get("occurred_at")
    current_timestamp = previous_timestamp
    if not isinstance(occurred_at, str) or not is_rfc3339_datetime(occurred_at):
        add_error(errors, code="INVALID_OCCURRED_AT", line=line_number, message="occurred_at must be an RFC3339 datetime")
    else:
        current_timestamp = normalize_timestamp(occurred_at)
        if previous_timestamp is not None and current_timestamp < previous_timestamp:
            add_error(errors, code="NON_MONOTONIC_OCCURRED_AT", line=line_number, message="occurred_at must be append-only and non-decreasing")

    note = payload.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip()):
        add_error(errors, code="INVALID_NOTE", line=line_number, message="note must be null or a non-blank string")

    if migration_type == "rollback_reference":
        if rollback_of_event_id is None:
            add_error(errors, code="ROLLBACK_REF_REQUIRED", line=line_number, message="rollback_reference events require rollback_of_event_id")
        if backup_ref is None and snapshot_ref is None:
            add_error(errors, code="ROLLBACK_EVIDENCE_REQUIRED", line=line_number, message="rollback_reference events require backup_ref or snapshot_ref")
    elif rollback_of_event_id is not None:
        add_error(errors, code="ROLLBACK_REF_NOT_ALLOWED", line=line_number, message="rollback_of_event_id is only allowed on rollback_reference events")

    if errors and errors[-1].get("line") == line_number:
        return None, current_timestamp
    return payload, current_timestamp


def validate_migration_ledger(target: Path) -> tuple[dict[str, Any], int]:
    counts = {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    latest_event: dict[str, Any] | None = None

    if not target.exists():
        errors.append({"code": "INPUT_NOT_FOUND", "line": None, "message": "input path does not exist"})
        return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_INPUT_UNAVAILABLE
    if not target.is_file():
        errors.append({"code": "INPUT_NOT_FILE", "line": None, "message": "input path is not a file"})
        return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_INPUT_UNAVAILABLE

    seen_event_ids: set[str] = set()
    seen_migration_ids: set[str] = set()
    previous_timestamp: datetime | None = None

    try:
        with target.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                counts["inspected"] += 1
                try:
                    payload = json.loads(line, object_pairs_hook=no_duplicate_object_pairs, parse_constant=reject_json_constant)
                except DuplicateJsonKeyError as exc:
                    counts["rejected"] += 1
                    add_error(errors, code="DUPLICATE_JSON_KEY", line=line_number, message=str(exc))
                    return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_VALIDATION_FAILED
                except NonStandardJsonConstantError as exc:
                    counts["rejected"] += 1
                    add_error(errors, code="NON_STANDARD_JSON_CONSTANT", line=line_number, message=str(exc))
                    return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_VALIDATION_FAILED
                except json.JSONDecodeError:
                    counts["rejected"] += 1
                    add_error(errors, code="JSON_PARSE_ERROR", line=line_number, message="invalid JSON syntax")
                    return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_VALIDATION_FAILED
                validated, previous_timestamp = validate_line(
                    payload,
                    line_number=line_number,
                    errors=errors,
                    seen_event_ids=seen_event_ids,
                    seen_migration_ids=seen_migration_ids,
                    previous_timestamp=previous_timestamp,
                )
                if validated is None:
                    counts["rejected"] += 1
                    return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_VALIDATION_FAILED
                latest_event = {
                    "event_id": validated["event_id"],
                    "workspace_id": validated["workspace_id"],
                    "migration_id": validated["migration_id"],
                    "migration_type": validated["migration_type"],
                    "occurred_at": validated["occurred_at"],
                    "tool_surface": validated["tool_surface"],
                    "input_version": validated["input_version"],
                    "output_version": validated["output_version"],
                    "backup_ref": validated.get("backup_ref"),
                    "snapshot_ref": validated.get("snapshot_ref"),
                    "rollback_of_event_id": validated.get("rollback_of_event_id"),
                }
                counts["accepted"] += 1
    except UnicodeDecodeError:
        errors.append({"code": "INPUT_DECODE_ERROR", "line": None, "message": "input file is not valid UTF-8"})
        return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_INPUT_UNAVAILABLE
    except OSError:
        errors.append({"code": "INPUT_UNREADABLE", "line": None, "message": "input file could not be read"})
        return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_INPUT_UNAVAILABLE

    if counts["accepted"] == 0:
        errors.append({"code": "EMPTY_LEDGER", "line": None, "message": "migration-ledger must contain at least one JSON object line"})
        return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_VALIDATION_FAILED

    return {"counts": counts, "errors": errors, "warnings": warnings, "latest_event": latest_event}, EXIT_PASS


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    result, exit_code = validate_migration_ledger(target)
    status = "pass" if exit_code == EXIT_PASS else "fail"
    output_artifacts = {
        "report_json": display_path(args.report_json),
        "report_text": display_path(args.report_text),
    }
    report = emit_report(
        contract_version=CONTRACT_VERSION,
        counts=result["counts"],
        errors=result["errors"],
        output_artifacts=output_artifacts,
        report_json_path=args.report_json,
        report_text_path=args.report_text,
        scenario=args.scenario,
        status=status,
        target=args.target_id or display_path(args.target) or str(target),
        validator=VALIDATOR_NAME,
        warnings=result["warnings"],
    )
    report["latest_event"] = result["latest_event"]
    text_report = render_text_report(report)
    write_json(args.report_json, report)
    write_text(args.report_text, text_report)
    sys.stdout.write(text_report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
