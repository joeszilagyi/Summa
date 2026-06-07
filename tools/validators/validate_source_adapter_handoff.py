#!/usr/bin/env python3
"""Validate source-adapter handoff JSON or JSONL artifacts."""

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

from tools.common.source_adapter_handoff import validate_source_adapter_handoff_record  # noqa: E402

try:
    import validate_source_adapter  # noqa: E402
except ModuleNotFoundError:
    from tools.validators import validate_source_adapter  # type: ignore  # noqa: E402


VALIDATOR_NAME = "source_adapter_handoff"
CONTRACT_VERSION = "1"
SCHEMA_PATH = "config/source_adapter_handoff.schema.json"


class DuplicateJsonKeyError(ValueError):
    """Raised when JSON object parsing sees a duplicate key."""


class NonStandardJsonConstantError(ValueError):
    """Raised for NaN, Infinity, and -Infinity JSON constants."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate source-adapter handoff JSON or JSONL artifacts.")
    parser.add_argument("target", help="Path to the handoff JSON or JSONL artifact.")
    parser.add_argument(
        "--adapter",
        help="Optional source-adapter manifest path. When supplied, handoff records must align with that manifest.",
    )
    add_report_args(parser)
    return parser.parse_args()


def add_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    line: int | None = None,
) -> None:
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


def parse_json_value(raw_text: str) -> Any:
    return json.loads(
        raw_text,
        object_pairs_hook=no_duplicate_object_pairs,
        parse_constant=reject_json_constant,
    )


def validate_sequence_values(
    records: list[tuple[int | None, dict[str, Any]]],
) -> tuple[dict[int | None, str], str | None]:
    duplicate_sequence_lines: dict[int | None, str] = {}
    sequences: dict[int, list[int | None]] = {}

    for line_number, record in records:
        sequence = record.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            continue
        if sequence < 1:
            continue
        sequences.setdefault(sequence, []).append(line_number)

    for sequence, lines in sorted(sequences.items()):
        if len(lines) > 1:
            for line_number in lines[1:]:
                duplicate_sequence_lines[line_number] = (
                    f"handoff sequence values must be unique: {sequence} appears more than once"
                )

    sorted_sequences = sorted(sequences)
    if not sorted_sequences:
        return duplicate_sequence_lines, None
    missing_sequences = [
        index for index in range(1, sorted_sequences[-1] + 1) if index not in sequences
    ]
    if missing_sequences:
        missing_sequence = missing_sequences[0]
        return (
            duplicate_sequence_lines,
            f"handoff sequence values must be contiguous starting at 1 (missing {missing_sequence})",
        )

    return duplicate_sequence_lines, None


def load_adapter_payload(adapter_path: Path | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    if adapter_path is None:
        return None, [], EXIT_PASS
    result, exit_code = validate_source_adapter.validate_source_adapter(adapter_path)
    if exit_code != validate_source_adapter.EXIT_PASS:
        errors = result.get("errors", [])
        if errors:
            return None, [errors[0]], EXIT_VALIDATION_FAILED
        return None, [{"code": "ADAPTER_INVALID", "line": None, "message": "source adapter validation failed"}], EXIT_VALIDATION_FAILED
    payload = json.loads(adapter_path.read_text(encoding="utf-8"))
    return payload, [], EXIT_PASS


def load_records(target: Path) -> tuple[list[tuple[int | None, dict[str, Any]]], list[dict[str, Any]], int]:
    errors: list[dict[str, Any]] = []
    if not target.exists():
        add_error(errors, code="INPUT_NOT_FOUND", message="input path does not exist")
        return [], errors, EXIT_INPUT_UNAVAILABLE
    if not target.is_file():
        add_error(errors, code="INPUT_NOT_FILE", message="input path is not a file")
        return [], errors, EXIT_INPUT_UNAVAILABLE

    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        add_error(errors, code="INPUT_UNREADABLE", message="input file could not be read")
        return [], errors, EXIT_INPUT_UNAVAILABLE

    if target.suffix == ".jsonl":
        records: list[tuple[int | None, dict[str, Any]]] = []
        for line_number, raw_line in enumerate(raw_text.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                payload = parse_json_value(raw_line)
            except DuplicateJsonKeyError as exc:
                add_error(errors, code="DUPLICATE_JSON_KEY", line=line_number, message=str(exc))
                return [], errors, EXIT_VALIDATION_FAILED
            except NonStandardJsonConstantError as exc:
                add_error(errors, code="NON_STANDARD_JSON_CONSTANT", line=line_number, message=str(exc))
                return [], errors, EXIT_VALIDATION_FAILED
            except json.JSONDecodeError:
                add_error(errors, code="JSON_PARSE_ERROR", line=line_number, message="invalid JSON syntax")
                return [], errors, EXIT_VALIDATION_FAILED
            if not isinstance(payload, dict):
                add_error(errors, code="OBJECT_REQUIRED", line=line_number, message="handoff JSONL lines must be JSON objects")
                return [], errors, EXIT_VALIDATION_FAILED
            records.append((line_number, payload))
        return records, errors, EXIT_PASS

    try:
        payload = parse_json_value(raw_text)
    except DuplicateJsonKeyError as exc:
        add_error(errors, code="DUPLICATE_JSON_KEY", line=1, message=str(exc))
        return [], errors, EXIT_VALIDATION_FAILED
    except NonStandardJsonConstantError as exc:
        add_error(errors, code="NON_STANDARD_JSON_CONSTANT", line=1, message=str(exc))
        return [], errors, EXIT_VALIDATION_FAILED
    except json.JSONDecodeError:
        add_error(errors, code="JSON_PARSE_ERROR", line=1, message="invalid JSON syntax")
        return [], errors, EXIT_VALIDATION_FAILED

    if isinstance(payload, dict):
        return [(None, payload)], errors, EXIT_PASS
    if isinstance(payload, list):
        records: list[tuple[int | None, dict[str, Any]]] = []
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                add_error(errors, code="OBJECT_REQUIRED", line=index, message="handoff JSON arrays must contain only JSON objects")
                return [], errors, EXIT_VALIDATION_FAILED
            records.append((index, item))
        return records, errors, EXIT_PASS

    add_error(errors, code="OBJECT_REQUIRED", line=None, message="top-level JSON value must be an object or array of objects")
    return [], errors, EXIT_VALIDATION_FAILED


def validate_source_adapter_handoff(target: Path, adapter_path: Path | None = None) -> tuple[dict[str, Any], int]:
    counts = {
        "inspected": 0,
        "accepted": 0,
        "rejected": 0,
        "deferred": 0,
    }
    warnings: list[dict[str, Any]] = []

    adapter_payload, adapter_errors, adapter_exit = load_adapter_payload(adapter_path)
    if adapter_exit != EXIT_PASS:
        return {"counts": counts, "errors": adapter_errors, "warnings": warnings}, adapter_exit

    records, errors, exit_code = load_records(target)
    if exit_code != EXIT_PASS:
        return {"counts": counts, "errors": errors, "warnings": warnings}, exit_code

    duplicate_sequence_lines, noncontiguous_sequences_error = validate_sequence_values(records)

    for line_number, record in records:
        counts["inspected"] += 1
        record_errors = validate_source_adapter_handoff_record(record, adapter_payload=adapter_payload)
        if line_number in duplicate_sequence_lines:
            record_errors.append(duplicate_sequence_lines[line_number])
        if noncontiguous_sequences_error is not None:
            record_errors.append(noncontiguous_sequences_error)
        if record_errors:
            counts["rejected"] += 1
            for message in record_errors:
                add_error(errors, code="INVALID_HANDOFF_RECORD", line=line_number, message=message)
        else:
            counts["accepted"] += 1

    return {"counts": counts, "errors": errors, "warnings": warnings}, EXIT_PASS if not errors else EXIT_VALIDATION_FAILED


def main() -> int:
    args = parse_args()
    target = Path(args.target)
    adapter_path = Path(args.adapter) if args.adapter else None
    result, exit_code = validate_source_adapter_handoff(target, adapter_path=adapter_path)
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
    sys.stdout.write(render_text_report(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
